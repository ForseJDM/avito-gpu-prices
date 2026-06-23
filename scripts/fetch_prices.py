#!/usr/bin/env python3
"""
Avito GPU Price Fetcher v2.1.1 (rev5 - Incremental)

Strategy:
  - Reads existing prices.json (if any) and skips already-parsed models
  - Parses 1 page per model (Avito bans IP after ~2 page loads)
  - Merges new results into existing prices.json
  - Each run adds a few models; over multiple runs, all get covered
  - Schedule workflow every 4 hours for gradual coverage

Usage:
    python fetch_prices.py [--template TEMPLATE] [--output OUTPUT] [--debug]
    python fetch_prices.py --model "RTX 4060"  # single model
    python fetch_prices.py --force             # re-parse all (ignore existing)
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
MAX_PAGES = 1          # Only 1 page per model (Avito bans after ~2 requests)
DELAY_MIN = 6.0        # Delay between models
DELAY_MAX = 12.0
PRICE_MIN = 1000       # Absolute minimum price (rubles)
PRICE_MAX = 500000     # Absolute maximum price (rubles)
MIN_RESULTS = 3
# Dynamic filtering: after IQR, remove prices below this fraction of median
# This catches accessories mixed into GPU search results
MIN_PRICE_FRACTION = 0.30  # Price < 30% of median = likely not a GPU
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
#  JS helpers
# ---------------------------------------------------------------------------

DETECT_CAPTCHA_JS = """
() => {
    const body = (document.body?.textContent || '').substring(0, 3000);
    const title = document.title || '';
    const indicators = [
        'Доступ ограничен', 'доступ ограничен',
        'Доступ к ресурсу заблокирован',
        'Подтвердите что вы не робот',
        'Access denied',
    ];
    for (const ind of indicators) {
        if (body.includes(ind) || title.toLowerCase().includes(ind.toLowerCase())) {
            return { blocked: true, reason: ind, title };
        }
    }
    return { blocked: false, reason: null, title };
}
"""

EXTRACT_EMBEDDED_JS = """
() => {
    const prices = [];
    const seen = new Set();
    function addPrice(n) {
        if (!isNaN(n) && n > 0 && !seen.has(n)) { seen.add(n); prices.push(n); }
    }
    document.querySelectorAll('script').forEach(script => {
        const text = script.textContent || '';
        if (text.includes('"price"') && text.length < 5000000) {
            try {
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

EXTRACT_DOM_JS = """
() => {
    const prices = [];
    const seen = new Set();
    function addPrice(n) {
        if (!isNaN(n) && n > 0 && !seen.has(n)) { seen.add(n); prices.push(n); }
    }
    document.querySelectorAll('[data-marker="item-price"], [data-marker="item-view/item-price"]').forEach(el => {
        const c = el.getAttribute('content');
        if (c) addPrice(parseInt(c, 10));
        el.querySelectorAll('meta[itemprop="price"], meta[itemProp="price"]').forEach(m => {
            const mc = m.getAttribute('content');
            if (mc) addPrice(parseInt(mc, 10));
        });
    });
    document.querySelectorAll('meta[itemprop="price"], meta[itemProp="price"]').forEach(el => {
        const c = el.getAttribute('content');
        if (c) addPrice(parseInt(c, 10));
    });
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
    re.compile(r'data-marker="item-price"[^>]*content="(\d+)"', re.IGNORECASE),
    re.compile(r'content="(\d+)"[^>]*data-marker="item-price"', re.IGNORECASE),
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
#  Browser
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


# ---------------------------------------------------------------------------
#  Fetch one model
# ---------------------------------------------------------------------------

def fetch_model(search_name, full_name, context, debug=False, debug_dir=DEBUG_DIR):
    """Fetch prices for one model. Returns (full_name, prices, was_blocked)."""
    page = context.new_page()
    page.add_init_script(STEALTH_JS)

    query = f"{search_name} видеокарта"
    search_query = urllib.parse.quote(query, safe='+')
    all_prices = []
    was_blocked = False

    try:
        url = f"{BASE_URL}?q={search_query}&category_id={CATEGORY_ID}&sort=date&p=1"
        if debug:
            print(f"  [DEBUG] {url}")

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except: pass
            page.wait_for_timeout(RENDER_WAIT)

            # Scroll
            page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
            page.wait_for_timeout(1500)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1500)

            # CAPTCHA check
            captcha = page.evaluate(DETECT_CAPTCHA_JS)
            if captcha.get("blocked"):
                print(f"  [BLOCKED] {captcha.get('reason')}")
                save_debug(f"BLOCKED_{full_name}", page, debug_dir)
                was_blocked = True
                return full_name, [], True

            # Method 1: Embedded
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

            # Method 3: Regex fallback
            if not all_prices:
                html = page.content()
                regex_prices = extract_prices_from_html(html)
                if regex_prices:
                    all_prices.extend(regex_prices)
                    if debug:
                        print(f"  [DEBUG] Regex: {len(regex_prices)} prices")

            # If nothing found, try without category
            if not all_prices:
                if debug:
                    print(f"  [DEBUG] No prices with category, trying without")
                url2 = f"{BASE_URL}?q={search_query}&sort=date&p=1"
                page.goto(url2, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
                try: page.wait_for_load_state("networkidle", timeout=15000)
                except: pass
                page.wait_for_timeout(RENDER_WAIT)

                captcha = page.evaluate(DETECT_CAPTCHA_JS)
                if captcha.get("blocked"):
                    print(f"  [BLOCKED] Also blocked without category")
                    was_blocked = True
                    return full_name, [], True

                all_prices.extend(page.evaluate(EXTRACT_EMBEDDED_JS) or [])
                all_prices.extend(page.evaluate(EXTRACT_DOM_JS) or [])
                if not all_prices:
                    all_prices.extend(extract_prices_from_html(page.content()))

                if not all_prices and debug:
                    save_debug(f"EMPTY_{full_name}", page, debug_dir)

        except Exception as e:
            print(f"  [WARN] Error: {e}")

    finally:
        page.close()

    unique = list(set(p for p in all_prices if PRICE_MIN <= p <= PRICE_MAX))
    return full_name, unique, was_blocked


# ---------------------------------------------------------------------------
#  Load/save prices.json with merge
# ---------------------------------------------------------------------------

def load_existing_prices(output_path):
    """Load existing prices.json, return dict of model->price_entry."""
    existing = {}
    if not output_path.exists():
        return existing
    try:
        with open(output_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for entry in data.get("prices", []):
            if "model" in entry:
                existing[entry["model"]] = entry
    except Exception:
        pass
    return existing


def save_prices(output_path, existing, new_entries, template, failed_models):
    """Merge new entries into existing and save."""
    # Update existing with new data
    for entry in new_entries:
        existing[entry["model"]] = entry

    all_prices = sorted(existing.values(), key=lambda e: e.get("model", ""))

    now = datetime.now(timezone.utc)
    output = {
        "version": template.get("version", 1),
        "updated_at": now.isoformat(),
        "update_interval_hours": template.get("update_interval_hours", 24),
        "total_models": len(template.get("models", [])),
        "parsed_models": len(all_prices),
        "coverage_percent": round(len(all_prices) / max(len(template.get("models", [])), 1) * 100, 1),
        "failed_models": failed_models,
        "prices": all_prices,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    return output


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Avito GPU Price Fetcher v2.1.1 (Incremental)")
    parser.add_argument("--template", default=DEFAULT_TEMPLATE)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--debug", action="store_true", default=True)
    parser.add_argument("--no-debug", action="store_true")
    parser.add_argument("--model", type=str, default=None,
                        help="Search name (e.g. 'RTX 4060')")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--force", action="store_true",
                        help="Re-parse all models (ignore existing)")
    args = parser.parse_args()
    debug = args.debug and not args.no_debug

    # Load template
    template_path = Path(args.template)
    if not template_path.exists():
        print(f"[ERROR] Template not found: {template_path}")
        sys.exit(1)

    with open(template_path, "r", encoding="utf-8") as f:
        template = json.load(f)

    raw_models = template.get("models", [])
    if not raw_models:
        print("[ERROR] No models in template")
        sys.exit(1)

    # Build model list
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
            print(f"[ERROR] Model '{args.model}' not in template")
            sys.exit(1)

    # Load existing prices
    output_path = Path(args.output)
    existing = {} if args.force else load_existing_prices(output_path)

    # Skip already-parsed models (unless --force)
    if not args.force and not args.model:
        remaining = [(s, f) for s, f in models if f not in existing]
        already = len(models) - len(remaining)
        if already > 0:
            print(f"[INFO] Skipping {already} already-parsed models")
        models = remaining

    if not models:
        print(f"[INFO] All models already parsed! Coverage: {len(existing)}/{len(raw_models)}")
        print(f"[DONE] No work needed. Output: {output_path}")
        return

    if args.limit:
        models = models[:args.limit]

    print(f"[INFO] Models to parse: {len(models)}")
    print(f"[INFO] Already parsed: {len(existing)}/{len(raw_models)}")
    print(f"[INFO] Method: Playwright Stealth (1 page per model)")
    print()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[ERROR] pip install playwright && playwright install chromium")
        sys.exit(1)

    new_entries = []
    failed = []
    global_blocked = False

    with sync_playwright() as p:
        browser, context = create_browser(p)

        for i, (search_name, full_name) in enumerate(models):
            print(f"[{i+1}/{len(models)}] {search_name} -> {full_name}")

            if global_blocked:
                print(f"  [SKIP] IP blocked, skipping")
                failed.append(full_name)
                continue

            name, prices, was_blocked = fetch_model(
                search_name, full_name, context,
                debug=debug, debug_dir=DEBUG_DIR
            )

            if was_blocked:
                global_blocked = True

            if not prices:
                print(f"    NO PRICES ({full_name})")
                failed.append(full_name)
            else:
                # Step 1: IQR outlier removal
                sp = sorted(prices)
                q1 = percentile(sp, 25)
                q3 = percentile(sp, 75)
                iqr = q3 - q1
                filtered = [pr for pr in prices if q1 - 1.5*iqr <= pr <= q3 + 1.5*iqr]
                if len(filtered) < MIN_RESULTS:
                    filtered = prices

                # Step 2: Dynamic fraction filter
                # Remove prices below MIN_PRICE_FRACTION of median
                # This catches accessories (water blocks, fans) mixed into GPU results
                if len(filtered) >= MIN_RESULTS:
                    med = percentile(sorted(filtered), 50)
                    dynamic_min = med * MIN_PRICE_FRACTION
                    fraction_filtered = [pr for pr in filtered if pr >= dynamic_min]
                    if len(fraction_filtered) >= MIN_RESULTS:
                        if debug and len(fraction_filtered) < len(filtered):
                            removed = len(filtered) - len(fraction_filtered)
                            print(f"  [FILTER] Removed {removed} prices below {round(dynamic_min):,} ({MIN_PRICE_FRACTION*100:.0f}% of median)")
                        filtered = fraction_filtered

                stats = calculate_stats(filtered)
                if stats:
                    new_entries.append({
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
                    print(f"  [DELAY] {delay:.1f}s")
                time.sleep(delay)

        context.close()
        browser.close()

    # Merge and save
    output = save_prices(output_path, existing, new_entries, template, failed)

    print()
    print(f"[DONE] New: {len(new_entries)} | Total: {output['parsed_models']}/{output['total_models']} ({output['coverage_percent']}%)")
    if global_blocked:
        print(f"[WARN] IP blocked by Avito. Some models skipped.")
        print(f"[HINT] Next run will continue from where we left off.")
    if failed:
        print(f"[WARN] Failed this run ({len(failed)}): {', '.join(failed[:5])}")
    print(f"[DONE] Output: {output_path}")


if __name__ == "__main__":
    main()
