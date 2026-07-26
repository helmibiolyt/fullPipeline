#!/usr/bin/env python3
"""
MOH Oman Drug Safety Center — Data Scraper
===========================================
Scrapes the 10 resource categories from:
  https://www.moh.gov.om/en/hospitals-directorates/directorates-and-centers-at-hq/drug-safety-center/

For each category:
  - Discovers all available files (PDF / Excel)
  - Downloads all files (clean full scrape each run)
  - Keeps documents exactly as published (no table extraction / no doc->CSV)
  - Writes moh_documents.csv indexing every downloaded file

Usage:
  python moh_scraper.py                    # scrape all categories
  python moh_scraper.py --category "Banned_Adulterated_Products"  # single category
"""

import argparse
import csv
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


try:
    import openpyxl
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL    = "https://www.moh.gov.om"
PAGE_URL    = BASE_URL + "/en/hospitals-directorates/directorates-and-centers-at-hq/drug-safety-center/"
OUTPUT_DIR  = Path(__file__).parent / "moh_data"
REQUEST_DELAY = 1.5
TODAY       = datetime.now().strftime("%Y-%m-%d")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Category mapping: data-description substring → folder name ────────────────
CATEGORIES: Dict[str, str] = {
    "registered pharmaceutical products list with prices":
        "Registered_Pharmaceutical_Products_List_with_Prices",
    "list of banned /adulterated products":
        "Banned_Adulterated_Products",
    "list of registered pharmaceutical manufactures":
        "List_of_Registered_Pharmaceutical_Manufacturers_and_Products",
    "list of herbal companies":
        "List_of_Registered_Herbal_Companies_and_Products",
    "list of registered health products":
        "List_of_Registered_Health_Products",
    "list of registered medicated medical devices":
        "List_of_Registered_Medicated_Medical_Devices",
    "list of licensed medical stores":
        "List_of_Licensed_Medical_Stores",
    "list of bioequivalence centers approved locally":
        "List_of_Bioequivalence_Centers_Approved_Locally_GCC",
    "moh hs code list 2026":
        "MOH_HS_Code_List_2026",
    "medical device supplier approved list":
        "Medical_Device_Supplier_Approved_List",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
}


# ── Utilities ─────────────────────────────────────────────────────────────────

def safe_name(s: str) -> str:
    """Convert a string to a safe filename."""
    s = re.sub(r"[^\w\s-]", "", s).strip()
    s = re.sub(r"[\s]+", "_", s)
    return s[:80]


def download_file(url: str, dest: Path) -> bool:
    """Download a file. Returns True on success."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=120, stream=True)
        r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
        log.info("  Downloaded: %s (%d KB)", dest.name, dest.stat().st_size // 1024)
        return True
    except Exception as e:
        log.error("  Download failed for %s: %s", url, e)
        return False


# ── Resource discovery ────────────────────────────────────────────────────────

def discover_resources() -> Dict[str, List[Dict]]:
    """
    Fetch the MOH page and return a dict:
      {folder_name: [{"filename": ..., "url": ..., "ext": ...}, ...]}
    """
    log.info("Fetching MOH Drug Safety Center page...")
    r = requests.get(PAGE_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    result: Dict[str, List[Dict]] = {}

    for item in soup.select(".resource-item"):
        desc = item.get("data-description", "").lower().strip()

        folder = None
        for key, val in CATEGORIES.items():
            if key in desc:
                folder = val
                break
        if not folder:
            continue

        files = []
        for span in item.select("span.fileURL[data-fileurl]"):
            fname = span.get("data-filename", "").strip()
            furl  = urljoin(BASE_URL, span["data-fileurl"])
            ext   = furl.rsplit(".", 1)[-1].lower() if "." in furl else "bin"
            files.append({"filename": fname, "url": furl, "ext": ext})

        if files:
            if folder not in result:
                result[folder] = []
            result[folder].extend(files)

    log.info("Discovered %d categories with files", len(result))
    return result


# ── Processing ────────────────────────────────────────────────────────────────

def process_file(file_info: Dict, category_dir: Path) -> Optional[Dict]:
    """Download one document and return its catalogue row (None if it failed).

    Documents are kept exactly as published — there is no table extraction and
    no document-to-CSV conversion. The vector store handles the raw files; this
    scraper only records where each one lives.
    """
    fname   = file_info["filename"]
    url     = file_info["url"]
    ext     = file_info["ext"]

    log.info("  Processing: %s", fname)

    # ── Download ─────────────────────────────────────────────────────────────
    dl_dir  = category_dir / "_downloads"
    dl_dir.mkdir(parents=True, exist_ok=True)
    dl_path = dl_dir / f"{safe_name(fname)}.{ext}"

    if not download_file(url, dl_path):
        return None

    time.sleep(REQUEST_DELAY)

    return {
        "category": category_dir.name,
        "filename": fname,
        "ext": ext,
        "source_url": url,
        "local_path": str(dl_path.relative_to(OUTPUT_DIR)).replace("\\", "/"),
        "size_bytes": dl_path.stat().st_size if dl_path.exists() else 0,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="MOH Oman Drug Safety Center scraper")
    parser.add_argument("--category", help="Process only this folder name (e.g. Banned_Adulterated_Products)")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    resources = discover_resources()

    total_files  = 0
    total_updated = 0
    catalogue = []

    for folder, files in resources.items():
        if args.category and folder != args.category:
            continue

        log.info("")
        log.info("=" * 60)
        log.info("Category: %s  (%d file(s))", folder, len(files))
        log.info("=" * 60)

        cat_dir = OUTPUT_DIR / folder
        cat_dir.mkdir(parents=True, exist_ok=True)

        for finfo in files:
            total_files += 1
            log.info("  File: %s", finfo["filename"])
            row = process_file(finfo, cat_dir)
            if row:
                catalogue.append(row)
                total_updated += 1

    # Index of every document fetched, so the run publishes CSV alongside docs.
    if catalogue:
        cat_csv = OUTPUT_DIR / "moh_documents.csv"
        with open(cat_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(catalogue[0].keys()))
            writer.writeheader()
            writer.writerows(catalogue)
        log.info("Document catalogue written: %s (%d rows)", cat_csv, len(catalogue))

    log.info("")
    log.info("=" * 60)
    log.info("Done — %d files checked, %d downloaded", total_files, total_updated)
    log.info("Output: %s", OUTPUT_DIR)
    log.info("=" * 60)
if __name__ == "__main__":
    main()
