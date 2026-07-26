#!/usr/bin/env python3
"""
pmda_collector.py
=================
A resilient, multi-threaded, and resumable data collector
for Japan's PMDA (Pharmaceuticals and Medical Devices Agency) approvals and review reports.

Features:
  1. Dynamic Web Scraping: Scrapes approval tables across Drugs, Devices,
     Regenerative Products, and Quasi-Drugs from PMDA English portals.
  2. In-Memory PDF Text Extraction: Streams PDF review reports into memory and
     extracts clean text using PyPDF without forcing local file storage.
  3. Raw Document Storage: review-report PDFs are saved as-is and indexed by
     pmda_metadata.csv (with local_pdf_path); no text or LLM extraction.
  5. Checkpoint & Resumption: Saves intermediate progress to support interruption recovery.

Requirements:
    pip install requests beautifulsoup4 pypdf
"""

import os
import re
import sys
import io
import time
import json
import csv
import logging
import argparse
import hashlib
import threading
from pathlib import Path
from urllib.parse import urljoin
import concurrent.futures

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from bs4 import BeautifulSoup

try:
    import pypdf
except ImportError:
    pypdf = None

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

# -- Environment & API Configuration -----------------------------------------
def load_dotenv():
    """Loads environment variables from local .env file if available."""
    env_paths = [
        Path(".env"),
        Path(__file__).parent / ".env",
        Path("c:/Users/LeMonde/Desktop/Biolyt_Inter/Biolyt_data_collection/.env")
    ]
    for env_path in env_paths:
        if env_path.exists():
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            os.environ.setdefault(k.strip(), v.strip())
                break
            except Exception:
                pass

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

BASE_URL = "https://www.pmda.go.jp"

PORTALS = {
    "drug": "https://www.pmda.go.jp/english/review-services/reviews/approved-information/drugs/0001.html",
    "device": "https://www.pmda.go.jp/english/review-services/reviews/approved-information/devices/0003.html",
    "regenerative": "https://www.pmda.go.jp/english/review-services/reviews/approved-information/0004.html",
    "quasi_drug": "https://www.pmda.go.jp/english/review-services/reviews/approved-information/0005.html"
}

MONTH_MAP = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "jun": "06",
    "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12"
}

# CSV output schema fields
checkpoint_lock = threading.Lock()
csv_lock = threading.Lock()

log = logging.getLogger("pmda_collector")

def setup_logging(output_dir: Path):
    """Sets up file and console logging."""
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / "pmda_collector.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] (%(threadName)s) %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout)
        ]
    )

def build_session(max_retries: int) -> requests.Session:
    """Builds requests Session with retries."""
    session = requests.Session()
    retry_strategy = Retry(
        total=max_retries,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": "BiolytInternPMDACollector/2.0 (Python; requests; BeautifulSoup)"
    })
    return session

def parse_approval_date(date_str: str) -> str:
    """Parses date string like 'September 2019' into 'YYYY-MM'."""
    if not date_str:
        return "unknown"
    cleaned = date_str.strip().lower()
    year = None
    for w in cleaned.split():
        w_clean = "".join(c for c in w if c.isdigit())
        if len(w_clean) == 4:
            year = w_clean
            break
    if not year:
        return "unknown"
    month_num = "00"
    for m_name, m_num in MONTH_MAP.items():
        if m_name in cleaned:
            month_num = m_num
            break
    return f"{year}-{month_num}"

