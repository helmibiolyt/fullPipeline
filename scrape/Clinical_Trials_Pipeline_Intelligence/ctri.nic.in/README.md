# ctri.nic.in

Clinical Trials Registry - India (CTRI).

## What it scrapes
Fetches CTRI trial detail pages directly by base64-encoding sequential database IDs
(bypassing the search form's CAPTCHA), using a concurrent downloader + streaming
parser. Raw HTML is parsed with BeautifulSoup into structured records, flattened,
and written to CSV. HTML caches are deleted as they are parsed; invalid IDs are
tracked to avoid refetching.

## Source URLs
- https://ctri.nic.in/Clinicaltrials/pmaindet2.php — trial detail page (accessed via `?EncHid=<base64 id>`)

## Output
- `ctri_trials/ctri_trials.csv` — one row per trial (flattened, analysis-ready).
- `ctri_trials/html/` — transient per-trial HTML cache (skipped/deleted during streaming).
- `ctri_trials/invalid_ids.txt`, `ctri_trials/failed_ids.txt` — resume/bookkeeping files.

## Run
This entrypoint is subcommand-based (`download` / `parse`):
```
pip install -r requirements.txt
python ctri_downloader.py download --start 1 --end 140300 --workers 10
python ctri_downloader.py parse            # recompile cached HTML into CSV
```

## Notes
- Writes only inside this folder (`BASE_DIR/ctri_trials/`).
- Incremental / append-only: `download` skips already-completed and invalid IDs and
  appends new rows to the existing CSV (mirror: false). It does not re-fetch records
  already collected, so the pipeline should not wipe the folder between runs.
- Heavy: the default ID range is 1–140,300, i.e. a very long HTTP crawl.
