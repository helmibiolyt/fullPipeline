#!/usr/bin/env python3
"""
Post-processing script: finds all downloaded non-tabular files (PDF, Word, PPT)
across scraper directories and extracts CSVs from them using MiniMax.

Usage:
    python process_files.py <scraper_dir>        # process one scraper
    python process_files.py <scraper_dir> --dry   # just list files, don't process
"""

import sys
import logging
from pathlib import Path

from file_converter import convert_file_to_csv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Extensions that need MiniMax processing
LLM_EXTENSIONS = {".pdf", ".doc", ".docx", ".dotx", ".ppt", ".pptx"}

# Extensions that are already tabular / handled directly — skip
SKIP_EXTENSIONS = {".csv", ".xlsx", ".xls", ".xlsm"}


def find_files_to_process(scraper_dir: Path) -> list[Path]:
    """Find all downloaded files that need MiniMax processing."""
    files = []
    for f in scraper_dir.rglob("*"):
        if not f.is_file():
            continue
        if f.suffix.lower() in LLM_EXTENSIONS:
            files.append(f)
    return sorted(files)


def already_processed(file_path: Path) -> bool:
    """Check if a CSV was already generated from this file by MiniMax."""
    base = file_path.stem
    parent = file_path.parent
    # Check for _table1.csv or just .csv with same base name
    candidates = list(parent.glob(f"{base}*.csv"))
    # Filter out CSVs that were scraped from HTML (not from this file)
    for c in candidates:
        # If there's a CSV with _table suffix or exact match, it was processed
        cname = c.stem
        if cname == base or cname.startswith(f"{base}_table"):
            return True
    return False


def process_scraper(scraper_dir: Path, dry_run: bool = False):
    """DISABLED: doc->CSV conversion is no longer part of the pipeline.

    Raw documents (PDF/Word/PPT) are now uploaded to S3 as-is and handled by the
    downstream graph/vector pipeline (chunk -> embed -> LLM extract). The 12
    scrapers that call this in their main() therefore become no-ops here. Set
    ENABLE_DOC_CONVERSION=1 to re-enable the old MiniMax behaviour.
    """
    import os
    if os.environ.get("ENABLE_DOC_CONVERSION", "0").lower() in ("0", "false", "no", ""):
        log.info("process_scraper is disabled (docs are uploaded raw); skipping %s", scraper_dir)
        return

    files = find_files_to_process(scraper_dir)

    if not files:
        log.info("No files to process in %s", scraper_dir)
        return

    # Split into already done vs pending
    pending = []
    skipped = []
    for f in files:
        if already_processed(f):
            skipped.append(f)
        else:
            pending.append(f)

    log.info("Found %d files total, %d already processed, %d pending",
             len(files), len(skipped), len(pending))

    if dry_run:
        for f in pending:
            print(f"  PENDING: {f.relative_to(scraper_dir)}")
        for f in skipped:
            print(f"  DONE:    {f.relative_to(scraper_dir)}")
        return

    for i, f in enumerate(pending, 1):
        log.info("[%d/%d] Processing: %s", i, len(pending), f.name)
        try:
            data = f.read_bytes()
            out_dir = f.parent
            base_name = f.stem
            results = convert_file_to_csv(data, f.suffix, out_dir, base_name)
            if results:
                for r in results:
                    log.info("  -> %s", r.name)
            else:
                log.warning("  No CSV output for %s", f.name)
        except Exception as e:
            log.error("  Failed: %s — %s", f.name, e)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python process_files.py <scraper_dir> [--dry]")
        sys.exit(1)

    scraper_dir = Path(sys.argv[1])
    if not scraper_dir.is_dir():
        print(f"Not a directory: {scraper_dir}")
        sys.exit(1)

    dry_run = "--dry" in sys.argv
    process_scraper(scraper_dir, dry_run)
