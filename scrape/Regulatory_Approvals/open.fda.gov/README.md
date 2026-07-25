# open.fda.gov — Drugs@FDA (openFDA)

## What it scrapes
FDA-approved drug application metadata from openFDA's Drugs@FDA dataset:
applications, sponsors, products, submissions, application documents, and the
flattened `openfda` enrichment fields (NDCs, RxCUIs, UNIIs, substance names,
pharm classes, etc.). Supports a bulk-ZIP download mode and a multi-threaded
live REST API harvest mode.

## Source URLs
- https://api.fda.gov/drug/drugsfda.json — live Drugs@FDA REST API
- https://api.fda.gov/download.json — bulk partition metadata (latest ZIP resolver)
- https://download.open.fda.gov/drug/drugsfda/ — bulk JSON ZIP archives

## Output
- `openfda_data/openfda_drugs.csv` — one row per product (flattened).
  A raw `.jsonl` and progress JSON are produced during the run and cleaned up,
  leaving CSV.

## Run
```
pip install -r requirements.txt
python openfda_downloader.py download-bulk
```

## Notes
- Output path is BASE_DIR-relative (`BASE_DIR / "openfda_data"`); the
  `-o/--output-dir` CLI override is preserved.
- Uses a required positional mode — bare `python openfda_downloader.py` exits
  non-zero. Use `download-bulk` (recommended), `api-harvest`, or `search`.
- `size_class: heavy` — the bulk archive / full API harvest is large; timeout
  raised to 360 min.
- `mirror: true` — each run re-fetches the full dataset (resumable).
- `tqdm` is an optional progress bar (NON-COMMON dependency).
