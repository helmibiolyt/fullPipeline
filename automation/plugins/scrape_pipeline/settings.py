"""Central configuration, all overridable via environment variables.

In docker-compose these are set in the `x-airflow-common` env block so every
worker sees the same values.
"""
import os
from pathlib import Path

# Root of the local scrape/ tree that holds the per-source scraper folders.
SCRAPE_ROOT = Path(os.environ.get("SCRAPE_ROOT", "/opt/scrape"))

# Where per-run staging snapshots (the "local temporary backup") are written.
RUN_ROOT = Path(os.environ.get("RUN_ROOT", "/opt/automation/data/runs"))

# Per-source manifest file name. Each scrape/<Topic>/<source>/ folder carries one;
# the pipeline auto-discovers scrapers by scanning for these. This keeps the whole
# scraper definition (code + config) inside the scrape/ folder a contributor owns.
MANIFEST_NAME = os.environ.get("MANIFEST_NAME", "manifest.yaml")

# S3 target.
S3_BUCKET = os.environ.get("S3_BUCKET", "moine-data")
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

# Immutable per-run backups live under this sub-prefix of each source.
RUNS_PREFIX = "_runs"
# Pointer object at the source root naming the current good run.
LATEST_KEY = "_LATEST.json"

# How many immutable runs to retain per source in S3 (rollback history).
# 0 = keep only the live CSVs; the per-run staging copy is deleted right after a
# successful commit (temporary copy, dropped on success). Raise to keep history.
DEFAULT_RETENTION_RUNS = int(os.environ.get("RETENTION_RUNS", "0"))

# How many local run snapshots to keep on disk after a successful commit.
# 0 = delete the local copy as soon as S3 has it (the default). S3 `_runs/`
# is the durable backup, so local data is purely temporary staging.
KEEP_LOCAL_RUNS = int(os.environ.get("KEEP_LOCAL_RUNS", "0"))

# After a successful commit, also delete the scraper's OWN downloaded data
# (keep code/manifest). Total scrape data exceeds local disk, so S3 is the sole
# durable copy. 0 = keep scraper folders (more disk, enables cross-run resume).
WIPE_SCRAPER_DIR = os.environ.get("WIPE_SCRAPER_DIR", "1").lower() not in ("0", "false", "no")

# Mirror-mode safety guard. A full-replace commit refuses to delete stale live
# data if the new run has fewer than this fraction of the currently-live file
# count OR total bytes — i.e. a partial/failed scrape can never wipe good data.
# Set to 0 to disable the guard.
MIN_COMPLETENESS_RATIO = float(os.environ.get("MIN_COMPLETENESS_RATIO", "0.5"))

# Published to S3: CSV (structured) + raw documents (unstructured — handled
# downstream by the graph/vector pipeline: chunk -> embed -> extract). Docs are
# NO LONGER converted to CSV here. Spreadsheets are converted to CSV (structured).
CSV_SUFFIXES = {".csv"}
DOC_SUFFIXES = {".pdf", ".doc", ".docx", ".dotx", ".ppt", ".pptx"}
SPREADSHEET_SUFFIXES = {".xlsx", ".xls", ".xlsm"}
TSV_SUFFIXES = {".tsv", ".tab"}
JSONL_SUFFIXES = {".jsonl", ".ndjson"}   # line-delimited JSON = tabular records
# Tabular formats converted to CSV before publishing.
TABULAR_SUFFIXES = SPREADSHEET_SUFFIXES | TSV_SUFFIXES | JSONL_SUFFIXES
# Everything published (kept as-is in the run dir): CSV + raw docs.
PUBLISH_SUFFIXES = CSV_SUFFIXES | DOC_SUFFIXES
# Structured formats that are DROPPED (scrapers parse these into CSV themselves).
# Logged at collect time so a dropped primary output is never silent.
STRUCTURED_DROPPED_SUFFIXES = {".json", ".xml", ".parquet"}
# Convert spreadsheets/TSV to CSV in-pipeline (1/true) or ignore them (0/false).
CONVERT_XLSX = os.environ.get("CONVERT_XLSX", "1").lower() not in ("0", "false", "no")
ARTIFACT_EXCLUDE_DIRS = {"__pycache__", ".claude", ".git"}
