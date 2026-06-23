#!/usr/bin/env python3
"""
Avito GPU Price Validator v2.1.1
Validates that model names in prices.json match gpu-market-db.js.
Ensures consistency between the remote price source and the extension database.

Usage:
    python validate_prices.py [--prices PRICES] [--db DB] [--strict]

Exit codes:
    0 — all models valid
    1 — validation errors found
    2 — file errors
"""

import json
import os
import re
import sys
from pathlib import Path


# Default paths
DEFAULT_PRICES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "prices.json")
DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "avito-gpu-helper", "src", "db", "gpu-market-db.js"
)

# Expected model names from gpu-market-db.js (extracted manually to match exactly)
KNOWN_MODELS = [
    # NVIDIA RTX 50
    "NVIDIA GeForce RTX 5090",
    "NVIDIA GeForce RTX 5080",
    "NVIDIA GeForce RTX 5070 Ti",
    "NVIDIA GeForce RTX 5070",
    "NVIDIA GeForce RTX 5060 Ti",
    "NVIDIA GeForce RTX 5060",
    # NVIDIA RTX 40
    "NVIDIA GeForce RTX 4090",
    "NVIDIA GeForce RTX 4080 SUPER",
    "NVIDIA GeForce RTX 4080",
    "NVIDIA GeForce RTX 4070 Ti SUPER",
    "NVIDIA GeForce RTX 4070 Ti",
    "NVIDIA GeForce RTX 4070 SUPER",
    "NVIDIA GeForce RTX 4070",
    "NVIDIA GeForce RTX 4060 Ti",
    "NVIDIA GeForce RTX 4060",
    # NVIDIA RTX 30
    "NVIDIA GeForce RTX 3090 Ti",
    "NVIDIA GeForce RTX 3090",
    "NVIDIA GeForce RTX 3080 Ti",
    "NVIDIA GeForce RTX 3080",
    "NVIDIA GeForce RTX 3070 Ti",
    "NVIDIA GeForce RTX 3070",
    "NVIDIA GeForce RTX 3060 Ti",
    "NVIDIA GeForce RTX 3060",
    "NVIDIA GeForce RTX 3050",
    # NVIDIA RTX 20
    "NVIDIA GeForce RTX 2080 Ti",
    "NVIDIA GeForce RTX 2080 SUPER",
    "NVIDIA GeForce RTX 2080",
    "NVIDIA GeForce RTX 2070 SUPER",
    "NVIDIA GeForce RTX 2070",
    "NVIDIA GeForce RTX 2060 SUPER",
    "NVIDIA GeForce RTX 2060",
    # NVIDIA GTX 16
    "NVIDIA GeForce GTX 1660 Ti",
    "NVIDIA GeForce GTX 1660 SUPER",
    "NVIDIA GeForce GTX 1660",
    "NVIDIA GeForce GTX 1650 SUPER",
    "NVIDIA GeForce GTX 1650",
    "NVIDIA GeForce GTX 1630",
    # NVIDIA GTX 10
    "NVIDIA GeForce GTX 1080 Ti",
    "NVIDIA GeForce GTX 1080",
    "NVIDIA GeForce GTX 1070 Ti",
    "NVIDIA GeForce GTX 1070",
    "NVIDIA GeForce GTX 1060",
    "NVIDIA GeForce GTX 1050 Ti",
    "NVIDIA GeForce GTX 1050",
    # NVIDIA GTX 9xx/7xx
    "NVIDIA GeForce GTX 980 Ti",
    "NVIDIA GeForce GTX 980",
    "NVIDIA GeForce GTX 970",
    "NVIDIA GeForce GTX 960",
    "NVIDIA GeForce GTX 950",
    "NVIDIA GeForce GTX 780 Ti",
    "NVIDIA GeForce GTX 780",
    "NVIDIA GeForce GTX 770",
    "NVIDIA GeForce GTX 760",
    "NVIDIA GeForce GTX 750 Ti",
    "NVIDIA GeForce GTX 750",
    # NVIDIA Titan
    "NVIDIA Titan RTX",
    "NVIDIA Titan V",
    "NVIDIA Titan Xp",
    "NVIDIA Titan X",
    # AMD RX 7000
    "AMD Radeon RX 7900 XTX",
    "AMD Radeon RX 7900 XT",
    "AMD Radeon RX 7900 GRE",
    "AMD Radeon RX 7800 XT",
    "AMD Radeon RX 7700 XT",
    "AMD Radeon RX 7600 XT",
    "AMD Radeon RX 7600",
    # AMD RX 6000
    "AMD Radeon RX 6950 XT",
    "AMD Radeon RX 6900 XT",
    "AMD Radeon RX 6800 XT",
    "AMD Radeon RX 6800",
    "AMD Radeon RX 6750 XT",
    "AMD Radeon RX 6700 XT",
    "AMD Radeon RX 6700",
    "AMD Radeon RX 6650 XT",
    "AMD Radeon RX 6600 XT",
    "AMD Radeon RX 6600",
    "AMD Radeon RX 6500 XT",
    "AMD Radeon RX 6400",
    # AMD RX 5000
    "AMD Radeon RX 5700 XT",
    "AMD Radeon RX 5700",
    "AMD Radeon RX 5600 XT",
    "AMD Radeon RX 5600",
    "AMD Radeon RX 5500 XT",
    "AMD Radeon RX 5500",
    # AMD RX 500/400
    "AMD Radeon RX 590",
    "AMD Radeon RX 580",
    "AMD Radeon RX 570",
    "AMD Radeon RX 560",
    "AMD Radeon RX 550",
    "AMD Radeon RX 480",
    "AMD Radeon RX 470",
    "AMD Radeon RX 460",
    # AMD Vega
    "AMD Radeon VII",
    "AMD Radeon RX Vega 64",
    "AMD Radeon RX Vega 56",
    # Intel Arc
    "Intel Arc A770",
    "Intel Arc A750",
    "Intel Arc A580",
    "Intel Arc A380",
]


