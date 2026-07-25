#!/usr/bin/env python3
"""Manifest entrypoint for europepmc: download metadata, then merge (best-effort).

  1. europe_pmc_downloader.py  -> europe_pmc/europe_pmc_metadata.csv (required)
  2. merge_europe_pmc.py       -> europe_pmc/europe_pmc_merged_clean.csv (best-effort)

The merge needs europe_pmc_full_text.csv, which only exists when open-access
full texts were extracted. If it's missing, we still publish the metadata CSV.
"""
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def main() -> None:
    print("=== europepmc: running europe_pmc_downloader.py ===", flush=True)
    rc = subprocess.run([sys.executable, "europe_pmc_downloader.py"],
                        cwd=str(BASE_DIR)).returncode
    if rc != 0:
        sys.exit(f"europe_pmc_downloader.py failed (exit {rc})")

    print("=== europepmc: running merge_europe_pmc.py (best-effort) ===", flush=True)
    rc = subprocess.run([sys.executable, "merge_europe_pmc.py"],
                        cwd=str(BASE_DIR)).returncode
    if rc != 0:
        print("merge skipped (no full-text CSV); metadata CSV is still published",
              flush=True)


if __name__ == "__main__":
    main()
