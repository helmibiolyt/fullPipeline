# loinc.org

Downloads / crawls the **Logical Observation Identifiers Names and Codes (LOINC)** medical
ontology and writes structured CSVs.

## What it scrapes
Two modes (subcommands):
- `crawl` — discovers LOINC codes via NLM's public Clinical Table Search API, then scrapes each
  public `loinc.org/<code>/` detail page (component, property, system, class, units, synonyms, ...).
- `bulk-download` — Playwright-assisted authenticated download of the complete LOINC package ZIP
  from loinc.org, extracted and reduced to core reference CSVs.

## Source URLs
- https://loinc.org/ — code detail pages and the complete-download package
- https://clinicaltables.nlm.nih.gov/api/loinc_items/v3/search — NLM public code-discovery API

## Output
Written under `loinc_data/`:
- `loinc_crawled_codes.csv` — one row per crawled LOINC code (crawl mode)
- `loinc_progress.json` — resume/progress checkpoint (crawl mode)
- `loinc_table.csv`, `loinc_parts.csv` + extracted package files (bulk-download mode)

## Run
```
pip install -r requirements.txt
python loinc_downloader.py crawl            # public detail-page crawl (no auth)
python loinc_downloader.py bulk-download     # authenticated full-package download
```
A `--output-dir` override is still accepted; the default resolves to `BASE_DIR/loinc_data`.

## Notes
- Writes only inside this folder (`BASE_DIR/loinc_data/`).
- `mirror: false` — crawl mode is incremental: it tracks progress in `loinc_progress.json` and
  appends only newly-crawled codes to the CSV, resuming rather than re-fetching completed codes.
- `bulk-download` needs **playwright** (headful browser; loinc.org uses a Wordfence captcha) and
  loinc.org credentials via `LOINC_USERNAME` / `LOINC_PASSWORD` env vars.
- Reads credentials from environment variables (`.env` supported via python-dotenv).
- NON-COMMON deps: `python-dotenv`, `playwright` (flagged for automation/requirements.txt).
