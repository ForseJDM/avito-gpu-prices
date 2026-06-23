#!/usr/bin/env python3
"""
Avito GPU Price Fetcher v2.1.1 (rev2)
Scrapes Avito search results for each GPU model, extracts prices,
calculates median and percentiles, outputs prices.json.

Uses Playwright with stealth patches to bypass Avito anti-bot.
Extracts prices via page.evaluate() from rendered DOM.

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
import hashlib
from pathlib import Path
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
#  Configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://www.avito.ru/rossiya"
CATEGORY_ID = 163  # Video cards category on Avito
MAX_PAGES = 3      # Max pages per model
DELAY_MIN = 3.0    # Min delay between requests (seconds) - longer for anti-bot
DELAY_MAX = 7.0    # Max delay between requests (seconds)
PRICE_MIN = 500    # Minimum price filter (rubles)
PRICE_MAX = 500000 # Maximum price filter (rubles)
MIN_RESULTS = 3    # Minimum number of prices to calculate stats (lowered for rare models)
PAGE_TIMEOUT = 45000  # Page load timeout (ms)
RENDER_WAIT = 5000    # Wait for JS render (ms)

# Debug output directory (relative to script location)
DEBUG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "debug")

# Model name -> Avito search query mapping
QUERY_OVERRIDES = {
    "Titan RTX": "NVIDIA Titan RTX видеокарта",
    "Titan V": "NVIDIA Titan V видеокарта",
    "Titan Xp": "NVIDIA Titan Xp видеокарта",
    "Titan X": "NVIDIA Titan X видеокарта -Xp",
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
#  Stealth patches for Playwright
# ---------------------------------------------------------------------------

STEALTH_JS = """
// Remove webdriver flag
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// Fake plugins (normal browsers have them)
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5],
});

// Fake languages
Object.defineProperty(navigator, 'languages', {
    get: () => ['ru-RU', 'ru', 'en-US', 'en'],
});

// Override Chrome object (headless lacks it)
window.chrome = {
    runtime: {},
    loadTimes: function() {},
    csi: function() {},
    app: {},
};

// Override permissions
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
        Promise.resolve({ state: Notification.permission }) :
        originalQuery(parameters)
);

// Fake iframe contentWindow (headless detection)
Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
    get: function() { return window; }
});

// Override toString for stealth functions
const nativeToString = Function.prototype.toString;
const customFunctions = new Map();

function patchToString(fn, replacement) {
    customFunctions.set(fn, replacement);
}

