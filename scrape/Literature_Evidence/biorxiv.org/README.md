# biorxiv.org

Crawls preprint metadata from bioRxiv using the official Cold Spring Harbor Laboratory (CSHL) bioRxiv API.

## What it scrapes
- **Metadata** for preprints hosted on bioRxiv (doi, title, authors, corresponding author, corresponding author institution, posting date, version, type, license, subject category, JATS XML link, abstract text, funder details, published journal reference, and host server).

## Source URLs
- https://api.biorxiv.org/details/biorxiv/ — CSHL bioRxiv details endpoint
- https://biorxiv.org/ — Project landing page

## Output
Everything is written into `biorxiv/`:
- `biorxiv_metadata.csv` — one row per preprint (metadata).
- `biorxiv_progress.json` — cursor/resume checkpoint.
- `biorxiv_downloader.log` — run log.

Output format: **CSV**.

## Run
```bash
pip install -r requirements.txt
python biorxiv_downloader.py
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
- Resumes incrementally using `biorxiv_progress.json` and de-duplicates with existing rows in `biorxiv_metadata.csv`.
