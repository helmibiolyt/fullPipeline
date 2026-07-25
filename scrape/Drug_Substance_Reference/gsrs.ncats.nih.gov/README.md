# gsrs.ncats.nih.gov — FDA GSRS / UNII Substance Registry

## What it scrapes
Downloads the full FDA Global Substance Registration System (GSRS / UNII) substance
catalog from the NCATS GSRS REST API using `view=full`, flattening each record
(UUID, UNII, preferred name, class, status, CAS number, synonyms) to CSV.

## Source URLs
- https://gsrs.ncats.nih.gov/ginas/app/api/v1/substances — GSRS REST API

## Output
- `gsrs_data/gsrs_substances.csv` — one row per substance (flattened).
- `gsrs_data/gsrs_substances.jsonl` — raw lossless records (intermediate; removed on full completion).

## Run
```
pip install -r requirements.txt
python gsrs_downloader.py
```
Options: `--page-size`, `--threads`, `--limit` (testing), `--no-resume`. Override with `--output-dir`.

## Notes
- Writes only inside this folder (`BASE_DIR/gsrs_data/`).
- Medium source: a slow, paginated crawl over the whole GSRS database (100k+ records) → `size_class: medium`.
- Resume support recovers interrupted runs, but a completed run represents the full catalog
  snapshot (skip 0 → total), so `mirror: true`.
- Output is CSV.
