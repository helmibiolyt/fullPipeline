# dailymed.nlm.nih.gov — DailyMed Structured Product Labeling (SPL)

## What it scrapes
Collects DailyMed drug/substance label data. Downloads the NLM bulk mapping ZIPs,
compiles them into a local indexed SQLite database, and exports joined master CSVs
of drug-to-substance (RxNorm) and pharmacologic-class mappings. Additional modes
harvest the active SPL catalog and enrich Set IDs with NDC / packaging / media detail
via the DailyMed REST API.

## Source URLs
- https://dailymed-data.nlm.nih.gov/public-release-files/ — bulk mapping ZIP files
- https://dailymed.nlm.nih.gov/dailymed/services/v2 — DailyMed REST API

## Output
- `dailymed_data/dailymed_master_mapping.csv` — SPL ↔ RxNorm joined mapping.
- `dailymed_data/dailymed_pharma_mapping.csv` — SPL ↔ pharmacologic-class mapping.
- `dailymed_data/dailymed_details.csv` — NDC / packaging / media per Set ID (api-fetch-details).
- `dailymed_data/dailymed.db`, `dailymed_data/downloads/` — intermediate SQLite DB + raw ZIPs.

## Run
```
pip install -r requirements.txt
python dailymed_downloader.py download-mappings     # bulk build + master CSVs
```
Other subcommands: `search`, `api-fetch-spls`, `api-fetch-details`. Override with `--output-dir`.

## Notes
- Writes only inside this folder (`BASE_DIR/dailymed_data/`).
- Heavy source: bulk mapping ZIPs + SQLite compilation over the full SPL catalog → `size_class: heavy`.
- The primary `download-mappings` mode rebuilds the full snapshot each run → `mirror: true`.
- `tqdm` is an optional (but imported) progress-bar dependency — see NON-COMMON DEPS.
