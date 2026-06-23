#!/usr/bin/env python3
"""
Avito GPU Price Fetcher v2.1.1 (rev3)
Scrapes Avito search results for each GPU model, extracts prices,
calculates median and percentiles, outputs prices.json.

Strategy:
  1. PRIMARY: Intercept Avito API responses (XHR) containing item data
  2. SECONDARY: Extract embedded JSON from page (window.__INITIAL_STATE__)
  3. FALLBACK: DOM-based extraction from rendered elements

Usage:
    python fetch_prices.py [--template TEMPLATE] [--output OUTPUT] [--debug]

Environment:
    GITHUB_TOKEN — optional
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
CATEGORY_ID = 163  # Video cards
MAX_PAGES = 3
DELAY_MIN = 4.0    # Longer delays to avoid rate limiting
DELAY_MAX = 8.0
PRICE_MIN = 500
PRICE_MAX = 500000
MIN_RESULTS = 3
PAGE_TIMEOUT = 60000   # 60s timeout
RENDER_WAIT = 8000      # 8s wait for JS render

DEBUG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "debug")

QUERY_OVERRIDES = {
    "Titan RTX": "NVIDIA Titan RTX видеокарта",
    "Titan V": "NVIDIA Titan V видеокарта",
    "Titan Xp": "NVIDIA Titan Xp видеокарта",
    "Titan X": "NVIDIA Titan X видеокарта -Xp",
    "Radeon VII": "AMD Radeon VII видеокарта",
    "Vega 64": "AMD Vega 64 видеокарта",
    "Vega 56": "AMD Vega 56 видеокарта",
}

DEFAULT_TEMPLATE = os.path.join(os.path.dirname(__file__), "prices_template.json")
DEFAULT_OUTPUT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "prices.json")


# ---------------------------------------------------------------------------
#  Statistics helpers
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
    d0 = sorted_data[int(f)] * (c - k)
    d1 = sorted_data[int(c)] * (k - f)
    return d0 + d1


def calculate_stats(prices):
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
#  Stealth patches
# ---------------------------------------------------------------------------

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU', 'ru', 'en-US', 'en'] });
window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){}, app: {} };
const origQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (p) => (
    p.name === 'notifications' ? Promise.resolve({ state: Notification.permission }) : origQuery(p)
);
"""


# ---------------------------------------------------------------------------
#  Captcha detection
# ---------------------------------------------------------------------------

DETECT_CAPTCHA_JS = """
() => {
    const body = (document.body?.textContent || '').substring(0, 3000);
    const title = document.title || '';
    const indicators = [
        'Доступ ограничен', 'доступ ограничен',
        'Введите символы', 'введите символы',
        'Подтвердите что вы не робот', 'подтвердите что вы не робот',
        'Доступ к ресурсу заблокирован',
        'Access denied', 'access denied',
        'cf-challenge', 'captcha', 'hcaptcha', 'recaptcha',
    ];
    for (const ind of indicators) {
        if (body.includes(ind) || title.toLowerCase().includes(ind.toLowerCase())) {
            return { blocked: true, reason: ind, title };
        }
    }
    if (document.getElementById('cf-challenge-running')) {
        return { blocked: true, reason: 'Cloudflare challenge', title };
    }
    return { blocked: false, reason: null, title };
}
"""


# ---------------------------------------------------------------------------
#  DOM price extraction
# ---------------------------------------------------------------------------

