# cdisc.org

Downloads and parses **CDISC** Controlled Terminology (from NCI EVS) and the list of CDISC
Therapeutic Area User Guides (from cdisc.org) into structured CSVs.

## What it scrapes
- Discovers CDISC standard directories on NCI EVS and downloads each Terminology/Glossary `.txt`
  file, converting the tab-separated terminology to CSV (tagged with its standard name).
- Crawls the CDISC website's Therapeutic Areas index and records each user-guide name + URL.

## Source URLs
- https://evs.nci.nih.gov/ftp1/CDISC/ — CDISC Controlled Terminology text files
- https://www.cdisc.org/standards/therapeutic-areas — Therapeutic Area User Guides index

## Output
Written under `CDISC/`:
- `CDISC/data/*.csv` — one CSV per terminology standard, plus `therapeutic_areas.csv`
- `CDISC/raw/*.txt` — raw downloaded terminology files
- `CDISC/tracker.json` — download/parse progress tracker

## Run
```
pip install -r requirements.txt   # no third-party deps; stdlib only
python cdisc_downloader.py
python cdisc_downloader.py --force  # re-download/re-parse everything
```
The output location is fixed to `BASE_DIR/CDISC/` (no `--output-dir` flag on this scraper).

## Notes
- Writes only inside this folder (`BASE_DIR/CDISC/`); `DATA_DIR` now resolves under `BASE_DIR`.
- Final output is **CSV** (in `CDISC/data/`).
- `mirror: false` — incremental: `tracker.json` marks completed standards and they are skipped on
  re-run unless `--force` is given (only new/pending resources are fetched).
- No third-party dependencies; no secrets required.