# -- Scraping Logic ----------------------------------------------------------
def scrape_portal(category: str, url: str, session: requests.Session) -> list:
    """Scrapes PMDA portal for product approval records and English PDF review report URLs."""
    log.info(f"Scraping {category.upper()} portal: {url}")
    records = []
    try:
        r = session.get(url, timeout=25)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')
        tables = soup.find_all('table', class_='normal-table')
        if not tables:
            log.warning(f"No normal-table elements found on {category} portal.")
            return records

        for table in tables:
            rows = table.find_all('tr')
            if not rows:
                continue

            first_row_links = table.find_all('a')
            if first_row_links and all(a.get('href', '').startswith('#') for a in first_row_links):
                continue

            header_row = rows[0]
            headers = [th.get_text(strip=True).lower() for th in header_row.find_all(['th', 'td'])]

            brand_idx = -1
            generic_idx = -1
            date_idx = -1
            en_idx = -1

            for idx, h in enumerate(headers):
                if "brand" in h:
                    brand_idx = idx
                elif "non-proprietary" in h or "term name" in h or "generic" in h:
                    generic_idx = idx
                elif "approved in" in h or "approved date" in h:
                    date_idx = idx
                elif "english" in h or "report (en)" in h:
                    en_idx = idx

            if brand_idx == -1 and len(headers) >= 4:
                brand_idx, generic_idx, date_idx, en_idx = 0, 1, 2, 3

            if brand_idx == -1:
                continue

            for row in rows[1:]:
                cols = row.find_all(['td', 'th'])
                if len(cols) <= max(brand_idx, generic_idx, date_idx):
                    continue

                brand_cell = cols[brand_idx]
                superscript = brand_cell.find('sup')
                approval_type = "Initial Approval"
                if superscript:
                    sup_text = superscript.get_text(strip=True)
                    if "change" in sup_text.lower() or "partial" in sup_text.lower():
                        approval_type = "Partial Change Approval"
                    superscript.extract()

                brand_name = brand_cell.get_text(" ", strip=True)
                if not brand_name or brand_name.lower() == "brand name":
                    continue

                generic_name = "-"
                if generic_idx != -1 and generic_idx < len(cols):
                    generic_name = cols[generic_idx].get_text(strip=True)

                approval_date_raw = "unknown"
                if date_idx != -1 and date_idx < len(cols):
                    approval_date_raw = cols[date_idx].get_text(strip=True)
                approval_date = parse_approval_date(approval_date_raw)

                en_urls = []
                if en_idx != -1 and en_idx < len(cols):
                    en_links = cols[en_idx].find_all('a')
                    for a in en_links:
                        href = a.get('href')
                        if href and href.lower().endswith('.pdf'):
                            en_urls.append(urljoin(BASE_URL, href))

                records.append({
                    "category": category,
                    "brand_name": brand_name,
                    "approval_type": approval_type,
                    "generic_name": generic_name,
                    "approval_date_raw": approval_date_raw,
                    "approval_date": approval_date,
                    "en_urls": en_urls
                })

        log.info(f"Scraped {len(records)} product records from {category} portal.")
    except Exception as e:
        log.error(f"Error scraping {category} portal: {e}")
    return records

