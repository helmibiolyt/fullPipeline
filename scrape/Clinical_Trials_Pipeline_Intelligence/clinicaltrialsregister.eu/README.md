# clinicaltrialsregister.eu

EU Clinical Trials Register (EudraCT).

## What it scrapes
Auto-detects the total number of search-result pages, then downloads each page's
"full details" text export directly over HTTP (with retries/backoff and polite
delays). Downloaded page text files are merged and parsed line-by-line into
structured trial records, which are written to a consolidated CSV. Raw text files
are cleaned up after conversion.

## Source URLs
- https://www.clinicaltrialsregister.eu/ctr-search/search — search results (page count)
- https://www.clinicaltrialsregister.eu/ctr-search/rest/download/full — per-page full-detail text export

## Output
- `eu_ctr_trials/eu_ctr_all_trials.csv` — one row per trial (parsed from the EudraCT text export).
- `eu_ctr_trials/page_*.txt`, `eu_ctr_trials/eu_ctr_all_trials.txt` — transient raw text (deleted after CSV conversion unless `--no-cleanup`).

## Run
```
pip install -r requirements.txt
python eu_ctr_downloader.py                 # all pages, then merge + convert to CSV
python eu_ctr_downloader.py --csv-only      # only re-convert existing txt files
```

## Notes
- Writes only inside this folder (`BASE_DIR/eu_ctr_trials/`).
- Full snapshot each run: page-file resume (skip existing) is an optimization, but the
  CSV is fully rebuilt from all parsed trials (mirror: true).
- `tqdm` is imported for progress bars but is optional (the code falls back to a no-op
  if it is not installed).
