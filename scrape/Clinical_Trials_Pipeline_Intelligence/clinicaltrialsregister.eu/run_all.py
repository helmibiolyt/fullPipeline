#!/usr/bin/env python3
"""Manifest entrypoint for EU CTR: download text pages, then parse to CSV.

  1. eu_ctr_downloader.py            -> eu_ctr_trials/page_*.txt, then merges
                                        them, writes eu_ctr_all_trials.csv and
                                        DELETES the pages it just consumed
  2. parse_eu_ctr.py eu_ctr_trials  -> only when step 1 left pages behind

The raw page_*.txt are intermediate (dropped by collect); only the CSV is
published.

Step 2 is conditional, and that is the whole point of this file's history.
eu_ctr_downloader.py already ends in convert_to_csv_and_cleanup(cleanup=True):
it writes the CSV itself and removes every page_*.txt. parse_eu_ctr.py then ran
unconditionally, found no pages, and exited 1 with

    no page_*.txt found in .../eu_ctr_trials

which failed the Airflow task AFTER a complete twelve-hour download. With
retries: 3 that is four full downloads of a live public registry, each one
discarding a correct 2.2 GB CSV over a step that had nothing left to do. It ran
twice before anyone noticed.

So the parser runs only if there is something for it to parse. It is kept
rather than deleted because it is the fallback for a downloader that exits
before its own conversion stage - then the pages ARE on disk and this rebuilds
the CSV from them.
"""
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = "eu_ctr_trials"
CSV_NAME = "eu_ctr_all_trials.csv"


def run(step) -> None:
    print(f"=== eu_ctr: running {' '.join(step[1:])} ===", flush=True)
    rc = subprocess.run(step, cwd=str(BASE_DIR)).returncode
    if rc != 0:
        sys.exit(f"step failed: {' '.join(step)} (exit {rc})")


def main() -> None:
    out = BASE_DIR / OUT_DIR
    run([sys.executable, "eu_ctr_downloader.py"])

    pages = sorted(out.glob("page_*.txt"))
    csv_path = out / CSV_NAME
    if pages:
        run([sys.executable, "parse_eu_ctr.py", OUT_DIR])
    elif csv_path.exists() and csv_path.stat().st_size > 0:
        print(f"=== eu_ctr: no pages left; downloader already wrote "
              f"{CSV_NAME} ({csv_path.stat().st_size / 1048576:.0f} MB) ===",
              flush=True)
    else:
        sys.exit("eu_ctr: downloader left neither pages nor a CSV")


if __name__ == "__main__":
    main()
