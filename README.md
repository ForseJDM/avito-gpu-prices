#!/usr/bin/env python3
"""
Avito GPU Price Fetcher v2.1.1
Scrapes Avito search results for each GPU model, extracts prices,
calculates median and percentiles, outputs prices.json.

Uses Playwright for JS-rendered pages (Avito requires JavaScript).
Runs in GitHub Actions with headless Chromium.

Usage:
    python fetch_prices.py [--template TEMPLATE] [--output OUTPUT] [--debug]

Environment:
    GITHUB_TOKEN — optional, for API rate limiting
"""

import json
import math
import os
import random
import re
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
#  Configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://www.avito.ru/rossiya"
CATEGORY_ID = 163  # Video cards category on Avito
MAX_PAGES = 3      # Max pages per model (up to ~50 items per page)
DELAY_MIN = 2.0    # Min delay between requests (seconds)
DELAY_MAX = 5.0    # Max delay between requests (seconds)
PRICE_MIN = 500    # Minimum price filter (rubles)
PRICE_MAX = 500000 # Maximum price filter (rubles)
MIN_RESULTS = 5    # Minimum number of prices to calculate stats

# Model name -> Avito search query mapping
# Some models need special query formatting for best results
QUERY_OVERRIDES = {
    "Titan RTX": "NVIDIA Titan RTX видеокарта",
    "Titan V": "NVIDIA Titan V видеокарта",
    "Titan Xp": "NVIDIA Titan Xp видеокарта",
    "Titan X": "NVIDIA Titan X видеокарта -Pascal",  # exclude "Titan Xp"
    "Radeon VII": "AMD Radeon VII видеокарта",
    "Vega 64": "AMD Vega 64 видеокарта",
    "Vega 56": "AMD Vega 56 видеокарта",
}

# Default template and output paths
DEFAULT_TEMPLATE = os.path.join(os.path.dirname(__file__), "prices_template.json")
DEFAULT_OUTPUT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "prices.json")


# ---------------------------------------------------------------------------
#  Statistics helpers
# ---------------------------------------------------------------------------

