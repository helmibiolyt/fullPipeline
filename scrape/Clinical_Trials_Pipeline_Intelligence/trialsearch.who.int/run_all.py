#!/usr/bin/env python3
"""Manifest entrypoint for WHO ICTRP: download XML, then convert to CSV.

  1. who_collector.py                          -> who_trials_xml/*.xml
  2. who_collector.py --csv-only --csv-dir X   -> who_trials_csv/who_trials.csv

The raw XML is intermediate (dropped by collect); only who_trials.csv is published.
"""
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CSV_DIR = "who_trials_csv"

STEPS = [
    [sys.executable, "who_collector.py"],
    [sys.executable, "who_collector.py", "--csv-only", "--csv-dir", CSV_DIR],
]


def main() -> None:
    for step in STEPS:
        print(f"=== who: running {' '.join(step[1:])} ===", flush=True)
        rc = subprocess.run(step, cwd=str(BASE_DIR)).returncode
        if rc != 0:
            sys.exit(f"step failed: {' '.join(step)} (exit {rc})")


if __name__ == "__main__":
    main()
