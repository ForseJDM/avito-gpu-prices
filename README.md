# avito-gpu-prices

📌 **Клиентское расширение** для этих данных:**[ForseJDM/avito-gpu-helper](https://github.com/ForseJDM/avito-gpu-helper)**
- Auto-updated GPU market prices for [Avito GPU Helper](https://github.com/ForseJDM/avito-gpu-helper) Chrome Extension.

## How it works

- **GitHub Actions** runs every 4 hours (00:00, 04:00, 08:00, 12:00, 16:00, 20:00 UTC)
- **Playwright** fetches Avito search results for each GPU model
- Prices are extracted, filtered (IQR outliers), and aggregated:
  - `average_price` = median
  - `min_safe_price` = 25th percentile
  - `max_fair_price` = 75th percentile
  - `scam_threshold` = 40% of median
- Result is committed as `prices.json`
- Extension fetches this file via `raw.githubusercontent.com`

## Files

| File | Purpose |
|------|---------|
| `prices.json` | Auto-generated price data (DO NOT EDIT manually) |
| `scripts/prices_template.json` | List of GPU models to parse |
| `scripts/fetch_prices.py` | Avito parser (Playwright) |
| `scripts/validate_prices.py` | Validates prices.json against extension DB |
| `.github/workflows/update-prices.yml` | GitHub Actions workflow |

## Manual trigger

Go to **Actions** tab -> **Update GPU Prices** -> **Run workflow**

Options:
- `model` — parse only one model (e.g. "RTX 4060")
- `debug` — verbose output

## Local testing

```bash
pip install -r requirements.txt
playwright install chromium

# Parse all models
python scripts/fetch_prices.py --debug

# Parse single model
python scripts/fetch_prices.py --model "RTX 4060" --debug

# Validate
python scripts/validate_prices.py --strict
```

## Adding new models

1. Add model name to `scripts/prices_template.json`
2. If the model needs a custom search query, add it to `QUERY_OVERRIDES` in `fetch_prices.py`
3. Push to main branch - next scheduled run will include it
