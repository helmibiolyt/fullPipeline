#!/usr/bin/env python3
"""Manifest entrypoint for CTRI: download trial pages straight into the CSV.

  1. ctri_downloader.py download -> ctri_trials/ctri_trials.csv (+ .jsonl)

`download` streams each page through a consumer thread that parses it and
APPENDS to the CSV, deleting the HTML immediately to save space.

Do NOT chain `parse` after it. `parse` reopens the same CSV in "w" mode and
rebuilds it from ctri_trials/html/, which `download` has just emptied — so it
truncated a full crawl down to the handful of pages still in flight at the end
(the published CSV held 119 rows instead of ~50k). `parse` remains available as
a standalone command for re-parsing a preserved HTML cache.
"""
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def main() -> None:
    for cmd in ("download",):
        print(f"=== ctri: running {cmd} ===", flush=True)
        rc = subprocess.run([sys.executable, "ctri_downloader.py", cmd],
                            cwd=str(BASE_DIR)).returncode
        if rc != 0:
            sys.exit(f"ctri {cmd} failed (exit {rc})")


if __name__ == "__main__":
    main()
