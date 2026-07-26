#!/usr/bin/env python3
"""
mhra_downloader.py
==================
An intelligent, resilient, multi-threaded, and resumable data collector for
UK Medicines and Healthcare products Regulatory Agency (MHRA) drug approvals.

Features:
  1. Direct Azure Search Integration: Bypasses slow web scraping, HTML parsing,
     and 503 gateway proxy errors by querying their underlying Azure AI Search
     service directly.
  2. Raw Document Storage: PDFs are streamed and saved to disk as-is. There is
     no text extraction and no LLM conversion in this scraper — the vector store
     handles documents downstream.
  3. Structured CSV Generation: Exports per-document metadata (including the
     local PDF path, which links each document back to its record) to
     mhra_documents.csv, plus raw_metadata.csv.
  4. Optional Local PDF Download: Allows saving local PDF files when --download-pdfs is specified.
  5. Resumable Checkpoint System: Progress is recorded in a thread-safe JSON checkpoint.

Requirements:
    pip install requests
    (Optional) pip install tqdm
"""

import os
import sys
import time
import json
import csv
import logging
import argparse
import hashlib
import threading
import re
import io
from pathlib import Path
from urllib.parse import urlparse
import concurrent.futures
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

# Optional progress indicator
try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

# Load .env file automatically
try:
    import dotenv
    dotenv.load_dotenv()
except ImportError:
    env_file = Path(".env")
    if env_file.exists():
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

# -- Configuration & Constants -----------------------------------------------
BASE_DIR = Path(__file__).resolve().parent

SEARCH_ENDPOINT = "https://mhraproducts4853.search.windows.net/indexes/products-index/docs"
API_KEY = "17CCFC430C1A78A169B392A35A99C49D"
API_VERSION = "2017-11-11"

ALL_CSV_FIELDS = [
    "id", "product_name", "doc_type", "pl_number", "substance_name",
    "title", "created", "metadata_storage_size", "original_filename",
    "azure_blob_url", "local_pdf_path",
    "therapeutic_indication", "posology_and_administration",
    "active_substances_detailed", "contraindications",
    "adverse_effects_summary", "storage_conditions",
    "marketing_authorisation_holder", "approval_or_revision_date"
]

# -- Threading and Lock Controls ---------------------------------------------
checkpoint_lock = threading.Lock()
csv_lock = threading.Lock()
log = logging.getLogger("mhra_downloader")

# -- Logging Setup -----------------------------------------------------------
def setup_logging(output_dir: Path):
    """Sets up file and console logging."""
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / "mhra_downloader.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] (%(threadName)s) %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout)
        ]
    )

# -- HTTP Session Builder ----------------------------------------------------
def build_session(max_retries: int) -> requests.Session:
    """Builds a requests.Session with retries and custom headers."""
    session = requests.Session()
    retry_strategy = Retry(
        total=max_retries,
        backoff_factor=2.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })
    return session

# -- Metadata Harvester ------------------------------------------------------
def harvest_metadata(session: requests.Session, limit: int = None) -> list:
    """
    Harvests the complete metadata of all documents from the Azure search index.
    Paginates using $skip and $top.
    """
    log.info("Starting Phase 1: Metadata Harvesting from Azure AI Search...")
    all_records = []
    skip = 0
    top = 1000
    total_count = None
    
    headers = {
        "api-key": API_KEY
    }
    
    while True:
        params = {
            "api-version": API_VERSION,
            "search": "*",
            "$top": str(top),
            "$skip": str(skip),
            "$count": "true"
        }
        
        log.info(f"Querying Azure index: skip={skip}, top={top}...")
        try:
            r = session.get(SEARCH_ENDPOINT, params=params, headers=headers, timeout=20)
            if r.status_code != 200:
                log.error(f"Azure Search API returned HTTP {r.status_code}: {r.text[:500]}")
                break
                
            data = r.json()
            
            if total_count is None:
                total_count = data.get("@odata.count", 0)
                log.info(f"Total documents reported in Azure index: {total_count:,}")
                
            values = data.get("value", [])
            if not values:
                log.info("No more documents returned from search index. Stopping.")
                break
                
            all_records.extend(values)
            log.info(f"Retrieved {len(values)} records. Total harvested so far: {len(all_records):,}")
            
            if limit and len(all_records) >= limit:
                all_records = all_records[:limit]
                log.info(f"Harvest limit of {limit} reached.")
                break
                
            if len(values) < top:
                log.info("Last page of search index reached.")
                break
                
            skip += top
            time.sleep(0.1)
            
        except Exception as e:
            log.error(f"Error querying search index: {e}")
            break
            
    log.info(f"Metadata harvesting complete. Total records: {len(all_records):,}")
    return all_records

