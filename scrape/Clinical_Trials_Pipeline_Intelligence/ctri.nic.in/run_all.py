#!/usr/bin/env python3
"""Manifest entrypoint for CTRI: download trial HTML, then parse to CSV.

  1. ctri_downloader.py download   -> ctri_trials/html/*.html
  2. ctri_downloader.py parse      -> ctri_trials/ctri_trials.csv (+ .jsonl)

Defaults are consistent (BASE_DIR/ctri_trials), so no args needed. The raw HTML
is intermediate (dropped by collect); only the CSV is published.
"""
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def main() -> None:
    for cmd in ("download", "parse"):
        print(f"=== ctri: running {cmd} ===", flush=True)
        rc = subprocess.run([sys.executable, "ctri_downloader.py", cmd],
                            cwd=str(BASE_DIR)).returncode
        if rc != 0:
            sys.exit(f"ctri {cmd} failed (exit {rc})")


if __name__ == "__main__":
    main()
