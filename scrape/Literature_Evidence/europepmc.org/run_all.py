#!/usr/bin/env python3
"""Manifest entrypoint for europepmc: metadata + open-access full text, then merge.

  1. europe_pmc_downloader.py --extract-fulltext
         -> europe_pmc/europe_pmc_metadata.csv  (required)
         -> europe_pmc/europe_pmc_full_text.csv (cleaned JATS text, no LLM)
  2. merge_europe_pmc.py
         -> europe_pmc/europe_pmc_merged_clean.csv (best-effort)

--extract-fulltext used to be omitted here, so the full-text CSV was never
written and the merge silently "skipped" every run — leaving only the small
metadata CSV published. The text now comes straight from the JATS XML parser;
no LLM is involved.
"""
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def main() -> None:
    print("=== europepmc: running europe_pmc_downloader.py ===", flush=True)
    rc = subprocess.run([sys.executable, "europe_pmc_downloader.py", "--extract-fulltext"],
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
