#!/usr/bin/env python3
"""
Avito GPU Price Validator v2.1.1
Validates that model names in prices.json match gpu-market-db.js.
Ensures consistency between the remote price source and the extension database.

Usage:
    python validate_prices.py [--prices PRICES] [--db DB] [--strict]

Exit codes:
    0 - all models valid
    1 - validation errors found
    2 - file errors
"""

import json
import os
import re
import sys
from pathlib import Path


DEFAULT_PRICES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "prices.json")
DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "avito-gpu-helper", "src", "db", "gpu-market-db.js"
)

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
