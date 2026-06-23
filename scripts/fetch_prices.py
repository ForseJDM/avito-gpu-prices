#!/usr/bin/env python3
"""
Avito GPU Price Fetcher v2.1.1 (rev4)
Scrapes Avito search results for each GPU model, extracts prices,
calculates median and percentiles, outputs prices.json.

Strategy:
  1. PRIMARY: Extract embedded JSON from page scripts
  2. SECONDARY: DOM-based extraction from rendered elements
  3. FALLBACK: HTML regex

Uses full model names from template to match gpu-market-db.js.
Limits to 2 pages per model (Avito IP-bans after ~6 requests).

Usage:
    python fetch_prices.py [--template TEMPLATE] [--output OUTPUT] [--debug]
"""

import json
import math
import os
import random
import re
import sys
import time
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
#  Configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://www.avito.ru/rossiya"
CATEGORY_ID = 163
MAX_PAGES = 2          # Avito IP-bans on page 3, so max 2 pages
DELAY_MIN = 6.0        # 6-12 sec between models (slower = less bans)
DELAY_MAX = 12.0
PAGE_DELAY_MIN = 3.0   # 3-6 sec between pages
PAGE_DELAY_MAX = 6.0
PRICE_MIN = 500
PRICE_MAX = 500000
MIN_RESULTS = 3
PAGE_TIMEOUT = 60000
RENDER_WAIT = 8000

DEBUG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "debug")

DEFAULT_TEMPLATE = os.path.join(os.path.dirname(__file__), "prices_template.json")
DEFAULT_OUTPUT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "prices.json")


# ---------------------------------------------------------------------------
#  Statistics
# ---------------------------------------------------------------------------

