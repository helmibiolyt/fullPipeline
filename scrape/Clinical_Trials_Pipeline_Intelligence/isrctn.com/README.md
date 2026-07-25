# isrctn.com

ISRCTN registry (UK-based international clinical trial registry).

## What it scrapes
Drives isrctn.com with Playwright: accepts cookies, opens "View all studies",
opens the "Export as CSV" modal, checks "Select All" fields, clicks Export, and
saves the site-generated CSV. No parsing needed — the registry emits CSV directly.

## Source URLs
- https://www.isrctn.com/ — homepage / "View all studies" / "Export as CSV"

## Output
- `isrctn_trials/<site-suggested-name>.csv` — full study export (one row per study).

## Run
```
pip install -r requirements.txt
python -m playwright install chromium
python isrctn_downloader.py            # headless
python isrctn_downloader.py --visible  # headful (watch the browser)
```

## Notes
- Writes only inside this folder (`BASE_DIR/isrctn_trials/`).
- Full snapshot each run: re-runs the site export (mirror: true).
- Needs Playwright + Chromium.
