# medrxiv.org

Crawls preprint metadata from medRxiv using the official Cold Spring Harbor Laboratory (CSHL) bioRxiv API.

## What it scrapes
- **Metadata** for preprints hosted on medRxiv (doi, title, authors, corresponding author, corresponding author institution, posting date, version, type, license, subject category, JATS XML link, abstract text, funder details, published journal reference, and host server).

## Source URLs
- https://api.biorxiv.org/details/medrxiv/ — CSHL details endpoint (medRxiv data)
- https://medrxiv.org/ — Project landing page

## Output
Everything is written into `medrxiv/`:
- `medrxiv_metadata.csv` — one row per preprint (metadata).
- `medrxiv_progress.json` — cursor/resume checkpoint.
- `medrxiv_downloader.log` — run log.

Output format: **CSV**.

## Run
```bash
pip install -r requirements.txt
python medrxiv_downloader.py
```
Useful options:
*   `--start-date YYYY-MM-DD` (defaults to 30 days ago)
*   `--end-date YYYY-MM-DD` (defaults to today)
*   `--limit N` (maximum records to download)
*   `--delay D` (delay in seconds between requests, default: 1.0s)
*   `--fresh` (disregard checkpoint/cache and crawl from scratch)
*   `--output-dir <path>` (overrides default output folder)
*   `--verbose` (enables debug level logging)

## Notes
- Free public API access (no authentication tokens required).
- Resumes incrementally using `medrxiv_progress.json` and de-duplicates with existing rows in `medrxiv_metadata.csv`.
