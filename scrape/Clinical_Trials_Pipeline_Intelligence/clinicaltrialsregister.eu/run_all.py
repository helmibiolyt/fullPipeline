#!/usr/bin/env python3
"""Manifest entrypoint for EU CTR: download text pages, then parse to CSV.

  1. eu_ctr_downloader.py            -> eu_ctr_trials/page_*.txt
  2. parse_eu_ctr.py eu_ctr_trials  -> eu_ctr_trials/eu_ctr_all_trials.csv

The raw page_*.txt are intermediate (dropped by collect); only the CSV is published.
"""
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = "eu_ctr_trials"

STEPS = [
    [sys.executable, "eu_ctr_downloader.py"],
    [sys.executable, "parse_eu_ctr.py", OUT_DIR],
]


def main() -> None:
    for step in STEPS:
        print(f"=== eu_ctr: running {' '.join(step[1:])} ===", flush=True)
        rc = subprocess.run(step, cwd=str(BASE_DIR)).returncode
        if rc != 0:
            sys.exit(f"step failed: {' '.join(step)} (exit {rc})")


if __name__ == "__main__":
    main()