def percentile(sorted_data, p):
    """Calculate p-th percentile (0-100) from sorted list."""
    if not sorted_data:
        return 0
    n = len(sorted_data)
    k = (n - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_data[int(k)]
    d0 = sorted_data[int(f)] * (c - k)
    d1 = sorted_data[int(c)] * (k - f)
    return d0 + d1


def calculate_stats(prices):
    """Calculate market stats from a list of prices."""
    if not prices or len(prices) < MIN_RESULTS:
        return None

    sorted_prices = sorted(prices)
    median = percentile(sorted_prices, 50)
    p25 = percentile(sorted_prices, 25)
    p75 = percentile(sorted_prices, 75)

    return {
        "average_price": round(median),
        "min_safe_price": round(p25),
        "max_fair_price": round(p75),
        "scam_threshold": round(median * 0.40),
        "sample_size": len(prices),
    }


# ---------------------------------------------------------------------------
#  Price extraction from HTML
# ---------------------------------------------------------------------------

# Avito price patterns in HTML — handle both old and new markup
PRICE_PATTERNS = [
    # data-marker="item-price" content="12345"
    re.compile(r'data-marker="item-price"[^>]*content="(\d+)"', re.IGNORECASE),
    # <meta itemprop="price" content="12345">
    re.compile(r'itemprop="price"\s+content="(\d+)"', re.IGNORECASE),
    # <meta itemProp="price" content="12345">  (React camelCase)
    re.compile(r'itemProp="price"\s+content="(\d+)"', re.IGNORECASE),
    # data-marker="item-view/item-price"
    re.compile(r'data-marker="item-view/item-price"[^>]*content="(\d+)"', re.IGNORECASE),
    # Text price: "12 345 ₽" or "12345руб"
    re.compile(r'(\d[\d\s]*)\s*[₽]|руб', re.IGNORECASE),
]


def extract_prices_from_html(html_text):
    """Extract all prices from Avito search results HTML."""
    prices = []
    seen = set()

    for pattern in PRICE_PATTERNS:
        for match in pattern.finditer(html_text):
            raw = match.group(1)
            # Clean: remove spaces, non-digit chars
            cleaned = re.sub(r'[^\d]', '', raw)
            if not cleaned:
                continue
            price = int(cleaned)
            # Deduplicate and filter
            if price in seen:
                continue
            seen.add(price)
            if PRICE_MIN <= price <= PRICE_MAX:
                prices.append(price)

    return prices


# ---------------------------------------------------------------------------
#  Fetching with Playwright
# ---------------------------------------------------------------------------

def fetch_with_playwright(model_name, debug=False):
    """Fetch Avito search page using Playwright (handles JS rendering)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [ERROR] Playwright not installed. Run: pip install playwright && playwright install chromium")
        return []

    query = QUERY_OVERRIDES.get(model_name, f"{model_name} видеокарта")
    # URL-encode the query for Avito search
    search_query = query.replace(" ", "+")

    all_prices = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="ru-RU",
        )
        page = context.new_page()

        for page_num in range(1, MAX_PAGES + 1):
            url = (
                f"{BASE_URL}"
                f"?q={search_query}"
                f"&category_id={CATEGORY_ID}"
                f"&sort=date"
                f"&p={page_num}"
            )

            if debug:
                print(f"  [DEBUG] Fetching page {page_num}: {url}")

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                # Wait for price elements to render
                page.wait_for_timeout(3000)

                html = page.content()
                prices = extract_prices_from_html(html)

                if not prices and page_num == 1:
                    # No prices on first page — model may not have listings
                    if debug:
                        print(f"  [DEBUG] No prices found on first page, trying without category filter")
                    url_no_cat = f"{BASE_URL}?q={search_query}&sort=date&p={page_num}"
                    page.goto(url_no_cat, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(3000)
                    html = page.content()
                    prices = extract_prices_from_html(html)

                if not prices:
                    if debug:
                        print(f"  [DEBUG] No prices on page {page_num}, stopping pagination")
                    break

                all_prices.extend(prices)

                if debug:
                    print(f"  [DEBUG] Page {page_num}: {len(prices)} prices found")

                # Random delay between pages
                if page_num < MAX_PAGES:
                    delay = random.uniform(DELAY_MIN, DELAY_MAX)
                    time.sleep(delay)

            except Exception as e:
                print(f"  [WARN] Page {page_num} error: {e}")
                break

        browser.close()

    return all_prices


# ---------------------------------------------------------------------------
#  Fallback: fetch with requests + BeautifulSoup (no JS rendering)
# ---------------------------------------------------------------------------

def fetch_with_requests(model_name, debug=False):
    """Fallback: fetch Avito search page using requests (limited, no JS)."""
    try:
        import requests
    except ImportError:
        print("  [ERROR] requests not installed. Run: pip install requests")
        return []

    query = QUERY_OVERRIDES.get(model_name, f"{model_name} видеокарта")
    search_query = query.replace(" ", "+")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    all_prices = []

    for page_num in range(1, MAX_PAGES + 1):
        url = (
            f"{BASE_URL}"
            f"?q={search_query}"
            f"&category_id={CATEGORY_ID}"
            f"&sort=date"
            f"&p={page_num}"
        )

        if debug:
            print(f"  [DEBUG] Fetching page {page_num}: {url}")

        try:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code != 200:
                print(f"  [WARN] HTTP {resp.status_code} on page {page_num}")
                break

            prices = extract_prices_from_html(resp.text)
            if not prices:
                break

            all_prices.extend(prices)

            if page_num < MAX_PAGES:
                delay = random.uniform(DELAY_MIN, DELAY_MAX)
                time.sleep(delay)

        except Exception as e:
            print(f"  [WARN] Page {page_num} error: {e}")
            break

    return all_prices


# ---------------------------------------------------------------------------
#  Main logic
# ---------------------------------------------------------------------------

def build_search_query(model_name):
    """Convert model name to Avito search query string."""
    return QUERY_OVERRIDES.get(model_name, f"{model_name} видеокарта")


def process_model(model_name, use_playwright=True, debug=False):
    """Process a single GPU model: fetch prices, calculate stats."""
    print(f"  Processing: {model_name}")

    if use_playwright:
        prices = fetch_with_playwright(model_name, debug)
    else:
        prices = fetch_with_requests(model_name, debug)

    if not prices:
        print(f"    NO PRICES FOUND ({model_name})")
        return None

    # Remove outliers using IQR method
    sorted_prices = sorted(prices)
    q1 = percentile(sorted_prices, 25)
    q3 = percentile(sorted_prices, 75)
    iqr = q3 - q1
    lower_bound = q1 - 1.5 * iqr
    upper_bound = q3 + 1.5 * iqr
    filtered = [p for p in prices if lower_bound <= p <= upper_bound]

    if len(filtered) < MIN_RESULTS:
        # Use unfiltered if too few after IQR
        filtered = prices

    stats = calculate_stats(filtered)
    if stats:
        print(f"    OK: median={stats['average_price']:,} | "
              f"range={stats['min_safe_price']:,}-{stats['max_fair_price']:,} | "
              f"scam<{stats['scam_threshold']:,} | "
              f"n={stats['sample_size']}")
    return stats


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Avito GPU Price Fetcher")
    parser.add_argument("--template", default=DEFAULT_TEMPLATE,
                        help="Path to prices_template.json")
    parser.add_argument("--output", default=DEFAULT_OUTPUT,
                        help="Path to output prices.json")
    parser.add_argument("--no-playwright", action="store_true",
                        help="Use requests instead of Playwright (limited)")
    parser.add_argument("--debug", action="store_true",
                        help="Verbose output")
    parser.add_argument("--model", type=str, default=None,
                        help="Process only this model (for testing)")
    args = parser.parse_args()

    # Load template
    template_path = Path(args.template)
    if not template_path.exists():
        print(f"[ERROR] Template not found: {template_path}")
        sys.exit(1)

    with open(template_path, "r", encoding="utf-8") as f:
        template = json.load(f)

    models = template.get("models", [])
    if not models:
        print("[ERROR] No models in template")
        sys.exit(1)

    if args.model:
        models = [args.model]
        print(f"[INFO] Single model mode: {args.model}")

    use_playwright = not args.no_playwright
    print(f"[INFO] Fetching prices for {len(models)} models")
    print(f"[INFO] Method: {'Playwright' if use_playwright else 'requests'}")
    print(f"[INFO] Output: {args.output}")
    print()

    results = []
    failed = []
    skipped = 0

    for i, model in enumerate(models):
        print(f"[{i+1}/{len(models)}] {model}")
        stats = process_model(model, use_playwright=use_playwright, debug=args.debug)

        if stats:
            results.append({
                "model": model,
                "average_price": stats["average_price"],
                "min_safe_price": stats["min_safe_price"],
                "max_fair_price": stats["max_fair_price"],
                "scam_threshold": stats["scam_threshold"],
                "sample_size": stats["sample_size"],
            })
        else:
            failed.append(model)
            skipped += 1

        # Delay between models to avoid rate limiting
        if i < len(models) - 1:
            delay = random.uniform(DELAY_MIN, DELAY_MAX)
            time.sleep(delay)

    # Build output JSON
    now = datetime.now(timezone.utc)
    output = {
        "version": template.get("version", 1),
        "updated_at": now.isoformat(),
        "update_interval_hours": template.get("update_interval_hours", 24),
        "total_models": len(models),
        "parsed_models": len(results),
        "failed_models": failed,
        "prices": results,
    }

    # Write output
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print()
    print(f"[DONE] Parsed: {len(results)}/{len(models)} models")
    if failed:
        print(f"[WARN] Failed models ({len(failed)}):")
        for m in failed:
            print(f"  - {m}")
    print(f"[DONE] Output: {output_path}")


if __name__ == "__main__":
    main()
