#!/usr/bin/env python3
"""
WelleCo Competitor Ads Tracker
Scrapes Meta Ad Library daily and pushes new ads to Notion.
Runs via GitHub Actions — no local computer needed.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ── Config ───────────────────────────────────────────────────────────────────

NOTION_API_KEY     = os.environ["NOTION_API_KEY"]
NOTION_DATABASE_ID = "0d54f62b-e747-4925-b3b3-4dd4f9544bf2"
NOTION_API_BASE    = "https://api.notion.com/v1"
SEEN_FILE          = Path("seen_ad_ids.json")
LOOKBACK_DAYS      = 2

COMPETITORS = [
    {"brand": "AG1",               "url": "https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=AU&q=AG1+Athletic+Greens&search_type=keyword_unordered"},
    {"brand": "Vital Proteins",    "url": "https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=AU&q=Vital+Proteins&search_type=keyword_unordered"},
    {"brand": "JSHealth Vitamins", "url": "https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=AU&q=JSHealth+Vitamins&search_type=keyword_unordered"},
    {"brand": "The Beauty Chef",   "url": "https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=AU&q=The+Beauty+Chef&search_type=keyword_unordered"},
    {"brand": "Ancient Nutrition", "url": "https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=AU&q=Ancient+Nutrition&search_type=keyword_unordered"},
    {"brand": "Bloom Nutrition",   "url": "https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=AU&q=Bloom+Nutrition&search_type=keyword_unordered"},
    {"brand": "Organifi",          "url": "https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=AU&q=Organifi&search_type=keyword_unordered"},
    {"brand": "The Collagen Co",   "url": "https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=AU&q=The+Collagen+Co&search_type=keyword_unordered"},
    {"brand": "Vida Glow",         "url": "https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=AU&q=Vida+Glow&search_type=keyword_unordered"},
    {"brand": "Swisse Beauty",     "url": "https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=AU&q=Swisse+Beauty&search_type=keyword_unordered"},
]

SPEND_BUCKETS = [
    (500_000, "$10k+"),
    (200_000, "$5k - $9.9k"),
    (50_000,  "$1k - $4.9k"),
    (10_000,  "$500 - $999"),
    (1_000,   "$100 - $499"),
    (0,       "< $100"),
]

PLATFORM_MAP = {
    "facebook":         "Facebook",
    "instagram":        "Instagram",
    "audience_network": "Audience Network",
    "messenger":        "Messenger",
}

CTA_MAP = {
    "SHOP_NOW":   "Shop Now",
    "LEARN_MORE": "Learn More",
    "SIGN_UP":    "Sign Up",
    "GET_OFFER":  "Get Offer",
    "SUBSCRIBE":  "Subscribe",
}

# ── JS injected into the browser page to extract ad data ─────────────────────

EXTRACT_JS = """
() => {
  const scripts = Array.from(document.querySelectorAll('script'));
  const dataScript = scripts.find(s => s.textContent.includes('ad_archive_id'));
  if (!dataScript) return [];
  const text = dataScript.textContent;

  const adIds = [...new Set([...text.matchAll(/"ad_archive_id":"(\\d+)"/g)].map(m => m[1]))];
  const ads = [];

  for (const adId of adIds) {
    const idx = text.indexOf('"ad_archive_id":"' + adId + '"');
    if (idx < 0) continue;
    const chunk = text.substring(idx, idx + 8000);

    const startMatch = chunk.match(/"start_date":(\\d+)/);
    const pageMatch  = chunk.match(/"page_name":"([^"]+)"/);
    const bodyMatch  = chunk.match(/"body":\\{"text":"((?:[^"\\\\]|\\\\.)*)"/);
    const titleMatch = chunk.match(/"title":"((?:[^"\\\\]|\\\\.)*)"/);
    const platMatch  = chunk.match(/"publisher_platform":\\[([^\\]]+)\\]/);
    const ctaMatch   = chunk.match(/"cta_text":"([^"]+)"/);

    ads.push({
      adId,
      startTimestamp: startMatch ? parseInt(startMatch[1]) : null,
      pageName:       pageMatch  ? pageMatch[1] : '',
      body:           bodyMatch  ? bodyMatch[1].replace(/\\\\n/g, '\\n').replace(/\\\\"/g, '"') : '',
      title:          titleMatch ? titleMatch[1].replace(/\\\\"/g, '"') : '',
      platforms:      platMatch  ? platMatch[1].split(',').map(p => p.replace(/"/g, '').trim()) : [],
      ctaRaw:         ctaMatch   ? ctaMatch[1] : '',
    });
  }
  return ads;
}
"""

IMPRESSIONS_JS = r"""
() => {
  const text = document.body.innerText;
  const m = text.match(/([\d,\.]+\s*[KkMm]?)\s*[–\-]\s*([\d,\.]+\s*[KkMm]?)\s*impressions/i)
         || text.match(/([\d,\.]+\s*[KkMm]+)\s*impressions/i);
  return m ? m[0] : null;
}
"""

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_seen_ids() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen_ids(ids: set):
    SEEN_FILE.write_text(json.dumps(sorted(ids), indent=2))


def cutoff_timestamp() -> int:
    dt = datetime.now(tz=timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    return int(dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def parse_impression_lower(raw: str) -> int:
    """Return lower-bound impression count from a string like '10K–50K impressions'."""
    if not raw:
        return -1
    first = re.split(r"[–\-]", raw)[0].strip()
    first = re.sub(r"[^\d.KkMm]", "", first)
    try:
        if first.lower().endswith("k"):
            return int(float(first[:-1]) * 1_000)
        if first.lower().endswith("m"):
            return int(float(first[:-1]) * 1_000_000)
        return int(first.replace(",", "")) if first else -1
    except ValueError:
        return -1


def impressions_to_spend(raw: str) -> str:
    count = parse_impression_lower(raw)
    if count < 0:
        return ""
    for threshold, label in SPEND_BUCKETS:
        if count >= threshold:
            return label
    return ""


def normalise_platform(raw: str) -> str:
    return PLATFORM_MAP.get(raw.lower(), raw.title())


def normalise_cta(raw: str) -> str:
    return CTA_MAP.get(raw.upper(), "")


# ── Notion API ────────────────────────────────────────────────────────────────

def _notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }


def _rich_text(value: str) -> list:
    return [{"text": {"content": value[:2000]}}] if value else []


def create_notion_page(ad: dict) -> bool:
    title_text = ad["title"] or (ad["body"][:100] if ad["body"] else ad["adId"])
    platforms  = [normalise_platform(p) for p in ad["platforms"]]
    valid_plats = {"Facebook", "Instagram", "Audience Network", "Messenger"}
    cta        = normalise_cta(ad["ctaRaw"])
    start_date = (
        datetime.fromtimestamp(ad["startTimestamp"], tz=timezone.utc).strftime("%Y-%m-%d")
        if ad.get("startTimestamp") else None
    )
    ad_link = f"https://www.facebook.com/ads/library/?id={ad['adId']}"

    props: dict = {
        "Ad Title / Copy": {"title": _rich_text(title_text)},
        "Brand":           {"select": {"name": ad["brand"]}},
        "Body Copy":       {"rich_text": _rich_text(ad["body"])},
        "Headline":        {"rich_text": _rich_text(ad["title"])},
        "Ad Library Link": {"url": ad_link},
        "Ad ID":           {"rich_text": _rich_text(ad["adId"])},
    }
    if start_date:
        props["First Seen"] = {"date": {"start": start_date}}
    if platforms:
        props["Platform"] = {"multi_select": [{"name": p} for p in platforms if p in valid_plats]}
    if cta:
        props["CTA"] = {"select": {"name": cta}}
    if ad.get("impressionsRaw"):
        props["Impressions Range"] = {"rich_text": _rich_text(ad["impressionsRaw"])}
    if ad.get("spendRange"):
        props["Spend Range"] = {"select": {"name": ad["spendRange"]}}

    resp = requests.post(
        f"{NOTION_API_BASE}/pages",
        headers=_notion_headers(),
        json={"parent": {"database_id": NOTION_DATABASE_ID}, "properties": props},
        timeout=30,
    )
    if not resp.ok:
        print(f"  [Notion] Error for ad {ad['adId']}: {resp.status_code} {resp.text[:300]}")
    return resp.ok


# ── Scraping ──────────────────────────────────────────────────────────────────

def scrape_competitor(page, competitor: dict, cutoff: int) -> list:
    brand = competitor["brand"]
    try:
        page.goto(competitor["url"], wait_until="networkidle", timeout=30_000)
    except PlaywrightTimeout:
        print(f"  [{brand}] Load timeout — continuing with whatever rendered")

    page.wait_for_timeout(3_000)
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(2_000)

    raw_ads = page.evaluate(EXTRACT_JS)
    recent  = [a for a in raw_ads if a.get("startTimestamp") and a["startTimestamp"] >= cutoff]
    print(f"  [{brand}] {len(raw_ads)} ads visible, {len(recent)} started in last {LOOKBACK_DAYS} days")
    for a in recent:
        a["brand"] = brand
    return recent


def get_impressions(page, ad_id: str) -> tuple:
    try:
        page.goto(
            f"https://www.facebook.com/ads/library/?id={ad_id}",
            wait_until="domcontentloaded",
            timeout=20_000,
        )
        page.wait_for_timeout(2_500)
        raw = page.evaluate(IMPRESSIONS_JS)
        if raw:
            return raw, impressions_to_spend(raw)
    except Exception as exc:
        print(f"    [impressions] Failed for {ad_id}: {exc}")
    return "", ""


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    seen_ids = load_seen_ids()
    cutoff   = cutoff_timestamp()
    print(f"Date cutoff : {datetime.fromtimestamp(cutoff, tz=timezone.utc).date()}")
    print(f"Seen IDs    : {len(seen_ids)} previously tracked")

    all_new_ads: list     = []
    results_by_brand: dict = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-AU",
            timezone_id="Australia/Sydney",
        )
        list_page   = ctx.new_page()
        detail_page = ctx.new_page()

        for comp in COMPETITORS:
            brand   = comp["brand"]
            recent  = scrape_competitor(list_page, comp, cutoff)
            new_ads = [a for a in recent if a["adId"] not in seen_ids]
            results_by_brand[brand] = len(new_ads)

            for ad in new_ads:
                print(f"    → New ad {ad['adId']} — fetching impressions...")
                imp_raw, spend         = get_impressions(detail_page, ad["adId"])
                ad["impressionsRaw"]   = imp_raw
                ad["spendRange"]       = spend
                seen_ids.add(ad["adId"])
                all_new_ads.append(ad)

        browser.close()

    # Push to Notion
    pushed = 0
    for ad in all_new_ads:
        if create_notion_page(ad):
            pushed += 1
            print(f"  [Notion] Created page — {ad['brand']} | {ad['adId']}")

    save_seen_ids(seen_ids)

    # Summary
    print("\n=== SUMMARY ===")
    print(f"Total new ads pushed to Notion: {pushed}")
    for brand, count in results_by_brand.items():
        print(f"  {brand}: {'no new ads' if count == 0 else f'{count} new ad(s)'}")

    no_new = [b for b, c in results_by_brand.items() if c == 0]
    if no_new:
        print(f"\nNo new ads today: {', '.join(no_new)}")

    print("\nNotion database: https://www.notion.so/33d64ffb862581f29d20fd944929eecf")


if __name__ == "__main__":
    main()