EXTRACT_PRICES_DOM_JS = """
() => {
    const prices = [];
    const seen = new Set();

    function addPrice(n) {
        if (!isNaN(n) && n > 0 && !seen.has(n)) {
            seen.add(n);
            prices.push(n);
        }
    }

    // Strategy 1: data-marker attributes
    document.querySelectorAll('[data-marker="item-price"], [data-marker="item-view/item-price"]').forEach(el => {
        const c = el.getAttribute('content');
        if (c) addPrice(parseInt(c, 10));
        el.querySelectorAll('meta[itemprop="price"], meta[itemProp="price"]').forEach(m => {
            const mc = m.getAttribute('content');
            if (mc) addPrice(parseInt(mc, 10));
        });
    });

    // Strategy 2: meta itemprop directly
    document.querySelectorAll('meta[itemprop="price"], meta[itemProp="price"]').forEach(el => {
        const c = el.getAttribute('content');
        if (c) addPrice(parseInt(c, 10));
    });

    // Strategy 3: text-based from price elements
    document.querySelectorAll('[data-marker="item-price"], [class*="price"], [class*="Price"]').forEach(el => {
        const text = el.textContent || '';
        (text.match(/(\\d[\\d\\s]*)/g) || []).forEach(m => {
            addPrice(parseInt(m.replace(/\\s/g, ''), 10));
        });
    });

    // Strategy 4: JSON-LD
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
#  Page content diagnostic
# ---------------------------------------------------------------------------

DIAGNOSE_PAGE_JS = """
() => {
    const info = {};
    info.title = document.title;
    info.url = document.location.href;
    info.bodyLen = document.body ? document.body.innerHTML.length : 0;

    // Count key elements
    info.itemsWithMarker = document.querySelectorAll('[data-marker]').length;
    info.itemsWithPrice = document.querySelectorAll('[data-marker*="price"]').length;
    info.metaPrices = document.querySelectorAll('meta[itemprop="price"], meta[itemProp="price"]').length;
    info.catalogSerp = document.querySelectorAll('[data-marker="catalog-serp"]').length;
    info.itemCards = document.querySelectorAll('[data-marker="item"]').length;
    info.scripts = document.querySelectorAll('script').length;

    // Check for embedded data
    info.hasNextData = !!document.getElementById('__NEXT_DATA__');
    info.hasInitialState = !!window.__INITIAL_STATE__;

    // Body text preview (first 500 chars)
    info.bodyPreview = (document.body?.textContent || '').substring(0, 500).replace(/\\s+/g, ' ').trim();

    return info;
}
"""


# ---------------------------------------------------------------------------
#  Extract prices from intercepted API response
# ---------------------------------------------------------------------------

def extract_prices_from_api_response(response_body):
    """Extract prices from Avito API JSON response."""
    prices = []

    try:
        if isinstance(response_body, str):
            data = json.loads(response_body)
        else:
            data = response_body
    except (json.JSONDecodeError, TypeError):
        return prices

    # Try common Avito API response structures
    # Structure 1: { result: { items: [{ price: { value: 12345 } }] } }
    items = []

    if isinstance(data, dict):
        # Navigate through various possible structures
        for key in ["result", "data", "items", "catalog"]:
            if key in data and isinstance(data[key], dict):
                for subkey in ["items", "list", "results", "catalogItems"]:
                    if subkey in data[key] and isinstance(data[key][subkey], list):
                        items = data[key][subkey]
                        break
                if items:
                    break
            elif key in data and isinstance(data[key], list):
                items = data[key]
                break

    if not items and isinstance(data, list):
        items = data

    for item in items:
        if not isinstance(item, dict):
            continue

        # Try various price field paths
        price = None

        # Path 1: item.price.value
        if "price" in item:
            p = item["price"]
            if isinstance(p, dict):
                price = p.get("value") or p.get("amount") or p.get("price")
            elif isinstance(p, (int, float)):
                price = p

        # Path 2: item.priceDict.value
        if not price and "priceDict" in item:
            pd = item["priceDict"]
            if isinstance(pd, dict):
                price = pd.get("value") or pd.get("amount")

        # Path 3: item.salePrice or item.sale_price
        if not price:
            for k in ["salePrice", "sale_price", "cost", "amount"]:
                if k in item:
                    v = item[k]
                    if isinstance(v, dict):
                        price = v.get("value") or v.get("amount")
                    elif isinstance(v, (int, float)):
                        price = v
                    if price:
                        break

        # Path 4: item.params with price
        if not price and "params" in item:
            params = item["params"]
            if isinstance(params, dict):
                price = params.get("price") or params.get("cost")
            elif isinstance(params, list):
                for param in params:
                    if isinstance(param, dict) and param.get("key") in ["price", "cost", "Цена"]:
                        try:
                            price = int(re.sub(r'[^\d]', '', str(param.get("value", ""))))
                        except:
                            pass
                        if price:
                            break

        if price and isinstance(price, (int, float)) and PRICE_MIN <= price <= PRICE_MAX:
            prices.append(int(price))

    return prices


# ---------------------------------------------------------------------------
#  Extract from embedded page data
# ---------------------------------------------------------------------------

EXTRACT_EMBEDDED_JS = """
() => {
    const prices = [];

    // Try __NEXT_DATA__ (Next.js apps)
    const nextDataEl = document.getElementById('__NEXT_DATA__');
    if (nextDataEl) {
        try {
            const d = JSON.parse(nextDataEl.textContent);
            const jsonStr = JSON.stringify(d);
            // Extract all price-like numbers from the JSON
            const matches = jsonStr.match(/"price"\\s*:\\s*[{"]*?(\\d+)/g) || [];
            matches.forEach(m => {
                const num = parseInt(m.replace(/[^0-9]/g, ''), 10);
                if (num > 0) prices.push(num);
            });
        } catch(e) {}
    }

    // Try window.__INITIAL_STATE__
    if (window.__INITIAL_STATE__) {
        try {
            const jsonStr = JSON.stringify(window.__INITIAL_STATE__);
            const matches = jsonStr.match(/"price"\\s*:\\s*[{"]*?(\\d+)/g) || [];
            matches.forEach(m => {
                const num = parseInt(m.replace(/[^0-9]/g, ''), 10);
                if (num > 0) prices.push(num);
            });
        } catch(e) {}
    }

    // Try any inline script with search results data
    document.querySelectorAll('script').forEach(script => {
        const text = script.textContent || '';
        if (text.includes('"price"') && text.length < 5000000) {
            try {
                const matches = text.match(/"price"\\s*:\\s*[{"]*?(\\d+)/g) || [];
                matches.forEach(m => {
                    const num = parseInt(m.replace(/[^0-9]/g, ''), 10);
                    if (num > 0) prices.push(num);
                });
            } catch(e) {}
        }
    });

    return prices;
}
"""


# ---------------------------------------------------------------------------
#  Debug helpers
# ---------------------------------------------------------------------------

def save_debug(name, page, debug_dir, debug=True):
    if not debug:
        return
    os.makedirs(debug_dir, exist_ok=True)
    safe_name = re.sub(r'[^\w]', '_', name)

    html_path = os.path.join(debug_dir, f"{safe_name}.html")
    try:
        html = page.content()
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as e:
        print(f"  [DEBUG] Save HTML failed: {e}")

    try:
        ss_path = os.path.join(debug_dir, f"{safe_name}.png")
        page.screenshot(path=ss_path, full_page=False)
    except Exception as e:
        print(f"  [DEBUG] Screenshot failed: {e}")


# ---------------------------------------------------------------------------
#  Main fetch with Playwright
# ---------------------------------------------------------------------------

def fetch_model_prices(model_name, browser, context, debug=False, debug_dir=DEBUG_DIR):
    """Fetch prices for a single model using shared browser context."""
    page = context.new_page()

    # Apply stealth
    page.add_init_script(STEALTH_JS)

    query = QUERY_OVERRIDES.get(model_name, f"{model_name} видеокарта")
    search_query = urllib.parse.quote(query, safe='+')

    all_prices = []
    was_blocked = False

    # Intercept API responses
    api_responses = []
    def handle_response(response):
        url = response.url
        # Avito API endpoints for search results
        api_patterns = [
            '/api/', '/web/1/', '/catalog/items',
            '/catalog/search', '/search/items',
        ]
        if any(p in url for p in api_patterns):
            try:
                if response.status == 200:
                    content_type = response.headers.get('content-type', '')
                    if 'json' in content_type or 'javascript' in content_type:
                        body = response.text()
                        api_responses.append(body)
                        if debug:
                            print(f"  [DEBUG] API response captured: {url[:100]} ({len(body)} bytes)")
            except Exception:
                pass

    page.on("response", handle_response)

    try:
        for page_num in range(1, MAX_PAGES + 1):
            url = (
                f"{BASE_URL}"
                f"?q={search_query}"
                f"&category_id={CATEGORY_ID}"
                f"&sort=date"
                f"&p={page_num}"
            )

            if debug:
                print(f"  [DEBUG] Navigating to page {page_num}")

            try:
                # Navigate and wait for network to settle
                page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)

                # Wait for network to be mostly idle
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass  # networkidle can timeout, that's ok

                # Additional render wait
                page.wait_for_timeout(RENDER_WAIT)

                # Scroll down to trigger lazy loading
                page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                page.wait_for_timeout(2000)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(2000)

                # Check for CAPTCHA
                captcha_info = page.evaluate(DETECT_CAPTCHA_JS)
                if captcha_info.get("blocked"):
                    print(f"  [BLOCKED] CAPTCHA/block: {captcha_info.get('reason')}")
                    print(f"  [BLOCKED] Title: {captcha_info.get('title')}")
                    save_debug(f"BLOCKED_{model_name}", page, debug_dir)
                    was_blocked = True
                    break

                # === METHOD 1: API response interception ===
                for body in api_responses:
                    api_prices = extract_prices_from_api_response(body)
                    if api_prices:
                        all_prices.extend(api_prices)
                        if debug:
                            print(f"  [DEBUG] API method: {len(api_prices)} prices")

                # === METHOD 2: Embedded data extraction ===
                embedded_prices = page.evaluate(EXTRACT_EMBEDDED_JS)
                if embedded_prices:
                    all_prices.extend(embedded_prices)
                    if debug:
                        print(f"  [DEBUG] Embedded method: {len(embedded_prices)} prices")

                # === METHOD 3: DOM extraction ===
                dom_prices = page.evaluate(EXTRACT_PRICES_DOM_JS)
                if dom_prices:
                    all_prices.extend(dom_prices)
                    if debug:
                        print(f"  [DEBUG] DOM method: {len(dom_prices)} prices")

                # === METHOD 4: HTML regex fallback ===
                if not all_prices:
                    html = page.content()
                    regex_prices = extract_prices_from_html(html)
                    if regex_prices:
                        all_prices.extend(regex_prices)
                        if debug:
                            print(f"  [DEBUG] Regex fallback: {len(regex_prices)} prices")

                # Diagnostic output
                if debug and not all_prices:
                    diag = page.evaluate(DIAGNOSE_PAGE_JS)
                    print(f"  [DIAG] title={diag.get('title')}")
                    print(f"  [DIAG] url={diag.get('url')}")
                    print(f"  [DIAG] bodyLen={diag.get('bodyLen')}, scripts={diag.get('scripts')}")
                    print(f"  [DIAG] data-marker elements={diag.get('itemsWithMarker')}, price={diag.get('itemsWithPrice')}")
                    print(f"  [DIAG] catalog-serp={diag.get('catalogSerp')}, item cards={diag.get('itemCards')}")
                    print(f"  [DIAG] __NEXT_DATA__={diag.get('hasNextData')}, __INITIAL_STATE__={diag.get('hasInitialState')}")
                    print(f"  [DIAG] body preview: {diag.get('bodyPreview', '')[:300]}")

                    save_debug(f"EMPTY_{model_name}_p{page_num}", page, debug_dir)

                if not all_prices and page_num == 1:
                    # Try without category filter
                    if debug:
                        print(f"  [DEBUG] Trying without category filter")
                    api_responses.clear()
                    url_no_cat = f"{BASE_URL}?q={search_query}&sort=date&p={page_num}"
                    page.goto(url_no_cat, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
                    try:
                        page.wait_for_load_state("networkidle", timeout=15000)
                    except:
                        pass
                    page.wait_for_timeout(RENDER_WAIT)
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                    page.wait_for_timeout(2000)

                    # Re-try all methods
                    for body in api_responses:
                        all_prices.extend(extract_prices_from_api_response(body))
                    all_prices.extend(page.evaluate(EXTRACT_EMBEDDED_JS) or [])
                    all_prices.extend(page.evaluate(EXTRACT_PRICES_DOM_JS) or [])

                    if not all_prices:
                        html = page.content()
                        all_prices.extend(extract_prices_from_html(html))

                    if debug and not all_prices:
                        diag = page.evaluate(DIAGNOSE_PAGE_JS)
                        print(f"  [DIAG no-cat] body preview: {diag.get('bodyPreview', '')[:300]}")
                        save_debug(f"EMPTY_NOCAT_{model_name}", page, debug_dir)

                if not all_prices:
                    if debug:
                        print(f"  [DEBUG] No prices on page {page_num}, stopping")
                    break

                # Clear api_responses for next page
                api_responses.clear()

                if page_num < MAX_PAGES:
                    delay = random.uniform(DELAY_MIN, DELAY_MAX)
                    time.sleep(delay)

            except Exception as e:
                print(f"  [WARN] Page {page_num} error: {e}")
                if debug:
                    save_debug(f"ERROR_{model_name}_p{page_num}", page, debug_dir)
                break

    finally:
        page.close()

    # Deduplicate and filter
    unique_prices = list(set(p for p in all_prices if PRICE_MIN <= p <= PRICE_MAX))

    if was_blocked:
        print(f"  [BLOCKED] Avito detected automation from this IP.")

    return unique_prices


# ---------------------------------------------------------------------------
#  HTML regex fallback
# ---------------------------------------------------------------------------

PRICE_PATTERNS = [
    re.compile(r'content="(\d+)"[^>]*data-marker="item-price"', re.IGNORECASE),
    re.compile(r'data-marker="item-price"[^>]*content="(\d+)"', re.IGNORECASE),
    re.compile(r'itemprop="price"\s+content="(\d+)"', re.IGNORECASE),
    re.compile(r'itemProp="price"\s+content="(\d+)"', re.IGNORECASE),
    re.compile(r'"price"\s*:\s*(\d+)', re.IGNORECASE),
    re.compile(r'"price":\s*"(\d+)"', re.IGNORECASE),
    re.compile(r'"value"\s*:\s*(\d+),', re.IGNORECASE),
]

def extract_prices_from_html(html_text):
    prices = []
    seen = set()
    for pattern in PRICE_PATTERNS:
        for match in pattern.finditer(html_text):
            raw = match.group(1)
            cleaned = re.sub(r'[^\d]', '', raw)
            if not cleaned:
                continue
            price = int(cleaned)
            if price in seen:
                continue
            seen.add(price)
            if PRICE_MIN <= price <= PRICE_MAX:
                prices.append(price)
    return prices


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------

def create_stealth_browser(playwright):
    browser = playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-infobars",
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
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Sec-Ch-Ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
        },
        java_script_enabled=True,
        bypass_csp=True,
    )
    return browser, context


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Avito GPU Price Fetcher (Stealth v3)")
    parser.add_argument("--template", default=DEFAULT_TEMPLATE)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--debug", action="store_true", default=True)
    parser.add_argument("--no-debug", action="store_true", help="Disable debug output")
    parser.add_argument("--model", type=str, default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    debug = args.debug and not args.no_debug

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
    elif args.limit:
        models = models[:args.limit]
        print(f"[INFO] Limited to first {args.limit} models")

    print(f"[INFO] Fetching prices for {len(models)} models")
    print(f"[INFO] Method: Playwright + Stealth + API interception")
    print(f"[INFO] Output: {args.output}")
    print()

    results = []
    failed = []

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[ERROR] Playwright not installed. Run: pip install playwright && playwright install chromium")
        sys.exit(1)

    with sync_playwright() as p:
        browser, context = create_stealth_browser(p)

        for i, model in enumerate(models):
            print(f"[{i+1}/{len(models)}] {model}")

            prices = fetch_model_prices(model, browser, context, debug=debug)

            if not prices:
                print(f"    NO PRICES FOUND ({model})")
                failed.append(model)
            else:
                # IQR outlier removal
                sorted_prices = sorted(prices)
                q1 = percentile(sorted_prices, 25)
                q3 = percentile(sorted_prices, 75)
                iqr = q3 - q1
                lower = q1 - 1.5 * iqr
                upper = q3 + 1.5 * iqr
                filtered = [pr for pr in prices if lower <= pr <= upper]
                if len(filtered) < MIN_RESULTS:
                    filtered = prices

                stats = calculate_stats(filtered)
                if stats:
                    results.append({
                        "model": model,
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
                    failed.append(model)

            if i < len(models) - 1:
                delay = random.uniform(DELAY_MIN, DELAY_MAX)
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
    if failed:
        print(f"[WARN] Failed ({len(failed)}): {', '.join(failed[:10])}")
        if len(failed) > 10:
            print(f"  ... and {len(failed) - 10} more")
    print(f"[DONE] Output: {output_path}")


if __name__ == "__main__":
    main()
