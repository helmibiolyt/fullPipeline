#!/usr/bin/env python3
"""
NCBI ClinVar bulk data scraper — downloads TSV files from the ClinVar FTP,
decompresses .gz archives, and converts all TSV files to CSV.
"""

import csv
import gzip
import io
import logging
import sys
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent

FTP_BASE = "https://ftp.ncbi.nlm.nih.gov/pub/clinvar/tab_delimited"

# (url, destination folder, output csv name)
DOWNLOADS = [
    (
        f"{FTP_BASE}/variant_summary.txt.gz",
        "Variants",
        "variant_summary.csv",
    ),
    (
        f"{FTP_BASE}/gene_specific_summary.txt",
        "Gene_Summaries",
        "gene_specific_summary.csv",
    ),
    (
        f"{FTP_BASE}/submission_summary.txt.gz",
        "Submissions",
        "submission_summary.csv",
    ),
    (
        f"{FTP_BASE}/var_citations.txt",
        "Citations",
        "var_citations.csv",
    ),
    (
        f"{FTP_BASE}/cross_references.txt",
        "Cross_References",
        "cross_references.csv",
    ),
    (
        f"{FTP_BASE}/summary_of_conflicting_interpretations.txt",
        "Conflicting_Interpretations",
        "summary_of_conflicting_interpretations.csv",
    ),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

MAX_RETRIES = 3
RETRY_BACKOFF = 2

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log = logging.getLogger("clinvar_scraper")
log.setLevel(logging.DEBUG)
fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
if not log.handlers:
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(sh)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _download_stream(url: str, dest: Path) -> None:
    """Stream-download a file to disk with retries."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with requests.get(url, headers=HEADERS, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        f.write(chunk)
            log.info("Downloaded %s (%s bytes)", dest.name, dest.stat().st_size)
            return
        except Exception as exc:
            log.warning("Attempt %d/%d failed for %s: %s", attempt, MAX_RETRIES, url, exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
    raise RuntimeError(f"Failed to download {url} after {MAX_RETRIES} attempts")


def _decompress_gz(gz_path: Path, out_path: Path) -> None:
    """Decompress a .gz file, streaming to avoid loading into memory."""
    with gzip.open(gz_path, "rb") as f_in, open(out_path, "wb") as f_out:
        while True:
            chunk = f_in.read(1024 * 1024)
            if not chunk:
                break
            f_out.write(chunk)
    log.info("Decompressed %s -> %s", gz_path.name, out_path.name)


def _tsv_to_csv(tsv_path: Path, csv_path: Path) -> None:
    """Convert a TSV file to CSV, streaming line by line.

    Skips comment lines starting with '#' and any metadata preamble lines
    (non-tab-separated text) that precede the actual header row.
    """
    csv.field_size_limit(10 * 1024 * 1024)
    header_found = False
    with open(tsv_path, "r", encoding="utf-8", errors="replace") as fin, \
         open(csv_path, "w", newline="", encoding="utf-8") as fout:
        reader = csv.reader(fin, delimiter="\t")
        writer = csv.writer(fout)
        for row in reader:
            # Always skip comment lines starting with '#'
            if row and row[0].startswith("#"):
                continue
            # Before the header is found, skip metadata preamble lines
            # (lines with only a single field, i.e. not tab-separated data)
            if not header_found:
                if len(row) <= 1:
                    continue
                header_found = True
            writer.writerow(row)
    log.info("Converted TSV -> CSV: %s", csv_path.name)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    log.info("=== NCBI ClinVar Bulk Data Scraper ===")

    for url, folder_name, csv_name in DOWNLOADS:
        folder = BASE_DIR / folder_name
        folder.mkdir(parents=True, exist_ok=True)

        filename = url.rsplit("/", 1)[-1]
        raw_path = folder / filename
        is_gz = filename.endswith(".gz")

        # Download
        log.info("Downloading %s ...", url)
        _download_stream(url, raw_path)

        # Decompress if needed
        if is_gz:
            tsv_path = folder / filename.replace(".gz", "")
            _decompress_gz(raw_path, tsv_path)
        else:
            tsv_path = raw_path

        # Convert TSV to CSV
        csv_path = folder / csv_name
        _tsv_to_csv(tsv_path, csv_path)

        log.info("Done: %s", folder_name)

        time.sleep(1)  # Be nice to NCBI FTP

    # Post-processing
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from process_files import process_scraper
    log.info("Post-processing downloaded files with MiniMax...")
    process_scraper(BASE_DIR)

    log.info("=== ClinVar scraper finished ===")


if __name__ == "__main__":
    main()