# -- Checkpoint and State Management -----------------------------------------
def load_checkpoint(output_dir: Path) -> dict:
    """Loads checkpoint file or initializes a new one."""
    checkpoint_file = output_dir / "checkpoint.json"
    if checkpoint_file.exists():
        try:
            with open(checkpoint_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            log.error(f"Failed to load checkpoint file: {e}. Starting fresh.")
            
    return {
        "processed_urls": {},
        "metadata_harvested": False
    }

def save_checkpoint(output_dir: Path, state: dict):
    """Saves checkpoint file atomically using a temporary file."""
    checkpoint_file = output_dir / "checkpoint.json"
    temp_file = output_dir / "checkpoint.json.tmp"
    try:
        with checkpoint_lock:
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            temp_file.replace(checkpoint_file)
    except Exception as e:
        log.error(f"Failed to save checkpoint file: {e}")

# -- Filename Sanitizer ------------------------------------------------------
def sanitize_filename(product_name: str, doc_type: str, pl_number: str, original_filename: str) -> str:
    """Creates a clean, Windows-safe, descriptive filename."""
    prod_clean = str(product_name).strip().upper()
    prod_clean = re.sub(r'[^a-zA-Z0-9\s_\-]', '', prod_clean)
    prod_clean = re.sub(r'\s+', '_', prod_clean)
    
    pl_clean = ""
    if pl_number:
        if isinstance(pl_number, list):
            pl_clean = pl_number[0]
        else:
            pl_clean = str(pl_number)
        pl_clean = re.sub(r'[^a-zA-Z0-9]', '', pl_clean)
        
    ext = ".pdf"
    if original_filename:
        orig_ext = Path(original_filename).suffix
        if orig_ext.lower() in ['.pdf', '.txt', '.doc', '.docx']:
            ext = orig_ext
            
    doc_type_clean = str(doc_type).lower()
    
    parts = []
    if prod_clean:
        parts.append(prod_clean[:80])
    if pl_clean:
        parts.append(pl_clean)
    parts.append(doc_type_clean)
    
    filename = "_".join(parts) + ext
    return filename

# -- Per-Document Metadata CSV Exporter --------------------------------------
def append_to_extracted_csv(output_dir: Path, row_data: dict):
    """Appends a single processed document record to mhra_documents.csv in a thread-safe manner."""
    csv_file = output_dir / "mhra_documents.csv"
    file_exists = csv_file.exists()
    
    with csv_lock:
        with open(csv_file, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=ALL_CSV_FIELDS)
            if not file_exists:
                writer.writeheader()
            writer.writerow(row_data)

def write_metadata(output_dir: Path, records: list):
    """Writes raw and unified metadata to CSV and JSON files."""
    csv_file = output_dir / "raw_metadata.csv"
    json_file = output_dir / "raw_metadata.json"
    
    if records:
        headers = set()
        for record in records:
            if isinstance(record, dict):
                headers.update(record.keys())
        headers = sorted(list(headers))
        
        with csv_lock:
            with open(csv_file, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=headers)
                writer.writeheader()
                for r in records:
                    row = {}
                    for k in headers:
                        val = r.get(k, "")
                        if isinstance(val, list):
                            row[k] = ";".join(str(x) for x in val)
                        elif isinstance(val, dict):
                            row[k] = json.dumps(val, ensure_ascii=False)
                        else:
                            row[k] = val
                    writer.writerow(row)
                    
            with open(json_file, 'w', encoding='utf-8') as f:
                json.dump(records, f, indent=2, ensure_ascii=False)

def json_to_csv(json_path: Path, csv_path: Path):
    """Converts a JSON file to a CSV file."""
    if not json_path.exists():
        return
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, list):
            data = [data]
        if not data:
            return
            
        headers = set()
        for record in data:
            if isinstance(record, dict):
                headers.update(record.keys())
        headers = sorted(list(headers))
        
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            for record in data:
                row = {}
                for k in headers:
                    val = record.get(k, "")
                    if isinstance(val, list):
                        row[k] = ";".join(str(x) for x in val)
                    elif isinstance(val, dict):
                        row[k] = json.dumps(val, ensure_ascii=False)
                    else:
                        row[k] = val
                writer.writerow(row)
    except Exception as e:
        print(f"Error converting {json_path} to CSV: {e}")