Function.prototype.toString = function() {
    if (customFunctions.has(this)) {
        return customFunctions.get(this);
    }
    return nativeToString.call(this);
};
patchToString(navigator.webdriver.get, 'function get webdriver() { [native code] }');
"""


# ---------------------------------------------------------------------------
#  DOM price extraction via page.evaluate()
# ---------------------------------------------------------------------------

EXTRACT_PRICES_JS = """
() => {
    const prices = [];
    const seen = new Set();

    // Strategy 1: data-marker attributes (Avito's React rendering)
    const priceMarkers = document.querySelectorAll(
        '[data-marker="item-price"], [data-marker="item-view/item-price"]'
    );
    priceMarkers.forEach(el => {
        const content = el.getAttribute('content');
        if (content) {
            const num = parseInt(content, 10);
            if (!isNaN(num) && num > 0) prices.push(num);
        }
        // Also check meta children
        const meta = el.querySelector('meta[itemprop="price"], meta[itemProp="price"]');
        if (meta) {
            const mc = meta.getAttribute('content');
            if (mc) {
                const num = parseInt(mc, 10);
                if (!isNaN(num) && num > 0) prices.push(num);
            }
        }
    });

    // Strategy 2: meta itemprop/itemProp="price" directly
    const metaPrices = document.querySelectorAll(
        'meta[itemprop="price"], meta[itemProp="price"]'
    );
    metaPrices.forEach(el => {
        const content = el.getAttribute('content');
        if (content) {
            const num = parseInt(content, 10);
            if (!isNaN(num) && num > 0 && !seen.has(num)) {
                seen.add(num);
                prices.push(num);
            }
        }
    });

    // Strategy 3: Text-based price extraction from listing cards
    // Avito renders prices as formatted text in some layouts
    const priceElements = document.querySelectorAll(
        '[data-marker="item-price"], [class*="price"], [class*="Price"]'
    );
    priceElements.forEach(el => {
        const text = el.textContent || '';
        // Match patterns: "12 345", "12 345 ₽", "12345"
        const matches = text.match(/(\\d[\\d\\s]*)/g);
        if (matches) {
            matches.forEach(m => {
                const cleaned = m.replace(/\\s/g, '');
                const num = parseInt(cleaned, 10);
                if (!isNaN(num) && num > 0 && !seen.has(num)) {
                    seen.add(num);
                    prices.push(num);
                }
            });
        }
    });

    // Strategy 4: Look for JSON-LD data
    const ldScripts = document.querySelectorAll('script[type="application/ld+json"]');
    ldScripts.forEach(script => {
        try {
            const data = JSON.parse(script.textContent);
            if (data.offers) {
                const offerList = Array.isArray(data.offers) ? data.offers : [data.offers];
                offerList.forEach(offer => {
                    if (offer.price) {
                        const num = parseInt(offer.price, 10);
                        if (!isNaN(num) && num > 0 && !seen.has(num)) {
                            seen.add(num);
                            prices.push(num);
                        }
                    }
                });
            }
        } catch (e) {}
    });

    return prices;
}
"""

DETECT_CAPTCHA_JS = """
() => {
    // Detect if page is a CAPTCHA or block page
    const title = document.title || '';
    const bodyText = (document.body?.textContent || '').substring(0, 2000);

    const captchaIndicators = [
        'Доступ ограничен',
        'доступ ограничен',
        'Введите символы',
        'введите символы',
        'Подтвердите, что вы не робот',
        'подтвердите, что вы не робот',
        'Access denied',
        'access denied',
        'cf-challenge',
        'captcha',
        'hcaptcha',
        'recaptcha',
        'Доступ к ресурсу заблокирован',
    ];

    for (const indicator of captchaIndicators) {
        if (bodyText.includes(indicator) || title.includes(indicator)) {
            return { blocked: true, reason: indicator, title: title };
        }
    }

    // Check for Cloudflare challenge page
    const cfChallenge = document.getElementById('cf-challenge-running');
    if (cfChallenge) {
        return { blocked: true, reason: 'Cloudflare challenge', title: title };
    }

    // Check if we got redirected to a non-search page
    const hasSearchResults = document.querySelector(
        '[data-marker="catalog-serp"], [data-marker="item"]'
    );

    return {
        blocked: false,
        reason: null,
        title: title,
        hasResults: !!hasSearchResults
    };
}
"""


# ---------------------------------------------------------------------------
#  Debug helpers
# ---------------------------------------------------------------------------

def save_debug(name, page, html_content, debug_dir, debug=False):
    """Save debug HTML and screenshot for analysis."""
    if not debug:
        return
    os.makedirs(debug_dir, exist_ok=True)
    safe_name = re.sub(r'[^\w]', '_', name)

    # Save HTML
    html_path = os.path.join(debug_dir, f"{safe_name}.html")
    try:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        print(f"  [DEBUG] HTML saved: {html_path}")
    except Exception as e:
        print(f"  [DEBUG] Failed to save HTML: {e}")

    # Save screenshot
    try:
        ss_path = os.path.join(debug_dir, f"{safe_name}.png")
        page.screenshot(path=ss_path, full_page=False)
        print(f"  [DEBUG] Screenshot saved: {ss_path}")
    except Exception as e:
        print(f"  [DEBUG] Failed to save screenshot: {e}")


# ---------------------------------------------------------------------------
#  Fetching with Playwright (stealth mode)
# ---------------------------------------------------------------------------

def create_stealth_browser(playwright):
    """Launch Chromium with stealth patches."""
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
            "--start-maximized",
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
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/avif,image/webp,*/*;q=0.8"
            ),
            "Sec-Ch-Ua": (
                '"Not/A)Brand";v="8", "Chromium";v="126", '
                '"Google Chrome";v="126"'
            ),
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        },
        # Emulate a real browser more closely
        java_script_enabled=True,
        bypass_csp=True,
    )

    return browser, context


def fetch_with_playwright(model_name, debug=False, debug_dir=DEBUG_DIR):
    """Fetch Avito search page using Playwright with stealth mode."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [ERROR] Playwright not installed. Run: pip install playwright && playwright install chromium")
        return []

    query = QUERY_OVERRIDES.get(model_name, f"{model_name} видеокарта")
    search_query = query.replace(" ", "+")

    all_prices = []
    was_blocked = False

    with sync_playwright() as p:
        browser, context = create_stealth_browser(p)
        page = context.new_page()

        # Apply stealth patches before any navigation
        page.add_init_script(STEALTH_JS)

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
                # Navigate with realistic wait strategy
                response = page.goto(
                    url,
                    wait_until="commit",
                    timeout=PAGE_TIMEOUT
                )

                # Wait for content to render
                page.wait_for_timeout(RENDER_WAIT)

                # Additional wait for dynamic content
                try:
                    page.wait_for_selector(
                        '[data-marker="item-price"], [data-marker="item"], [data-marker="catalog-serp"]',
                        timeout=8000
                    )
                except Exception:
                    # No search results found - might be blocked or empty
                    pass

                # Check for CAPTCHA/block
                captcha_info = page.evaluate(DETECT_CAPTCHA_JS)
                if captcha_info.get("blocked"):
                    print(f"  [BLOCKED] Page is a CAPTCHA/block page: {captcha_info.get('reason')}")
                    print(f"  [BLOCKED] Title: {captcha_info.get('title')}")

                    # Save debug info for first blocked attempt
                    if debug or page_num == 1:
                        html = page.content()
                        save_debug(f"BLOCKED_{model_name}", page, html, debug_dir, debug=True)

                    was_blocked = True
                    break

                if debug:
                    print(f"  [DEBUG] Page check: hasResults={captcha_info.get('hasResults')}, title={captcha_info.get('title')}")

                # Extract prices from rendered DOM
                prices = page.evaluate(EXTRACT_PRICES_JS)

                if debug:
                    print(f"  [DEBUG] Extracted {len(prices)} raw prices from DOM")

                # Also try HTML regex as fallback
                if not prices:
                    html = page.content()
                    prices = extract_prices_from_html(html)
                    if debug:
                        print(f"  [DEBUG] Regex fallback: {len(prices)} prices")

                # Save debug for first page of first model
                if page_num == 1 and debug:
                    html = page.content()
                    save_debug(model_name, page, html, debug_dir, debug=True)

                # If no prices and first page, try without category filter
                if not prices and page_num == 1:
                    if debug:
                        print(f"  [DEBUG] No prices with category, trying without")
                    url_no_cat = f"{BASE_URL}?q={search_query}&sort=date&p={page_num}"
                    page.goto(url_no_cat, wait_until="commit", timeout=PAGE_TIMEOUT)
                    page.wait_for_timeout(RENDER_WAIT)

                    captcha_info = page.evaluate(DETECT_CAPTCHA_JS)
                    if captcha_info.get("blocked"):
                        print(f"  [BLOCKED] Also blocked without category")
                        was_blocked = True
                        break

                    prices = page.evaluate(EXTRACT_PRICES_JS)
                    if not prices:
                        html = page.content()
                        prices = extract_prices_from_html(html)

                if not prices:
                    if debug:
                        print(f"  [DEBUG] No prices on page {page_num}, stopping")
                    break

                # Filter prices
                filtered = [p for p in prices if PRICE_MIN <= p <= PRICE_MAX]
                all_prices.extend(filtered)

                if debug:
                    print(f"  [DEBUG] Page {page_num}: {len(filtered)} valid prices (of {len(prices)} raw)")

                # Delay between pages
                if page_num < MAX_PAGES:
                    delay = random.uniform(DELAY_MIN, DELAY_MAX)
                    time.sleep(delay)

            except Exception as e:
                print(f"  [WARN] Page {page_num} error: {e}")
                if debug:
                    try:
                        html = page.content()
                        save_debug(f"ERROR_{model_name}_p{page_num}", page, html, debug_dir, debug=True)
                    except:
                        pass
                break

        browser.close()

    if was_blocked:
        print(f"  [BLOCKED] Avito detected automation. Prices may be unavailable from GitHub Actions IPs.")
        print(f"  [BLOCKED] Consider running parser locally or using a proxy.")

    return all_prices


