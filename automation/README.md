# Scraping Pipeline — Airflow orchestration

Orchestrates the 14 source scrapers under `../scrape/` and publishes each
source's data to the `moine-data` S3 bucket **without ever deleting live data
before the replacement is verified**.

## Design at a glance

- **One DAG** (`scrapers_pipeline`), **one TaskGroup ("node") per scraper**.
  Collapsed, the graph shows 14 boxes; expanded, each is the per-source flow.
- Sources are declared in [`config/sources.yaml`](config/sources.yaml). Adding a
  scraper = one YAML entry, no DAG code change.
- Reusable logic lives in [`plugins/scrape_pipeline/`](plugins/scrape_pipeline).

### Per-source flow

```
scrape -> collect -> validate_local -> upload_run -> verify_run -> commit -> prune_runs
                                                                       \--> cleanup_local
                                             \--------------------> rollback_staging (on failure)
```

Published to S3: **CSV (structured) + raw documents (PDF/Word/PPT)**. Documents are
NOT converted to CSV here — they are uploaded as-is and handled downstream by the
graph/vector pipeline (chunk -> embed -> LLM extract). Spreadsheets (xlsx) are
converted to CSV during `collect` (local pandas). json/logs/code are ignored.

| Step | What it does |
|------|--------------|
| `scrape` | Runs the source scraper (cwd = its folder). (Doc->CSV conversion via `process_files` is disabled — see `scrape/process_files.py`; set `ENABLE_DOC_CONVERSION=1` to restore.) |
| `collect` | Snapshots CSV + raw docs (12/14 write next to their script) into an isolated run dir — the local temporary backup — converting any xlsx/xls to CSV on the way. |
| `validate_local` | Fails if no files / any zero-byte file. Builds `_MANIFEST.json` (sha256 + size per file). |
| `upload_run` | Uploads the run to `s3://moine-data/<topic>/<source>/_runs/<run_id>/` (immutable). |
| `verify_run` | Re-lists S3 and checks every object's presence + size against the manifest. |
| `commit` | Copies the verified run into the **live** flat path, deletes stale live objects, then flips `_LATEST.json`. This is the only step that mutates live data. |
| `prune_runs` | Keeps the last `RETENTION_RUNS` immutable runs in S3 (rollback history). |
| `cleanup_local` | **Deletes the local run snapshot** — but only on the success path. S3 `_runs/` is the durable backup, so local data is purely temporary staging. A failed run keeps its local copy for retry/inspection without re-scraping. Set `KEEP_LOCAL_RUNS>0` to retain the last N locally. |
| `rollback_staging` | `trigger_rule=one_failed`: deletes only this run's half-written `_runs/<run_id>/`. **Never touches live data or the pointer.** |

### S3 layout per source

```
<topic>/<source>/<relpath>...        live view consumers read (unchanged contract)
<topic>/<source>/_runs/<run_id>/data/...   immutable verified backups (last N kept)
<topic>/<source>/_LATEST.json        pointer to the current good run
```

Live data is only touched in `commit`, after verification passes, and every
committed run is retained under `_runs/`, so an interrupted commit is always
recoverable. Rollback = re-point/re-sync from the previous `_runs/<run_id>/`.

## Run it (Docker)

```bash
cd automation
cp .env.example .env          # set AIRFLOW_UID=$(id -u) on Linux
docker compose build
docker compose up airflow-init
docker compose up -d
# http://localhost:8080  (airflow / airflow) — unpause "scrapers_pipeline"
```

S3 auth uses the host `~/.aws` `moine` profile (mounted read-only,
`AWS_PROFILE=moine`).

## Test one source without Airflow

```bash
export SCRAPE_ROOT=../scrape RUN_ROOT=./data/runs \
       SOURCES_REGISTRY=./config/sources.yaml S3_BUCKET=moine-data AWS_PROFILE=moine
# local only (no S3):
PYTHONPATH=plugins python run_source.py uniprot_org --no-s3
# reuse existing output, then push to S3:
PYTHONPATH=plugins python run_source.py uniprot_org --skip-scrape
```

## Retry / safety knobs

- Scrape/upload/commit: `retries=3`, exponential backoff (5 → 30 min).
- `validate_local`: `retries=0` (fail fast on bad data).
- `max_active_runs=1` per DAG (no overlapping scrapes of a source).
- Pools by size class: `scrapers_small` (4), `scrapers_medium` (3),
  `scrapers_heavy` (2) — created by `airflow-init`.
- Retention: `RETENTION_RUNS` env (default 3).
