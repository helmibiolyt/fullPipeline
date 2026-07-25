# health-products.canada.ca — Health Canada Drug Product Database (DPD)

## What it scrapes
Bulk extracts of Health Canada's Drug Product Database (DPD): drug products,
active ingredients, companies, routes, forms, status, therapeutic class,
schedules, packaging, veterinary species, pharmaceutical standards and
biosimilar flags — for Marketed, Approved, Inactive and Dormant products.
Compiles them into per-type and consolidated master CSV datasets, with an
optional concurrent REST API enrichment mode.

## Source URLs
- https://www.canada.ca/en/health-canada/services/drugs-health-products/drug-products/drug-product-database.html — DPD landing
- https://www.canada.ca/content/dam/hc-sc/documents/services/drug-product-database/ — bulk ZIP archives (allfiles.zip, allfiles_ap.zip, allfiles_ia.zip, allfiles_dr.zip)
- https://health-products.canada.ca/api/drug/ — DPD REST API (api-enrich mode)

## Output
- `canada_dpd_data/` — per-type unified CSVs (`drug.csv`, `ingred.csv`, …) and
  `canada_dpd_master.csv`. Intermediate SQLite DB / JSON / raw text are produced
  and then cleaned up, leaving CSV.

## Run
```
pip install -r requirements.txt
python canada_dpd_downloader.py download-bulk --csv-only
```

## Notes
- Output path is BASE_DIR-relative (`BASE_DIR / "canada_dpd_data"`); the
  `--output-dir` CLI override is preserved.
- Uses a required subcommand — bare `python canada_dpd_downloader.py` prints help
  and exits non-zero. Use `download-bulk` (add `--csv-only` for CSV-only output),
  `api-enrich`, or `search`.
- `mirror: true` — each run re-downloads the full DPD bulk set (resumable via
  HTTP Range).
- `tqdm` is an optional progress bar (NON-COMMON dependency).