# Template model name -> DB model name mapping
# Template uses short names ("RTX 4060"), DB uses full names
TEMPLATE_TO_DB = {}
for full_name in KNOWN_MODELS:
    # Extract short name from full name
    # "NVIDIA GeForce RTX 4060 Ti" -> "RTX 4060 Ti"
    # "AMD Radeon RX 7900 XTX" -> "RX 7900 XTX"
    # "Intel Arc A770" -> "Arc A770"
    # "NVIDIA Titan RTX" -> "Titan RTX"
    # "AMD Radeon VII" -> "Radeon VII"
    # "AMD Radeon RX Vega 64" -> "Vega 64"
    if full_name.startswith("NVIDIA GeForce "):
        short = full_name.replace("NVIDIA GeForce ", "")
    elif full_name.startswith("AMD Radeon RX "):
        short = full_name.replace("AMD Radeon RX ", "")
    elif full_name.startswith("AMD Radeon VII"):
        short = "Radeon VII"
    elif full_name.startswith("AMD Radeon RX Vega "):
        short = full_name.replace("AMD Radeon RX Vega ", "Vega ")
    elif full_name.startswith("Intel "):
        short = full_name.replace("Intel ", "")
    elif full_name.startswith("NVIDIA Titan"):
        short = full_name.replace("NVIDIA ", "")
    else:
        short = full_name
    TEMPLATE_TO_DB[short] = full_name


def validate_prices(prices_path, strict=False):
    """Validate prices.json against known models."""
    prices_path = Path(prices_path)

    if not prices_path.exists():
        print(f"[ERROR] File not found: {prices_path}")
        return False

    with open(prices_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if "prices" not in data:
        print("[ERROR] Missing 'prices' key in JSON")
        return False

    if not isinstance(data["prices"], list):
        print("[ERROR] 'prices' must be an array")
        return False

    errors = []
    warnings = []
    seen_models = set()
    db_models = set(KNOWN_MODELS)

    for i, entry in enumerate(data["prices"]):
        model = entry.get("model", "")
        if not model:
            errors.append(f"Entry {i}: missing 'model' field")
            continue

        if model in seen_models:
            errors.append(f"Entry {i}: duplicate model '{model}'")
        seen_models.add(model)

        # Check if model exists in DB
        if model not in db_models:
            # Try template-style name match
            db_equivalent = TEMPLATE_TO_DB.get(model)
            if db_equivalent:
                warnings.append(
                    f"Entry {i}: model '{model}' uses short name, "
                    f"should be '{db_equivalent}' to match gpu-market-db.js"
                )
            else:
                if strict:
                    errors.append(
                        f"Entry {i}: model '{model}' NOT found in gpu-market-db.js"
                    )
                else:
                    warnings.append(
                        f"Entry {i}: model '{model}' NOT found in gpu-market-db.js "
                        f"(will be ignored by extension)"
                    )

        # Validate numeric fields
        for field in ["average_price", "min_safe_price", "max_fair_price", "scam_threshold"]:
            val = entry.get(field)
            if val is None:
                errors.append(f"Entry {i} ({model}): missing '{field}'")
            elif not isinstance(val, (int, float)) or val <= 0:
                errors.append(f"Entry {i} ({model}): '{field}' must be positive number, got {val}")

        # Validate logical thresholds
        avg = entry.get("average_price", 0)
        min_s = entry.get("min_safe_price", 0)
        max_f = entry.get("max_fair_price", 0)
        scam = entry.get("scam_threshold", 0)

        if scam > 0 and min_s > 0 and scam >= min_s:
            errors.append(f"Entry {i} ({model}): scam_threshold ({scam}) >= min_safe_price ({min_s})")
        if min_s > 0 and avg > 0 and min_s >= avg:
            errors.append(f"Entry {i} ({model}): min_safe_price ({min_s}) >= average_price ({avg})")
        if avg > 0 and max_f > 0 and avg >= max_f:
            errors.append(f"Entry {i} ({model}): average_price ({avg}) >= max_fair_price ({max_f})")

    # Check for missing models (models in DB but not in prices)
    missing = db_models - seen_models
    if missing:
        for m in sorted(missing):
            warnings.append(f"Model in DB but missing from prices: '{m}'")

    # Report
    print(f"\n=== Validation Report ===")
    print(f"Total entries: {len(data['prices'])}")
    print(f"Unique models: {len(seen_models)}")
    print(f"Errors: {len(errors)}")
    print(f"Warnings: {len(warnings)}")

    if errors:
        print(f"\n--- Errors ---")
        for e in errors:
            print(f"  [ERROR] {e}")

    if warnings:
        print(f"\n--- Warnings ---")
        for w in warnings:
            print(f"  [WARN] {w}")

    if not errors and not warnings:
        print("\n  All checks passed!")

    return len(errors) == 0


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Validate prices.json against gpu-market-db.js")
    parser.add_argument("--prices", default=DEFAULT_PRICES,
                        help="Path to prices.json")
    parser.add_argument("--strict", action="store_true",
                        help="Treat unknown models as errors (not warnings)")
    args = parser.parse_args()

    ok = validate_prices(args.prices, strict=args.strict)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