def percentile(sorted_data, p):
    if not sorted_data:
        return 0
    n = len(sorted_data)
    k = (n - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_data[int(k)]
    return sorted_data[int(f)] * (c - k) + sorted_data[int(c)] * (k - f)


def calculate_stats(prices):
    if not prices or len(prices) < MIN_RESULTS:
        return None
    sp = sorted(prices)
    median = percentile(sp, 50)
    p25 = percentile(sp, 25)
    p75 = percentile(sp, 75)
    return {
        "average_price": round(median),
        "min_safe_price": round(p25),
        "max_fair_price": round(p75),
        "scam_threshold": round(median * 0.40),
        "sample_size": len(prices),
    }


# ---------------------------------------------------------------------------
#  Stealth
# ---------------------------------------------------------------------------

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU', 'ru', 'en-US', 'en'] });
window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){}, app: {} };
"""


# ---------------------------------------------------------------------------
#  JS: Captcha detection
# ---------------------------------------------------------------------------

DETECT_CAPTCHA_JS = """
() => {
    const body = (document.body?.textContent || '').substring(0, 3000);
    const title = document.title || '';
    const indicators = [
        'Доступ ограничен', 'доступ ограничен',
        'Доступ к ресурсу заблокирован',
        'Подтвердите что вы не робот',
        'Access denied', 'captcha',
    ];
    for (const ind of indicators) {
        if (body.includes(ind) || title.toLowerCase().includes(ind.toLowerCase())) {
            return { blocked: true, reason: ind, title };
        }
    }
    return { blocked: false, reason: null, title };
}
"""


# ---------------------------------------------------------------------------
#  JS: Embedded data extraction
# ---------------------------------------------------------------------------

EXTRACT_EMBEDDED_JS = """
() => {
    const prices = [];
    const seen = new Set();
    function addPrice(n) {
        if (!isNaN(n) && n > 0 && !seen.has(n)) { seen.add(n); prices.push(n); }
    }
    // Inline scripts containing "price"
    document.querySelectorAll('script').forEach(script => {
        const text = script.textContent || '';
        if (text.includes('"price"') && text.length < 5000000) {
            try {
                // Match "price":{"value":12345} or "price":12345 or "price":"12345"
                const re = /"price"\\s*:\\s*(?:\\{[^}]*?"value"\\s*:\\s*(\\d+)|"(\\d+)"|(\\d+))/g;
                let m;
                while ((m = re.exec(text)) !== null) {
                    const num = parseInt(m[1] || m[2] || m[3], 10);
                    if (num > 0) addPrice(num);
                }
            } catch(e) {}
        }
    });
    return prices;
}
"""


# ---------------------------------------------------------------------------
#  JS: DOM extraction
# ---------------------------------------------------------------------------

EXTRACT_DOM_JS = """
() => {
    const prices = [];
    const seen = new Set();
    function addPrice(n) {
        if (!isNaN(n) && n > 0 && !seen.has(n)) { seen.add(n); prices.push(n); }
    }
    // data-marker
    document.querySelectorAll('[data-marker="item-price"], [data-marker="item-view/item-price"]').forEach(el => {
        const c = el.getAttribute('content');
        if (c) addPrice(parseInt(c, 10));
        el.querySelectorAll('meta[itemprop="price"], meta[itemProp="price"]').forEach(m => {
            const mc = m.getAttribute('content');
            if (mc) addPrice(parseInt(mc, 10));
        });
    });
    // meta directly
    document.querySelectorAll('meta[itemprop="price"], meta[itemProp="price"]').forEach(el => {
        const c = el.getAttribute('content');
        if (c) addPrice(parseInt(c, 10));
    });
    // JSON-LD
    document.querySelectorAll('script[type="application/ld+json"]').forEach(script => {
        try {
            const d = JSON.parse(script.textContent);
            const offers = d.offers ? (Array.isArray(d.offers) ? d.offers : [d.offers]) : [];
            offers.forEach(o => { if (o.price) addPrice(parseInt(o.price, 10)); });
        } catch(e) {}
    });
    return prices;
}
"""


# ---------------------------------------------------------------------------
#  HTML regex fallback
# ---------------------------------------------------------------------------

PRICE_PATTERNS = [
    re.compile(r'"price"\s*:\s*\{[^}]*?"value"\s*:\s*(\d+)', re.IGNORECASE),
    re.compile(r'"price"\s*:\s*"(\d+)"', re.IGNORECASE),
    re.compile(r'"price"\s*:\s*(\d+)', re.IGNORECASE),
    re.compile(r'content="(\d+)"[^>]*data-marker="item-price"', re.IGNORECASE),
    re.compile(r'data-marker="item-price"[^>]*content="(\d+)"', re.IGNORECASE),
    re.compile(r'itemprop="price"\s+content="(\d+)"', re.IGNORECASE),
    re.compile(r'itemProp="price"\s+content="(\d+)"', re.IGNORECASE),
]

def extract_prices_from_html(html_text):
    prices = []
    seen = set()
    for pattern in PRICE_PATTERNS:
        for match in pattern.finditer(html_text):
            cleaned = re.sub(r'[^\d]', '', match.group(1))
            if not cleaned: continue
            price = int(cleaned)
            if price in seen: continue
            seen.add(price)
            if PRICE_MIN <= price <= PRICE_MAX:
                prices.append(price)
    return prices


# ---------------------------------------------------------------------------
#  Debug
# ---------------------------------------------------------------------------

def save_debug(name, page, debug_dir):
    os.makedirs(debug_dir, exist_ok=True)
    safe_name = re.sub(r'[^\w]', '_', name)
    try:
        with open(os.path.join(debug_dir, f"{safe_name}.html"), "w", encoding="utf-8") as f:
            f.write(page.content())
    except: pass
    try:
        page.screenshot(path=os.path.join(debug_dir, f"{safe_name}.png"), full_page=False)
    except: pass


# ---------------------------------------------------------------------------
#  Main fetch
# ---------------------------------------------------------------------------

def create_browser(playwright):
    browser = playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox", "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--window-size=1920,1080",
        ]
    )
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1920, "height": 1080},
        locale="ru-RU",
        timezone_id="Europe/Moscow",
        extra_http_headers={
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Sec-Ch-Ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
        },
        java_script_enabled=True,
        bypass_csp=True,
    )
    return browser, context


def fetch_model(search_name, full_name, browser, context, debug=False, debug_dir=DEBUG_DIR):
    """Fetch prices for one model. Returns (full_name, prices_list) or (full_name, [])."""
    page = context.new_page()
    page.add_init_script(STEALTH_JS)

    query = f"{search_name} видеокарта"
    search_query = urllib.parse.quote(query, safe='+')
    all_prices = []
    was_blocked = False

    try:
        for page_num in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}?q={search_query}&category_id={CATEGORY_ID}&sort=date&p={page_num}"
            if debug:
                print(f"  [DEBUG] Page {page_num}: {url}")

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except: pass
                page.wait_for_timeout(RENDER_WAIT)

                # Scroll for lazy loading
                page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                page.wait_for_timeout(1500)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1500)

                # CAPTCHA check
                captcha = page.evaluate(DETECT_CAPTCHA_JS)
                if captcha.get("blocked"):
                    print(f"  [BLOCKED] {captcha.get('reason')} (title: {captcha.get('title')})")
                    save_debug(f"BLOCKED_{full_name}", page, debug_dir)
                    was_blocked = True
                    break

                # Method 1: Embedded data
                embedded = page.evaluate(EXTRACT_EMBEDDED_JS) or []
                if embedded:
                    all_prices.extend(embedded)
                    if debug:
                        print(f"  [DEBUG] Embedded: {len(embedded)} prices")

                # Method 2: DOM
                dom = page.evaluate(EXTRACT_DOM_JS) or []
                if dom:
                    all_prices.extend(dom)
                    if debug:
                        print(f"  [DEBUG] DOM: {len(dom)} prices")

                # Method 3: HTML regex (only if nothing found)
                if not all_prices:
                    html = page.content()
                    regex_prices = extract_prices_from_html(html)
                    if regex_prices:
                        all_prices.extend(regex_prices)
                        if debug:
                            print(f"  [DEBUG] Regex: {len(regex_prices)} prices")

                # No results on first page? Try without category
                if not all_prices and page_num == 1:
                    if debug:
                        print(f"  [DEBUG] No prices with category, trying without")
                    url2 = f"{BASE_URL}?q={search_query}&sort=date&p={page_num}"
                    page.goto(url2, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
                    try: page.wait_for_load_state("networkidle", timeout=15000)
                    except: pass
                    page.wait_for_timeout(RENDER_WAIT)

                    captcha = page.evaluate(DETECT_CAPTCHA_JS)
                    if captcha.get("blocked"):
                        print(f"  [BLOCKED] Also blocked without category")
                        was_blocked = True
                        break

                    all_prices.extend(page.evaluate(EXTRACT_EMBEDDED_JS) or [])
                    all_prices.extend(page.evaluate(EXTRACT_DOM_JS) or [])
                    if not all_prices:
                        all_prices.extend(extract_prices_from_html(page.content()))

                    if not all_prices and debug:
                        save_debug(f"EMPTY_{full_name}", page, debug_dir)

                if not all_prices:
                    break

                # Delay between pages
                if page_num < MAX_PAGES:
                    time.sleep(random.uniform(PAGE_DELAY_MIN, PAGE_DELAY_MAX))

            except Exception as e:
                print(f"  [WARN] Page {page_num} error: {e}")
                break

    finally:
        page.close()

    # Deduplicate and filter
    unique = list(set(p for p in all_prices if PRICE_MIN <= p <= PRICE_MAX))
    return full_name, unique, was_blocked


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Avito GPU Price Fetcher v2.1.1")
    parser.add_argument("--template", default=DEFAULT_TEMPLATE)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--debug", action="store_true", default=True)
    parser.add_argument("--no-debug", action="store_true")
    parser.add_argument("--model", type=str, default=None,
                        help="Search name to process (e.g. 'RTX 4060')")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    debug = args.debug and not args.no_debug

    # Load template
    template_path = Path(args.template)
    if not template_path.exists():
        print(f"[ERROR] Template not found: {template_path}")
        sys.exit(1)

    with open(template_path, "r", encoding="utf-8") as f:
        template = json.load(f)

    # Support both old format (array of strings) and new format (array of objects)
    raw_models = template.get("models", [])
    if not raw_models:
        print("[ERROR] No models in template")
        sys.exit(1)

    models = []
    for m in raw_models:
        if isinstance(m, dict):
            models.append((m["search"], m["full_name"]))
        else:
            models.append((m, m))

    # Filter by --model
    if args.model:
        models = [(s, f) for s, f in models if s == args.model or f == args.model]
        if not models:
            print(f"[ERROR] Model '{args.model}' not found in template")
            sys.exit(1)
        print(f"[INFO] Single model: {models[0][0]} -> {models[0][1]}")

    if args.limit:
        models = models[:args.limit]
        print(f"[INFO] Limited to {args.limit} models")

    print(f"[INFO] Fetching prices for {len(models)} models")
    print(f"[INFO] Method: Playwright Stealth + Embedded/DOM extraction")
    print(f"[INFO] Max pages per model: {MAX_PAGES}")
    print()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[ERROR] pip install playwright && playwright install chromium")
        sys.exit(1)

    results = []
    failed = []
    global_blocked = False

    with sync_playwright() as p:
        browser, context = create_browser(p)

        for i, (search_name, full_name) in enumerate(models):
            print(f"[{i+1}/{len(models)}] {search_name} -> {full_name}")

            if global_blocked:
                print(f"  [SKIP] IP already blocked, skipping remaining models")
                failed.append(full_name)
                continue

            name, prices, was_blocked = fetch_model(
                search_name, full_name, browser, context,
                debug=debug, debug_dir=DEBUG_DIR
            )

            if was_blocked:
                global_blocked = True

            if not prices:
                print(f"    NO PRICES ({full_name})")
                failed.append(full_name)
            else:
                # IQR filtering
                sp = sorted(prices)
                q1 = percentile(sp, 25)
                q3 = percentile(sp, 75)
                iqr = q3 - q1
                filtered = [pr for pr in prices if q1 - 1.5*iqr <= pr <= q3 + 1.5*iqr]
                if len(filtered) < MIN_RESULTS:
                    filtered = prices

                stats = calculate_stats(filtered)
                if stats:
                    results.append({
                        "model": full_name,
                        "average_price": stats["average_price"],
                        "min_safe_price": stats["min_safe_price"],
                        "max_fair_price": stats["max_fair_price"],
                        "scam_threshold": stats["scam_threshold"],
                        "sample_size": stats["sample_size"],
                    })
                    print(f"    OK: median={stats['average_price']:,} | "
                          f"range={stats['min_safe_price']:,}-{stats['max_fair_price']:,} | "
                          f"scam<{stats['scam_threshold']:,} | n={stats['sample_size']}")
                else:
                    failed.append(full_name)

            # Delay between models
            if i < len(models) - 1 and not global_blocked:
                delay = random.uniform(DELAY_MIN, DELAY_MAX)
                if debug:
                    print(f"  [DELAY] {delay:.1f}s before next model")
                time.sleep(delay)

        context.close()
        browser.close()

    # Build output
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

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print()
    print(f"[DONE] Parsed: {len(results)}/{len(models)} models")
    if global_blocked:
        print(f"[WARN] IP was blocked by Avito. Remaining models skipped.")
        print(f"[HINT] Run again later, or reduce model count, or use a proxy.")
    if failed:
        print(f"[WARN] Failed ({len(failed)}): {', '.join(failed[:5])}")
        if len(failed) > 5:
            print(f"  ... and {len(failed) - 5} more")
    print(f"[DONE] Output: {output_path}")


if __name__ == "__main__":
    main()
