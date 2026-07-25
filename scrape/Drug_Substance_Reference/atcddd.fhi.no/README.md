# atcddd.fhi.no — WHO ATC/DDD Database

## What it scrapes
Recursively crawls the WHO Collaborating Centre for Drug Statistics Methodology
site to extract the complete Anatomical Therapeutic Chemical (ATC) classification
hierarchy (Levels 1–4) and the Defined Daily Dose (DDD) substance table (Level 5).

## Source URLs
- https://atcddd.fhi.no/atc_ddd_index/ — ATC/DDD index (crawled by `?code=` per branch)

## Output
- `atc_ddd_data/atc_classes.csv` — one row per class (Levels 1–4).
- `atc_ddd_data/atc_substances.csv` — one row per Level 5 substance with DDD.
- `atc_ddd_data/atc_ddd_full.csv` — unified/denormalized classes + substances.

## Run
```
pip install -r requirements.txt
python atc_ddd_downloader.py
```
Override the output location with `--output-dir`; limit a test crawl with `--limit-branch V`.

## Notes
- Writes only inside this folder (`BASE_DIR/atc_ddd_data/`).
- Re-fetches the full hierarchy each run (idempotent snapshot) → `mirror: true`.
- Output is CSV. `pandas` is used only for an optional inline preview of the results.