# ---------------------------------------------------------------------------
#  HTML regex price extraction (fallback)
# ---------------------------------------------------------------------------

PRICE_PATTERNS = [
    re.compile(r'content="(\d+)"[^>]*data-marker="item-price"', re.IGNORECASE),
    re.compile(r'data-marker="item-price"[^>]*content="(\d+)"', re.IGNORECASE),
    re.compile(r'itemprop="price"\s+content="(\d+)"', re.IGNORECASE),
    re.compile(r'itemProp="price"\s+content="(\d+)"', re.IGNORECASE),
    re.compile(r'data-marker="item-view/item-price"[^>]*content="(\d+)"', re.IGNORECASE),
    re.compile(r'"price"\s*:\s*(\d+)', re.IGNORECASE),
    re.compile(r'"price":\s*"(\d+)"', re.IGNORECASE),
]


def extract_prices_from_html(html_text):
    """Extract prices from raw HTML using regex (fallback method)."""
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
#  Main logic
# ---------------------------------------------------------------------------

def process_model(model_name, debug=False, debug_dir=DEBUG_DIR):
    """Process a single GPU model: fetch prices, calculate stats."""
    print(f"  Processing: {model_name}")

    prices = fetch_with_playwright(model_name, debug=debug, debug_dir=debug_dir)

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
    parser = argparse.ArgumentParser(description="Avito GPU Price Fetcher (Stealth)")
    parser.add_argument("--template", default=DEFAULT_TEMPLATE,
                        help="Path to prices_template.json")
    parser.add_argument("--output", default=DEFAULT_OUTPUT,
                        help="Path to output prices.json")
    parser.add_argument("--debug", action="store_true",
                        help="Verbose output + save HTML/screenshots")
    parser.add_argument("--model", type=str, default=None,
                        help="Process only this model (for testing)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit number of models (for testing)")
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
    elif args.limit:
        models = models[:args.limit]
        print(f"[INFO] Limited to first {args.limit} models")

    print(f"[INFO] Fetching prices for {len(models)} models")
    print(f"[INFO] Method: Playwright + Stealth")
    print(f"[INFO] Output: {args.output}")
    if args.debug:
        print(f"[INFO] Debug dir: {DEBUG_DIR}")
    print()

    results = []
    failed = []

    for i, model in enumerate(models):
        print(f"[{i+1}/{len(models)}] {model}")
        stats = process_model(model, debug=args.debug)

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

        # Delay between models
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