# -- Main Execution Pipeline -------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Resumable, multi-threaded PMDA Japan approvals collector: metadata CSV plus raw review-report PDFs."
    )
    parser.add_argument(
        "--output-dir", default=str(BASE_DIR / "pmda_data"), help="Directory to save output files and metadata."
    )
    parser.add_argument(
        "--threads", type=int, default=5, help="Number of concurrent processing threads."
    )
    parser.add_argument(
        "--max-retries", type=int, default=3, help="Maximum HTTP retries."
    )
    parser.add_argument(
        "--timeout", type=int, default=25, help="Timeout in seconds for HTTP requests."
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Limit number of product approvals to process."
    )
    parser.add_argument(
        "--refresh-all", action="store_true",
        help="Ignore the previous metadata CSV and re-download every document."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Scrape portal records only; do not download PDFs."
    )

    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    setup_logging(output_dir)

    log.info("Starting PMDA Collector (metadata CSV + raw PDFs)...")
    session = build_session(args.max_retries)

    # Phase 1: Scrape Portals
    all_records = []
    for category, portal_url in PORTALS.items():
        records = scrape_portal(category, portal_url, session)
        all_records.extend(records)

    log.info(f"Total PMDA product records scraped across categories: {len(all_records)}")

    # Add unique IDs
    for r in all_records:
        hash_input = f"{r['category']}_{r['brand_name']}_{r['approval_date']}"
        r["id"] = hashlib.md5(hash_input.encode('utf-8')).hexdigest()[:8]

    if args.limit:
        log.info(f"Limiting execution to first {args.limit} records.")
        all_records = all_records[:args.limit]

    if args.dry_run:
        log.info("Dry-run active. Exporting raw scraped metadata...")
        dry_csv = output_dir / "metadata_raw.csv"
        with open(dry_csv, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=list(all_records[0].keys()))
            writer.writeheader()
            writer.writerows(all_records)
        log.info(f"Dry-run completed. Raw metadata saved to {dry_csv}")
        return

    # Phase 2: download PDFs to disk + write metadata CSV. There is no text
    # extraction and no LLM conversion — the vector store handles documents.
    pdfs_dir = output_dir / "pdfs"
    pdfs_dir.mkdir(parents=True, exist_ok=True)
    meta_csv_path = output_dir / "pmda_metadata.csv"
    meta_rows = []

    # Incremental: the pipeline's hydrate step restores the previous
    # pmda_metadata.csv, which records every document already published to S3.
    # Those rows are carried forward untouched so the CSV stays complete, and
    # their PDFs are not re-downloaded (S3 keeps them — this source is additive).
    previous_rows = {}
    if not args.refresh_all and meta_csv_path.exists():
        try:
            with open(meta_csv_path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row.get("id") and row.get("local_pdf_path"):
                        previous_rows[row["id"]] = row
            log.info(f"Incremental mode: {len(previous_rows):,} document(s) already collected; "
                     f"only new records will be downloaded.")
        except Exception as e:
            log.warning(f"Could not read previous {meta_csv_path.name} ({e}); doing a full run.")
            previous_rows = {}

    todo = [r for r in all_records if r["id"] not in previous_rows]
    meta_rows.extend(previous_rows.values())
    log.info(f"{len(all_records):,} record(s) found | {len(previous_rows):,} already held | "
             f"{len(todo):,} to download.")

    raw_pbar = tqdm(total=len(todo), desc="Downloading PMDA PDFs", unit="pdf") if tqdm else None

    def raw_worker(rec):
        pdf_url = rec["en_urls"][0] if rec.get("en_urls") else ""
        local_pdf = ""
        if pdf_url:
            try:
                r = session.get(pdf_url, stream=True, timeout=args.timeout)
                r.raise_for_status()
                pdf_bytes = r.content
                if pdf_bytes.startswith(b"%PDF-"):
                    fname = f"{rec['id']}.pdf"
                    with open(pdfs_dir / fname, "wb") as f:
                        f.write(pdf_bytes)
                    local_pdf = f"pdfs/{fname}"
                else:
                    log.warning(f"[{rec.get('brand_name','?')}] not a PDF: {pdf_url}")
            except Exception as e:
                log.warning(f"Failed to download PDF {pdf_url}: {e}")

        row = {
            "id": rec["id"],
            "category": rec.get("category", ""),
            "brand_name": rec.get("brand_name", ""),
            "generic_name": rec.get("generic_name", ""),
            "approval_type": rec.get("approval_type", ""),
            "approval_date": rec.get("approval_date", ""),
            "approval_date_raw": rec.get("approval_date_raw", ""),
            "source_pdf_url": pdf_url,
            "local_pdf_path": local_pdf,
        }
        with csv_lock:
            meta_rows.append(row)
        if raw_pbar:
            raw_pbar.update(1)

    log.info(f"Launching raw-download pool with {args.threads} threads...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.threads, thread_name_prefix="RawWorker") as executor:
        futures = [executor.submit(raw_worker, rec) for rec in todo]
        concurrent.futures.wait(futures)
    if raw_pbar:
        raw_pbar.close()

    if meta_rows:
        # Carried-forward rows and fresh rows share a schema, but pick the widest
        # to be safe if an older CSV had extra columns.
        fieldnames = max((list(r.keys()) for r in meta_rows), key=len)
        with open(meta_csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(meta_rows)
    new_saved = sum(1 for row in meta_rows
                    if row.get("local_pdf_path") and row["id"] not in previous_rows)
    total_docs = sum(1 for row in meta_rows if row.get("local_pdf_path"))
    log.info(f"Complete: {new_saved} new PDF(s) downloaded to {pdfs_dir}, "
             f"{len(previous_rows)} carried forward, {total_docs} document(s) indexed "
             f"in {meta_csv_path.name}")
    return

if __name__ == "__main__":
    main()