def cleanup_and_transform(output_dir: Path):
    """Deletes temporary files and releases logging locks."""
    checkpoint_file = output_dir / "checkpoint.json"
    log_file = output_dir / "mhra_downloader.log"

    print("\nStarting post-download cleanup...")

    raw_json_file = output_dir / "raw_metadata.json"
    if raw_json_file.exists():
        try:
            raw_json_file.unlink()
            print(f"Deleted raw metadata JSON file: {raw_json_file.name}")
        except Exception as e:
            print(f"Failed to delete raw metadata JSON file: {e}")

    if checkpoint_file.exists():
        try:
            checkpoint_file.unlink()
            print(f"Deleted checkpoint file: {checkpoint_file.name}")
        except Exception as e:
            print(f"Failed to delete checkpoint file: {e}")

    if log_file.exists():
        try:
            for handler in logging.root.handlers[:]:
                handler.close()
                logging.root.removeHandler(handler)
            logging.basicConfig(level=logging.INFO, handlers=[logging.StreamHandler(sys.stdout)])
            log_file.unlink()
            print(f"Deleted log file: {log_file.name}")
        except Exception as e:
            print(f"Failed to delete log file: {e}")

# -- Main Execution Pipeline -------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="MHRA UK approvals collector: per-document metadata CSV plus raw PDFs."
    )
    parser.add_argument(
        "--output-dir", default=str(BASE_DIR / "mhra_data"), help="Directory to save output files and metadata."
    )
    parser.add_argument(
        "--threads", type=int, default=5, help="Number of concurrent processing threads."
    )
    parser.add_argument(
        "--max-retries", type=int, default=5, help="Maximum retries for failed downloads."
    )
    parser.add_argument(
        "--timeout", type=int, default=30, help="Timeout in seconds for HTTP requests."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Scrape metadata index only, without downloading PDFs."
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Limit the number of products to process (for testing)."
    )
    parser.add_argument(
        "--doc-types", default=None, help="Comma-separated document types to process (e.g., 'Par,Spc')."
    )
    parser.add_argument(
        "--download-pdfs", "--save-pdfs", action="store_true", default=False, help="Save local PDF files to disk (the vector store extracts them downstream)."
    )
    
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    setup_logging(output_dir)
    log.info("MHRA UK approvals data collector started.")
    
    session = build_session(args.max_retries)
    checkpoint = load_checkpoint(output_dir)
    
    # -- Phase 1: Harvest/Load Metadata ---------------------------------------
    metadata_file = output_dir / "raw_metadata.json"
    all_records = []
    
    if metadata_file.exists() and checkpoint.get("metadata_harvested", False):
        log.info(f"Loading cached metadata from {metadata_file}...")
        try:
            with open(metadata_file, 'r', encoding='utf-8') as f:
                all_records = json.load(f)
            log.info(f"Loaded {len(all_records):,} records from local cache.")
        except Exception as e:
            log.error(f"Failed to load cached metadata: {e}. Re-harvesting from scratch.")
            all_records = []
            
    if not all_records:
        all_records = harvest_metadata(session, limit=args.limit)
        if not all_records:
            log.critical("Failed to retrieve any records from the search index. Exiting.")
            sys.exit(1)
            
        try:
            with open(metadata_file, 'w', encoding='utf-8') as f:
                json.dump(all_records, f, indent=2, ensure_ascii=False)
            checkpoint["metadata_harvested"] = True
            save_checkpoint(output_dir, checkpoint)
            log.info(f"Cached raw metadata to {metadata_file}")
        except Exception as e:
            log.error(f"Failed to cache metadata: {e}")
            
    for r in all_records:
        url = r.get("metadata_storage_path", "")
        r["id"] = hashlib.md5(url.encode('utf-8')).hexdigest()[:8]
        r["local_pdf_path"] = ""
        
    if args.doc_types:
        allowed_types = [t.strip().lower() for t in args.doc_types.split(',')]
        log.info(f"Filtering queue by document types: {allowed_types}")
        all_records = [r for r in all_records if str(r.get("doc_type")).lower() in allowed_types]
        log.info(f"Records remaining after filter: {len(all_records):,}")
        
    if args.limit:
        log.info(f"Limiting processing queue to first {args.limit} records.")
        all_records = all_records[:args.limit]
        
    if args.dry_run:
        log.info("Dry-run active. Exporting raw metadata without downloading PDFs...")
        write_metadata(output_dir, all_records)
        log.info("Dry-run completed successfully.")
        return
        
    # -- Phase 2: Build Processing Queue --------------------------------------
    # Documents already published to S3, restored by the pipeline's hydrate step.
    # checkpoint.json tracks progress but never leaves the host, so without this a
    # fresh machine re-downloads all ~43k PDFs S3 already holds. Filenames are
    # deterministic (sanitize_filename), so matching on the relative path is safe.
    published = set()
    idx_path = output_dir / "mhra_documents.csv"
    if idx_path.exists():
        with open(idx_path, newline="", encoding="utf-8", errors="replace") as f:
            for row in csv.DictReader(f):
                p = (row.get("local_pdf_path") or "").strip().replace("\\", "/")
                if p:
                    published.add(p)
        log.info(f"{len(published):,} document(s) already published to S3 will be skipped.")
    else:
        log.info("No previous mhra_documents.csv - treating this as a first run.")

    process_queue = []
    already_have = 0

    for idx, r in enumerate(all_records):
        url = r.get("metadata_storage_path")
        if not url:
            continue

        doc_type = str(r.get("doc_type", "unknown")).lower()
        product_name = r.get("product_name", "unknown")
        pl_number = r.get("pl_number", [])
        original_filename = r.get("file_name")
        
        dest_filename = sanitize_filename(product_name, doc_type, pl_number, original_filename)
        dest_path = output_dir / "pdfs" / doc_type / dest_filename
        
        rel_pdf_path = str(dest_path.relative_to(output_dir)) if args.download_pdfs else ""
        r["local_pdf_path"] = rel_pdf_path
        
        state_record = checkpoint["processed_urls"].get(url, {})
        if rel_pdf_path and rel_pdf_path.replace("\\", "/") in published:
            # Already in S3 from an earlier run; keep the row so the metadata CSV
            # stays complete, but do not fetch the file again.
            already_have += 1
        elif state_record.get("status") == "completed":
            log.debug(f"Document already processed in checkpoint: {dest_filename}")
        else:
            process_queue.append({
                "worker_id": idx,
                "url": url,
                "dest_path": dest_path,
                "record": r,
                "product_name": product_name,
                "doc_type": doc_type,
                "pl_number": pl_number,
                "original_filename": original_filename
            })
            
    log.info(f"{len(all_records):,} catalogued | {already_have:,} already in S3 | "
             f"{len(process_queue):,} to download")
    
    if not process_queue:
        log.info("All documents are already processed. Writing metadata...")
        write_metadata(output_dir, all_records)
        log.info("MHRA UK approvals processor finished.")
        cleanup_and_transform(output_dir)
        return
        
    # -- Phase 3: Concurrent Worker Pool --------------------------------------
    success_count = 0
    fail_count = 0
    
    pbar = None
    if tqdm:
        pbar = tqdm(total=len(process_queue), desc="Processing MHRA Documents", unit="doc")
        
    def worker(task):
        nonlocal success_count, fail_count
        worker_id = task["worker_id"]
        url = task["url"]
        dest_path = task["dest_path"]
        record = task["record"]
        doc_type = task["doc_type"]
        product_name = task["product_name"]
        
        with checkpoint_lock:
            checkpoint["processed_urls"][url] = {
                "status": "pending",
                "processed_at": None,
                "error": None
            }
        save_checkpoint(output_dir, checkpoint)
        
        try:
            # 1. Stream PDF in memory
            r = session.get(url, stream=True, timeout=args.timeout)
            if r.status_code != 200:
                log.warning(f"HTTP {r.status_code} fetching {url}")
                with checkpoint_lock:
                    checkpoint["processed_urls"][url].update({"status": "failed", "error": f"HTTP {r.status_code}"})
                    fail_count += 1
                save_checkpoint(output_dir, checkpoint)
                if pbar: pbar.update(1)
                return
                
            pdf_bytes = r.content
            
            # Save PDF to disk if requested
            if args.download_pdfs:
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                with open(dest_path, "wb") as f:
                    f.write(pdf_bytes)
                log.debug(f"Saved PDF to {dest_path}")

            # 4. Construct output CSV row
            pl_str = ";".join(record.get("pl_number", [])) if isinstance(record.get("pl_number"), list) else record.get("pl_number", "")
            sub_str = ";".join(record.get("substance_name", [])) if isinstance(record.get("substance_name"), list) else record.get("substance_name", "")
            
            row_data = {
                "id": record.get("id", ""),
                "product_name": record.get("product_name", ""),
                "doc_type": record.get("doc_type", ""),
                "pl_number": pl_str,
                "substance_name": sub_str,
                "title": record.get("title", ""),
                "created": record.get("created", ""),
                "metadata_storage_size": record.get("metadata_storage_size", ""),
                "original_filename": record.get("file_name", ""),
                "azure_blob_url": url,
                "local_pdf_path": str(dest_path.relative_to(output_dir)) if args.download_pdfs else "",
            }
            
            append_to_extracted_csv(output_dir, row_data)
            
            with checkpoint_lock:
                checkpoint["processed_urls"][url].update({
                    "status": "completed",
                    "processed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "error": None
                })
                success_count += 1
                
        except Exception as e:
            log.warning(f"Error processing {url}: {e}")
            with checkpoint_lock:
                checkpoint["processed_urls"][url].update({
                    "status": "failed",
                    "error": str(e)
                })
                fail_count += 1
                
        save_checkpoint(output_dir, checkpoint)
        if pbar:
            pbar.update(1)

    log.info(f"Launching processing pool with {args.threads} worker threads...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.threads, thread_name_prefix="MHRAWorker") as executor:
        futures = [executor.submit(worker, task) for task in process_queue]
        concurrent.futures.wait(futures)
        
    if pbar:
        pbar.close()
        
    # -- Phase 4: Write Final Metadata & Summary ------------------------------
    write_metadata(output_dir, all_records)
    
    log.info("==================================================")
    log.info("            MHRA Processing Summary               ")
    log.info("==================================================")
    log.info(f"Total product records: {len(all_records):,}")
    log.info(f"Successfully processed: {success_count:,}")
    log.info(f"Failed: {fail_count:,}")
    log.info(f"Document metadata CSV saved to: {(output_dir / 'mhra_documents.csv').resolve()}")
    log.info("MHRA processor finished successfully.")
    
    cleanup_and_transform(output_dir)

if __name__ == "__main__":
    main()
