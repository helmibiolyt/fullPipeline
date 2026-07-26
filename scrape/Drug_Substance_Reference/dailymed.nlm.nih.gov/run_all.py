#!/usr/bin/env python3
"""Manifest entrypoint for DailyMed: run every stage that produces a CSV.

  1. download-mappings --delete-db -> dailymed_master_mapping.csv
                                      dailymed_pharma_mapping.csv
  2. api-fetch-spls                -> dailymed_catalog.csv (+ .jsonl)
  3. api-fetch-details             -> dailymed_details.csv (+ .jsonl)

The manifest previously ran only `api-fetch-spls`, so the catalog was the sole
published CSV and the mapping/detail datasets were missing entirely.

Stage 3 needs a Set ID list; it falls back to reading dailymed_catalog.jsonl,
which stage 2 writes, so the stages must run in this order. `--delete-db` drops
the intermediate SQLite/downloads once the mapping CSVs are exported (collect
would discard them anyway, and they are large).
"""
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

STAGES = [
    ["download-mappings", "--delete-db"],
    ["api-fetch-spls"],
    ["api-fetch-details"],
]


def main() -> None:
    for stage in STAGES:
        print(f"=== dailymed: running {' '.join(stage)} ===", flush=True)
        rc = subprocess.run([sys.executable, "dailymed_downloader.py", *stage],
                            cwd=str(BASE_DIR)).returncode
        if rc != 0:
            sys.exit(f"dailymed {stage[0]} failed (exit {rc})")


if __name__ == "__main__":
    main()
