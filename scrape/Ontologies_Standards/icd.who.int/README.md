# icd.who.int

Crawls and compiles the WHO **ICD-10 (2019)** and **ICD-11 (2026-01)** disease classifications into
structured CSVs.

## What it scrapes
- ICD-10: walks the WHO ICD-10 browser JSON/HTML API chapter-by-chapter and block-by-block, with
  block-level checkpointing, extracting codes, titles, inclusions/exclusions/notes and hierarchy.
- ICD-11: downloads the official "Simple Tabulation" ZIP from the WHO CDN and flattens its
  tab-delimited file to CSV.

## Source URLs
- https://icd.who.int/browse10/2019/en — ICD-10 browser API
- https://icdcdn.who.int/static/releasefiles/2026-01/SimpleTabulation-ICD-11-MMS-en.zip — ICD-11 ZIP

## Output
Written under `icd_data/`:
- `icd10_chapters.csv`, `icd10_blocks.csv`, `icd10_codes.csv`
- `icd11_codes.csv`
- (transient `icd_data/temp_blocks/` + `progress_icd10.json` checkpoints during crawling)

## Run
```
pip install -r requirements.txt
python who_icd_downloader.py
```
A `--output-dir` / `-o` override is still accepted; the default resolves to `BASE_DIR/icd_data`.

## Notes
- Writes only inside this folder (`BASE_DIR/icd_data/`).
- Final output is **CSV**. The ICD-10 crawler checkpoints for resume, but a full run compiles fresh
  CSVs from all blocks and cleans up temp files → full-snapshot behaviour, `mirror: true`.
- `size_class: medium` — the ICD-10 crawl visits ~260 blocks with a 1.5s polite delay (slow API crawl).
- No secrets required.
