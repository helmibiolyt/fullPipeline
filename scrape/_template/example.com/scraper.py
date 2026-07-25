#!/usr/bin/env python3
"""Template scraper. Copy this folder to scrape/<Topic>/<source>/ and edit.

Contract (see ../../CONTRIBUTING.md):
  - runnable as `python scraper.py`
  - resolve your own path via BASE_DIR (never rely on the current directory)
  - write CSV only, inside your own folder
  - exit non-zero on failure
  - re-fetch the full dataset each run (so mirror mode is correct)
"""
import csv
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def fetch_rows():
    """Replace with your scraping logic. Return a list of dict rows."""
    return [{"id": 1, "name": "example"}]


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    rows = fetch_rows()
    if not rows:
        sys.exit("no data scraped")          # non-zero exit -> pipeline marks it failed
    write_csv(BASE_DIR / "MyCategory" / "my_dataset.csv", rows)
    # You may also download .xlsx (auto-converted) or .pdf/.doc (converted via
    # MiniMax by the pipeline) into BASE_DIR — the data must end up as CSV.


if __name__ == "__main__":
    main()
