# anzctr.org.au

Australian New Zealand Clinical Trials Registry (ANZCTR).

## What it scrapes
Drives the ANZCTR trial search page with Playwright (headful Chrome by default to
clear Cloudflare Turnstile), runs a blank search to select every trial, then uses
the site's built-in "Download ALL ANZCTR trials to Excel" export. The exported ZIP
is unpacked, the Excel workbook is converted to CSV with pandas, and temporary
files (ZIP/XLSX) are cleaned up.

## Source URLs
- https://www.anzctr.org.au/TrialSearch.aspx — trial search + bulk export

## Output
- `anzctr_trials/anzctr_trials.csv` — one row per trial (converted from the site's Excel export).
- `anzctr_trials/*.pdf` — any PDF included in the export ZIP (kept as-is).

## Run
```
pip install -r requirements.txt
python -m playwright install chromium
python anzctr_downloader.py            # headful (recommended, bypasses Turnstile)
python anzctr_downloader.py --headless # headless (may be blocked by Cloudflare)
```

## Notes
- Writes only inside this folder (`BASE_DIR/anzctr_trials/`).
- Full snapshot each run: re-runs the bulk export and rebuilds `anzctr_trials.csv` (mirror: true).
- Needs Playwright + a real Chrome install; the server-side ZIP generation for ~42k
  trials can take several minutes (`DOWNLOAD_WAIT_S = 600`).
