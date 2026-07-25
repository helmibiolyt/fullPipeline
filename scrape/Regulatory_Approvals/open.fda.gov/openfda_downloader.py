#!/usr/bin/env python3
"""
openfda_downloader.py
=====================
A robust, professional data collector for the FDA's approved drug database (Drugs@FDA) via openFDA.
Fetches comprehensive drug application, product, submission, and labeling metadata.

Features:
  1. download-bulk (Default & Recommended):
     - Automatically resolves the latest partition download link from openFDA.
     - Downloads the official Drugs@FDA bulk JSON zip archive (approx. 9MB).
     - Range-Based Resumption: Resumes interrupted downloads using HTTP Range headers.
     - Streams and extracts the data directly, writing raw JSONL and a highly flattened analytical CSV.
  2. api-harvest:
     - Concurrently harvests the live Drugs@FDA database via the REST API.
     - Bypasses the 25,000 skip limit using the 'search_after' cursor from Link headers.
     - Multi-threaded Partitioning: Parallelizes queries across 4 concurrent threads using application
       prefixes (NDA, ANDA0, ANDA2, BLA) to balance the load and speed up download.
     - Resumability: Saves progress at the partition level. Resumes seamlessly from interruptions.
     - Throttling: Automatically respects public rate limits (40 req/min) or uses higher speeds with an API key.
  3. search:
     - Queries the local compiled dataset by application number, brand name, active ingredient, or sponsor.
     - Falls back to the live API if the local dataset is not found.

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
import zipfile
import io
import threading
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import concurrent.futures
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

# Optional progress indicator
try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

# -- Configuration & Constants -----------------------------------------------
API_BASE_URL = "https://api.fda.gov/drug/drugsfda.json"
METADATA_URL = "https://api.fda.gov/download.json"
DEFAULT_FALLBACK_ZIP_URL = "https://download.open.fda.gov/drug/drugsfda/drug-drugsfda-0001-of-0001.json.zip"

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = str(BASE_DIR / "openfda_data")
DEFAULT_PAGE_SIZE = 1000  # Max page size allowed by openFDA
MAX_RETRIES = 5

# 4-partition strategy to split the 29,159 records for multi-threading
PARTITIONS = {
    "NDA": "application_number:NDA*",
    "ANDA0": "application_number:ANDA0*",
    "ANDA2": "application_number:ANDA2*",
    "BLA": "application_number:BLA*"
}

CSV_FIELDS = [
    "application_number",
    "sponsor_name",
    "product_number",
    "brand_name",
    "reference_drug",
    "reference_standard",
    "dosage_form",
    "route",
    "marketing_status",
    "active_ingredients",
    "openfda_brand_names",
    "openfda_generic_names",
    "openfda_manufacturers",
    "openfda_product_ndcs",
    "openfda_package_ndcs",
    "openfda_rxcuis",
    "openfda_uniis",
    "openfda_substance_names",
    "openfda_spl_set_ids",
    "openfda_product_types",
    "openfda_pharm_class_epc",
    "openfda_pharm_class_moa",
    "openfda_pharm_class_pe",
    "submissions",
    "application_docs"
]

# -- Logging Setup -----------------------------------------------------------
log = logging.getLogger("openfda_downloader")

def setup_logging(output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / "openfda_downloader.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] (%(threadName)s) %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout)
        ]
    )

# -- HTTP Session Configuration ----------------------------------------------
def build_session(api_key: str = None) -> requests.Session:
    """Builds a requests.Session with retries and default headers."""
    session = requests.Session()
    retry_strategy = Retry(
        total=MAX_RETRIES,
        backoff_factor=2.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    
    headers = {
        "Accept": "application/json",
        "User-Agent": "BiolytInternOpenFDADownloader/1.0 (Python; requests)"
    }
    if api_key:
        headers["X-Api-Key"] = api_key
        
    session.headers.update(headers)
    return session

# -- Data Extractor & Flattener ----------------------------------------------
def extract_and_flatten_record(record: dict) -> list:
    """
    Parses a single nested Drugs@FDA application record and flattens it
    into a list of row dictionaries (one row per product).
    """
    def safe_str(val) -> str:
        if val is None:
            return ""
        return str(val).strip()

    app_number = safe_str(record.get("application_number"))
    sponsor_name = safe_str(record.get("sponsor_name"))

    # 1. Compile submissions history
    sub_list = []
    for sub in record.get("submissions", []):
        sub_type = safe_str(sub.get("submission_type"))
        sub_num = safe_str(sub.get("submission_number"))
        sub_status = safe_str(sub.get("submission_status"))
        sub_date = safe_str(sub.get("submission_status_date"))
        if len(sub_date) == 8:
            sub_date = f"{sub_date[:4]}-{sub_date[4:6]}-{sub_date[6:]}"
        sub_list.append(f"{sub_type}({sub_num}) - {sub_status} ({sub_date})")
    submissions_str = " | ".join(sub_list)

    # 2. Compile application documents
    doc_list = []
    for sub in record.get("submissions", []):
        for doc in sub.get("application_docs", []):
            d_type = safe_str(doc.get("type"))
            d_url = safe_str(doc.get("url"))
            d_date = safe_str(doc.get("date"))
            if len(d_date) == 8:
                d_date = f"{d_date[:4]}-{d_date[4:6]}-{d_date[6:]}"
            if d_url:
                doc_list.append(f"{d_type} ({d_date}): {d_url}")
    docs_str = " | ".join(doc_list)

    # 3. Flatten openfda enrichment sub-object
    openfda = record.get("openfda", {})
    def get_openfda_list(key):
        val = openfda.get(key, [])
        if isinstance(val, list):
            return " | ".join(safe_str(v) for v in val if safe_str(v))
        return safe_str(val)

    of_brand = get_openfda_list("brand_name")
    of_generic = get_openfda_list("generic_name")
    of_mfr = get_openfda_list("manufacturer_name")
    of_prod_ndc = get_openfda_list("product_ndc")
    of_pkg_ndc = get_openfda_list("package_ndc")
    of_rxcui = get_openfda_list("rxcui")
    of_unii = get_openfda_list("unii")
    of_substance = get_openfda_list("substance_name")
    of_spl_set = get_openfda_list("spl_set_id")
    of_prod_type = get_openfda_list("product_type")
    of_class_epc = get_openfda_list("pharm_class_epc")
    of_class_moa = get_openfda_list("pharm_class_moa")
    of_class_pe = get_openfda_list("pharm_class_pe")

    # 4. Generate rows for each product
    products = record.get("products", [])
    flattened_rows = []

    if not products:
        # Fallback if application has no products listed
        row = {
            "application_number": app_number,
            "sponsor_name": sponsor_name,
            "product_number": "",
            "brand_name": "",
            "reference_drug": "",
            "reference_standard": "",
            "dosage_form": "",
            "route": "",
            "marketing_status": "",
            "active_ingredients": "",
            "openfda_brand_names": of_brand,
            "openfda_generic_names": of_generic,
            "openfda_manufacturers": of_mfr,
            "openfda_product_ndcs": of_prod_ndc,
            "openfda_package_ndcs": of_pkg_ndc,
            "openfda_rxcuis": of_rxcui,
            "openfda_uniis": of_unii,
            "openfda_substance_names": of_substance,
            "openfda_spl_set_ids": of_spl_set,
            "openfda_product_types": of_prod_type,
            "openfda_pharm_class_epc": of_class_epc,
            "openfda_pharm_class_moa": of_class_moa,
            "openfda_pharm_class_pe": of_class_pe,
            "submissions": submissions_str,
            "application_docs": docs_str
        }
        flattened_rows.append(row)
    else:
        for prod in products:
            prod_num = safe_str(prod.get("product_number"))
            brand_name = safe_str(prod.get("brand_name"))
            ref_drug = safe_str(prod.get("reference_drug"))
            ref_std = safe_str(prod.get("reference_standard"))
            dosage_form = safe_str(prod.get("dosage_form"))
            route = safe_str(prod.get("route"))
            mkt_status = safe_str(prod.get("marketing_status"))

            # Active ingredients list
            ing_list = []
            for ing in prod.get("active_ingredients", []):
                ing_name = safe_str(ing.get("name"))
                ing_strength = safe_str(ing.get("strength"))
                if ing_strength:
                    ing_list.append(f"{ing_name} ({ing_strength})")
                else:
                    ing_list.append(ing_name)
            ingredients_str = " | ".join(ing_list)

            row = {
                "application_number": app_number,
                "sponsor_name": sponsor_name,
                "product_number": prod_num,
                "brand_name": brand_name,
                "reference_drug": ref_drug,
                "reference_standard": ref_std,
                "dosage_form": dosage_form,
                "route": route,
                "marketing_status": mkt_status,
                "active_ingredients": ingredients_str,
                "openfda_brand_names": of_brand,
                "openfda_generic_names": of_generic,
                "openfda_manufacturers": of_mfr,
                "openfda_product_ndcs": of_prod_ndc,
                "openfda_package_ndcs": of_pkg_ndc,
                "openfda_rxcuis": of_rxcui,
                "openfda_uniis": of_unii,
                "openfda_substance_names": of_substance,
                "openfda_spl_set_ids": of_spl_set,
                "openfda_product_types": of_prod_type,
                "openfda_pharm_class_epc": of_class_epc,
                "openfda_pharm_class_moa": of_class_moa,
                "openfda_pharm_class_pe": of_class_pe,
                "submissions": submissions_str,
                "application_docs": docs_str
            }
            flattened_rows.append(row)

    return flattened_rows

# -- Downloader Class --------------------------------------------------------
class OpenFDADownloader:
    def __init__(self, output_dir: Path, session: requests.Session, delay: float = 1.5):
        self.output_dir = output_dir
        self.session = session
        self.delay = delay
        self.progress_lock = threading.Lock()

        # Paths
        self.zip_path = self.output_dir / "drug-drugsfda-0001-of-0001.json.zip"
        self.jsonl_path = self.output_dir / "openfda_drugs.jsonl"
        self.csv_path = self.output_dir / "openfda_drugs.csv"
        self.progress_path = self.output_dir / "openfda_progress.json"

        # Ensure output directory exists
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------------------
    # Bulk Download Mode
    # -------------------------------------------------------------------------
    def fetch_bulk_zip_url(self) -> str:
        """Queries openFDA download metadata to get the latest zip URL."""
        log.info("Querying openFDA download metadata for latest Drugs@FDA zip URL...")
        try:
            resp = self.session.get(METADATA_URL, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                drugsfda_meta = data.get("results", {}).get("drug", {}).get("drugsfda", {})
                partitions = drugsfda_meta.get("partitions", [])
                if partitions:
                    download_url = partitions[0].get("file")
                    if download_url:
                        log.info(f"Successfully resolved latest bulk download URL: {download_url}")
                        return download_url
            log.warning(f"Could not resolve link from metadata (status {resp.status_code}). Using fallback URL.")
        except Exception as e:
            log.warning(f"Metadata query failed: {e}. Using fallback URL.")
        return DEFAULT_FALLBACK_ZIP_URL

    def download_bulk_zip(self, url: str) -> bool:
        """Downloads the bulk ZIP file with range-based resumption support."""
        log.info(f"Initiating download for: {url}")
        
        # Check remote file size using HEAD request
        try:
            head_resp = self.session.head(url, timeout=30)
            head_resp.raise_for_status()
            total_size = int(head_resp.headers.get("content-length", 0))
            accept_ranges = head_resp.headers.get("accept-ranges", "").lower()
            log.info(f"Remote file size: {total_size / (1024 * 1024):.2f} MB (Accept-Ranges: {accept_ranges})")
        except Exception as e:
            log.warning(f"HEAD request failed: {e}. Download will proceed without range resumption verification.")
            total_size = 0
            accept_ranges = "none"

        # Determine download headers and local file mode
        headers = {}
        local_size = 0
        file_mode = "wb"

        if self.zip_path.exists() and total_size > 0:
            local_size = self.zip_path.stat().st_size
            if local_size < total_size:
                if "bytes" in accept_ranges or accept_ranges == "yes":
                    log.info(f"Partially downloaded file found ({local_size / (1024*1024):.2f} MB). Resuming download...")
                    headers["Range"] = f"bytes={local_size}-"
                    file_mode = "ab"
                else:
                    log.info("Partially downloaded file found but server does not support ranges. Restarting download.")
            elif local_size == total_size:
                log.info("Bulk ZIP file is already fully downloaded. Skipping download.")
                return True
            else:
                log.info("Local file size is larger than remote size. Re-downloading from scratch.")

        # Stream the download
        try:
            resp = self.session.get(url, headers=headers, stream=True, timeout=60)
            resp.raise_for_status()
            
            # If server ignored the Range header and sent the whole file (HTTP 200 instead of 206)
            if resp.status_code == 200 and file_mode == "ab":
                log.warning("Server ignored Range request; downloading full file from scratch.")
                file_mode = "wb"
                local_size = 0

            downloaded = local_size
            chunk_size = 128 * 1024  # 128 KB
            
            # Display progress bar if tqdm is available, else log progress
            if tqdm:
                pbar = tqdm(total=total_size, initial=downloaded, unit="B", unit_scale=True, desc="Downloading Bulk ZIP", leave=True)
            else:
                pbar = None
                log.info("Starting download streaming...")

            last_log_time = time.time()
            
            with open(self.zip_path, file_mode) as f:
                for chunk in resp.iter_content(chunk_size=chunk_size):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if pbar:
                            pbar.update(len(chunk))
                        elif time.time() - last_log_time > 3.0:
                            percent = (downloaded / total_size * 100) if total_size > 0 else 0
                            log.info(f"Downloaded: {downloaded / (1024*1024):.2f} MB ({percent:.1f}%)")
                            last_log_time = time.time()

            if pbar:
                pbar.close()
            
            log.info("Bulk ZIP download completed successfully.")
            return True
        except Exception as e:
            log.error(f"Download failed or was interrupted: {e}")
            return False

    def process_bulk_zip(self) -> bool:
        """Extracts the zipped JSON, parses all records, and writes JSONL and CSV."""
        if not self.zip_path.exists():
            log.error(f"ZIP file not found at {self.zip_path}. Cannot process.")
            return False

        log.info(f"Opening ZIP file: {self.zip_path.name}...")
        try:
            with zipfile.ZipFile(self.zip_path, "r") as zf:
                file_list = zf.namelist()
                json_files = [f for f in file_list if f.endswith(".json")]
                if not json_files:
                    log.error("No JSON file found inside the ZIP archive.")
                    return False
                
                target_json = json_files[0]
                log.info(f"Extracting and parsing {target_json} from ZIP...")
                
                with zf.open(target_json) as jf:
                    # Load JSON in memory (approx. 60MB uncompressed, very fast in python)
                    payload = json.load(jf)
                    
            results = payload.get("results", [])
            total_records = len(results)
            log.info(f"Loaded {total_records:,} raw drug application records from JSON.")

            # Compile into JSONL and CSV
            log.info("Compiling outputs (raw JSONL and flattened CSV)...")
            
            with open(self.jsonl_path, "w", encoding="utf-8") as jl_file, \
                 open(self.csv_path, "w", newline="", encoding="utf-8") as csv_file:
                
                csv_writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
                csv_writer.writeheader()

                # Optional progress bar for compilation
                iterator = tqdm(results, desc="Processing Records") if tqdm else results

                for record in iterator:
                    # Write Raw JSONL
                    jl_file.write(json.dumps(record) + "\n")
                    
                    # Flatten and write to CSV
                    flat_rows = extract_and_flatten_record(record)
                    for row in flat_rows:
                        csv_writer.writerow(row)

                csv_file.flush()
                jl_file.flush()

            log.info(f"Successfully processed and saved data:")
            log.info(f"  - Raw JSON Lines: {self.jsonl_path.resolve()} ({self.jsonl_path.stat().st_size / (1024*1024):.2f} MB)")
            log.info(f"  - Flattened CSV : {self.csv_path.resolve()} ({self.csv_path.stat().st_size / (1024*1024):.2f} MB)")
            return True
            
        except Exception as e:
            log.error(f"Error processing ZIP file: {e}", exc_info=True)
            return False

    # -------------------------------------------------------------------------
    # API Harvesting Mode
    # -------------------------------------------------------------------------
    def load_api_progress(self) -> dict:
        """Loads or initializes the progress state for partition harvesting."""
        if self.progress_path.exists():
            try:
                with open(self.progress_path, "r", encoding="utf-8") as f:
                    state = json.load(f)
                log.info(f"Loaded existing progress state from {self.progress_path.name}.")
                # Verify partitions match our specification
                if "partitions" in state:
                    return state
            except Exception as e:
                log.warning(f"Could not parse progress file: {e}. Initializing a fresh state.")

        # Initialize default state
        state = {
            "completed": False,
            "timestamp": time.time(),
            "partitions": {}
        }
        for name, query in PARTITIONS.items():
            state["partitions"][name] = {
                "query": query,
                "downloaded": 0,
                "total": 0,
                "search_after": None,
                "completed": False
            }
        return state

    def save_api_progress(self, state: dict):
        """Thread-safe save of the progress state to disk."""
        with self.progress_lock:
            state["timestamp"] = time.time()
            try:
                with open(self.progress_path, "w", encoding="utf-8") as f:
                    json.dump(state, f, indent=2)
            except Exception as e:
                log.warning(f"Failed to save progress state: {e}")

    def fetch_partition_page(self, name: str, query: str, limit: int, search_after: str = None) -> tuple[dict | None, str | None]:
        """
        Fetches a single page of data from the openFDA API for a given partition,
        returning the JSON payload and the next 'search_after' token.
        """
        params = {
            "search": query,
            "limit": limit,
            "sort": "application_number:asc"
        }
        if search_after:
            params["search_after"] = search_after

        try:
            resp = self.session.get(API_BASE_URL, params=params, timeout=45)
            
            # Check for rate limiting or errors
            if resp.status_code == 429:
                log.warning(f"Rate limited (429) on partition {name}. Backing off for 15 seconds...")
                time.sleep(15.0)
                return None, search_after
            
            if resp.status_code == 404:
                # 404 means no records found or end of partition
                return {"results": [], "meta": {"results": {"total": 0}}}, None

            resp.raise_for_status()
            payload = resp.json()

            # Parse next search_after from Link header
            next_token = None
            link_header = resp.headers.get("Link") or resp.headers.get("link")
            if link_header:
                # Format: <url>; rel="next", <url2>; rel="prev"
                parts = link_header.split(",")
                for part in parts:
                    if 'rel="next"' in part.lower():
                        # Extract URL inside angle brackets
                        url_start = part.find("<") + 1
                        url_end = part.find(">")
                        if url_start > 0 and url_end > 0:
                            next_url = part[url_start:url_end]
                            parsed_url = urlparse(next_url)
                            query_params = parse_qs(parsed_url.query)
                            tokens = query_params.get("search_after", [])
                            if tokens:
                                next_token = tokens[0]
                                break

            return payload, next_token

        except Exception as e:
            log.warning(f"HTTP request exception on partition {name}: {e}")
            return None, search_after

    def harvest_partition_thread(self, name: str, state_part: dict, progress_state: dict, limit: int, max_records: int | None):
        """Worker thread loop to download a single database partition."""
        thread_name = threading.current_thread().name
        query = state_part["query"]
        
        # Determine total records available if not already resolved
        if state_part["total"] == 0:
            log.info(f"Querying total record count for partition {name}...")
            payload, _ = self.fetch_partition_page(name, query, limit=1)
            if payload:
                state_part["total"] = payload.get("meta", {}).get("results", {}).get("total", 0)
                log.info(f"Partition {name} has {state_part['total']:,} total records available.")
                self.save_api_progress(progress_state)
            else:
                log.error(f"Failed to fetch initial count for partition {name}. Aborting thread.")
                return

        total_to_fetch = state_part["total"]
        if max_records is not None:
            total_to_fetch = min(total_to_fetch, max_records)

        if state_part["completed"] or state_part["downloaded"] >= total_to_fetch:
            log.info(f"Partition {name} already fully harvested ({state_part['downloaded']:,}/{total_to_fetch:,} records).")
            return

        # Open partition temp JSONL file in append mode
        part_jsonl_path = self.output_dir / f"part_{name}.jsonl"
        mode = "a" if state_part["downloaded"] > 0 else "w"
        
        log.info(f"Starting harvest loop for partition {name} from downloaded={state_part['downloaded']:,} to target={total_to_fetch:,}...")
        
        consecutive_failures = 0
        
        with open(part_jsonl_path, mode, encoding="utf-8") as f:
            while state_part["downloaded"] < total_to_fetch:
                current_search_after = state_part["search_after"]
                
                # Fetch next page
                # Dynamic page limit to not over-fetch the limit
                page_limit = min(limit, total_to_fetch - state_part["downloaded"])
                
                payload, next_token = self.fetch_partition_page(name, query, limit=page_limit, search_after=current_search_after)
                
                if payload is None:
                    consecutive_failures += 1
                    if consecutive_failures >= 5:
                        log.error(f"Partition {name} encountered 5 consecutive failures. Pausing thread and saving state.")
                        break
                    # Wait and retry
                    time.sleep(2.0 * consecutive_failures)
                    continue

                consecutive_failures = 0
                results = payload.get("results", [])
                
                if not results:
                    log.info(f"Partition {name} returned 0 records. Mark as completed.")
                    state_part["completed"] = True
                    break

                # Write raw records to partition JSONL
                for record in results:
                    f.write(json.dumps(record) + "\n")
                
                f.flush()
                
                # Update progress
                state_part["downloaded"] += len(results)
                state_part["search_after"] = next_token
                
                log.info(f"Partition {name} Progress: {state_part['downloaded']:,}/{total_to_fetch:,} records downloaded.")
                
                if not next_token or state_part["downloaded"] >= total_to_fetch:
                    log.info(f"Partition {name} reached its end or target limit.")
                    state_part["completed"] = True
                    self.save_api_progress(progress_state)
                    break

                self.save_api_progress(progress_state)
                
                # Polite throttle delay
                time.sleep(self.delay)

        log.info(f"Thread for partition {name} finished.")

    def run_api_harvest(self, limit_per_page: int = DEFAULT_PAGE_SIZE, max_records_total: int | None = None) -> bool:
        """
        Orchestrates the multi-threaded harvesting of the openFDA database,
        merging the results and flattening them to CSV when finished.
        """
        progress_state = self.load_api_progress()
        
        # Determine maximum records per partition if limit is active
        # Simple split of limit among partitions (for testing purposes)
        limit_per_partition = None
        if max_records_total is not None:
            limit_per_partition = max_records_total // len(PARTITIONS)
            log.info(f"Testing Limit active: Harvesting up to {limit_per_partition:,} records per partition (total: {max_records_total:,})")

        # Concurrently execute partition harvesting
        log.info(f"Spawning thread pool to harvest partitions: {list(PARTITIONS.keys())}")
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(PARTITIONS), thread_name_prefix="Harvester") as executor:
            futures = []
            for name in PARTITIONS.keys():
                state_part = progress_state["partitions"][name]
                futures.append(
                    executor.submit(
                        self.harvest_partition_thread,
                        name,
                        state_part,
                        progress_state,
                        limit_per_page,
                        limit_per_partition
                    )
                )
            # Wait for all threads to complete
            concurrent.futures.wait(futures)

        # Check if all partitions are completed successfully
        all_done = True
        for name, state_part in progress_state["partitions"].items():
            target_to_check = state_part["total"]
            if limit_per_partition is not None:
                target_to_check = min(target_to_check, limit_per_partition)
                
            if state_part["downloaded"] < target_to_check and not state_part["completed"]:
                log.warning(f"Partition {name} did not finish downloading ({state_part['downloaded']:,}/{target_to_check:,}).")
                all_done = False

        if not all_done:
            log.error("One or more harvesting partitions failed to complete. Please re-run the script to resume.")
            return False

        log.info("All partitions downloaded successfully. Merging and compile-flattening datasets...")
        
        # Merge partition files and compile JSONL and CSV
        try:
            with open(self.jsonl_path, "w", encoding="utf-8") as jl_file, \
                 open(self.csv_path, "w", newline="", encoding="utf-8") as csv_file:
                
                csv_writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
                csv_writer.writeheader()

                total_records_merged = 0
                for name in PARTITIONS.keys():
                    part_path = self.output_dir / f"part_{name}.jsonl"
                    if not part_path.exists() or part_path.stat().st_size == 0:
                        continue
                    
                    log.info(f"Merging partition {name} into master file...")
                    with open(part_path, "r", encoding="utf-8") as p_file:
                        for line in p_file:
                            line = line.strip()
                            if not line:
                                continue
                            jl_file.write(line + "\n")
                            
                            # Flatten to CSV
                            record = json.loads(line)
                            flat_rows = extract_and_flatten_record(record)
                            for row in flat_rows:
                                csv_writer.writerow(row)
                            
                            total_records_merged += 1
                
                csv_file.flush()
                jl_file.flush()

            log.info(f"Successfully compiled live harvested data:")
            log.info(f"  - Total applications merged: {total_records_merged:,}")
            log.info(f"  - Raw JSON Lines: {self.jsonl_path.resolve()} ({self.jsonl_path.stat().st_size / (1024*1024):.2f} MB)")
            log.info(f"  - Flattened CSV : {self.csv_path.resolve()} ({self.csv_path.stat().st_size / (1024*1024):.2f} MB)")
            
            # Clean up temp partition files to save disk space
            for name in PARTITIONS.keys():
                part_path = self.output_dir / f"part_{name}.jsonl"
                if part_path.exists():
                    part_path.unlink()
                    
            progress_state["completed"] = True
            self.save_api_progress(progress_state)
            log.info("Cleaned up partition temporary files.")
            return True
            
        except Exception as e:
            log.error(f"Error compiling merged files: {e}", exc_info=True)
            return False

# -----------------------------------------------------------------------------
# Local Search & API Fallback
# -----------------------------------------------------------------------------
def perform_local_search(jsonl_path: Path, query: str):
    """Searches the locally compiled JSONL file for matching records."""
    log.info(f"Searching local file {jsonl_path.name} for query '{query}'...")
    query_upper = query.upper()
    matches = []

    if not jsonl_path.exists():
        log.warning("Local dataset not found. Cannot search locally.")
        return None

    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            record = json.loads(line)
            
            # Match fields
            app_num = record.get("application_number", "").upper()
            sponsor = record.get("sponsor_name", "").upper()
            
            # Match in brand names
            brand_names = []
            for prod in record.get("products", []):
                brand_names.append(prod.get("brand_name", "").upper())
            
            # Match in active ingredients
            ingredients = []
            for prod in record.get("products", []):
                for ing in prod.get("active_ingredients", []):
                    ingredients.append(ing.get("name", "").upper())
            
            # Match logic
            if (query_upper in app_num or 
                query_upper in sponsor or 
                any(query_upper in bn for bn in brand_names) or 
                any(query_upper in ing for ing in ingredients)):
                matches.append(record)
                if len(matches) >= 5:  # Cap at 5 results for readability
                    break
    return matches

def perform_api_search(session: requests.Session, query: str):
    """Queries the live openFDA API for a matching drug record."""
    log.info(f"Querying live openFDA API for query '{query}'...")
    
    # Construct a broad query matching application_number, brand_name, active_ingredients, or sponsor
    # e.g., application_number:ANDA077631 OR sponsor_name:SUN OR products.brand_name:CETIRIZINE
    api_query = (
        f'application_number:"{query}" OR '
        f'sponsor_name:"{query}" OR '
        f'products.brand_name:"{query}" OR '
        f'products.active_ingredients.name:"{query}"'
    )
    params = {
        "search": api_query,
        "limit": 5
    }
    try:
        resp = session.get(API_BASE_URL, params=params, timeout=20)
        if resp.status_code == 200:
            return resp.json().get("results", [])
        elif resp.status_code == 404:
            log.info("No matching records found on the live openFDA API.")
            return []
        else:
            log.error(f"API search failed with status: {resp.status_code}. Response: {resp.text}")
    except Exception as e:
        log.error(f"API search failed with exception: {e}")
    return []

def cleanup_files(output_dir: Path, jsonl_path: Path, progress_path: Path, zip_path: Path = None):
    """Deletes temporary/unneeded files (jsonl, progress JSON, ZIP archive, and log file) after successful completion."""
    # 1. Delete JSONL
    if jsonl_path.exists():
        try:
            jsonl_path.unlink()
            print(f"Cleaned up JSONL file: {jsonl_path}")
        except Exception as e:
            print(f"Error deleting JSONL file: {e}")

    # 2. Delete Progress JSON
    if progress_path.exists():
        try:
            progress_path.unlink()
            print(f"Cleaned up progress file: {progress_path}")
        except Exception as e:
            print(f"Error deleting progress file: {e}")

    # 3. Delete ZIP file if provided
    if zip_path and zip_path.exists():
        try:
            zip_path.unlink()
            print(f"Cleaned up ZIP file: {zip_path}")
        except Exception as e:
            print(f"Error deleting ZIP file: {e}")

    # 4. Shutdown logging so we can delete the log file on Windows
    log_file = output_dir / "openfda_downloader.log"
    if log_file.exists():
        try:
            logging.shutdown()  # Closes and flushes all log handlers
            log_file.unlink()
            print(f"Cleaned up log file: {log_file}")
        except Exception as e:
            print(f"Error deleting log file: {e}")

# -- Main Execution -----------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="openFDA Drugs@FDA Downloader: Scrapes, downloads, and processes FDA-approved drug metadata.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    
    # Mode selection
    parser.add_argument("mode", choices=["download-bulk", "api-harvest", "search"],
                        help="Data collection mode. 'download-bulk' is the recommended full download. "
                             "'api-harvest' crawls the live API. 'search' queries records.")
    
    # Options
    parser.add_argument("-o", "--output-dir", type=str, default=DEFAULT_OUTPUT_DIR,
                        help="Output directory for data, progress, and logs.")
    parser.add_argument("-t", "--threads", type=int, default=4,
                        help="Number of threads for partition API harvesting.")
    parser.add_argument("-d", "--delay", type=float, default=None,
                        help="Polite delay between API requests. Default: 1.5s (public) or 0.25s (with API key).")
    parser.add_argument("-l", "--limit", type=int, default=None,
                        help="Maximum records to harvest (for test runs).")
    parser.add_argument("--api-key", type=str, default=None,
                        help="Optional openFDA API key for higher rate limits (240 requests/min).")
    parser.add_argument("--query", type=str, default=None,
                        help="Query string for search mode (application number, brand, ingredient, or sponsor).")
    parser.add_argument("--no-resume", action="store_true",
                        help="Start download/harvest fresh, ignoring any progress files.")
    
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    setup_logging(output_dir)

    log.info("=" * 70)
    log.info("      openFDA DRUGS@FDA DATA COLLECTOR")
    log.info("=" * 70)
    log.info(f"Mode             : {args.mode}")
    log.info(f"Output Directory : {output_dir.resolve()}")
    
    session = build_session(args.api_key)

    # Resolve delays based on API Key presence
    delay = args.delay
    if delay is None:
        delay = 0.25 if args.api_key else 1.5

    if args.no_resume:
        log.info("Fresh download requested. Progress states and temp files will be ignored.")
        # Clean progress if no-resume
        progress_file = output_dir / "openfda_progress.json"
        if progress_file.exists():
            progress_file.unlink()
        # Clean zip if no-resume
        zip_file = output_dir / "drug-drugsfda-0001-of-0001.json.zip"
        if zip_file.exists() and args.mode == "download-bulk":
            zip_file.unlink()

    downloader = OpenFDADownloader(output_dir, session, delay)

    if args.mode == "download-bulk":
        bulk_url = downloader.fetch_bulk_zip_url()
        success = downloader.download_bulk_zip(bulk_url)
        if success:
            processing_success = downloader.process_bulk_zip()
            if processing_success:
                log.info("Bulk download and compile process completed successfully.")
                cleanup_files(output_dir, downloader.jsonl_path, downloader.progress_path, downloader.zip_path)
                sys.exit(0)
        sys.exit(1)

    elif args.mode == "api-harvest":
        log.info(f"Harvest Delay    : {delay} seconds per request")
        success = downloader.run_api_harvest(limit_per_page=DEFAULT_PAGE_SIZE, max_records_total=args.limit)
        if success:
            log.info("API harvesting and compile process completed successfully.")
            cleanup_files(output_dir, downloader.jsonl_path, downloader.progress_path)
            sys.exit(0)
        sys.exit(1)

    elif args.mode == "search":
        if not args.query:
            log.error("Search mode requires a query string. Use --query <search_term>")
            sys.exit(1)
        
        # 1. Try local search
        local_results = perform_local_search(downloader.jsonl_path, args.query)
        
        # 2. Try API fallback
        if local_results is None:
            log.info("Local dataset not found. Falling back to live openFDA API search...")
            results = perform_api_search(session, args.query)
        elif len(local_results) == 0:
            log.info("No matching records found locally. Falling back to live openFDA API search...")
            results = perform_api_search(session, args.query)
        else:
            results = local_results
            log.info(f"Found {len(results)} matching records locally.")

        if not results:
            log.info("No matching records found.")
            sys.exit(0)

        # Print results beautifully
        print("\n" + "=" * 70)
        print(f" SEARCH RESULTS FOR: {args.query}")
        print("=" * 70)
        for idx, rec in enumerate(results, 1):
            print(f"\n[{idx}] Application Number: {rec.get('application_number')}")
            print(f"    Sponsor Name      : {rec.get('sponsor_name')}")
            
            # Print Products
            print("    Products:")
            for prod in rec.get("products", []):
                active_ing = ", ".join(f"{i.get('name')} ({i.get('strength')})" for i in prod.get("active_ingredients", []))
                print(f"      - Product #{prod.get('product_number')}: {prod.get('brand_name')} | Route: {prod.get('route')} | Status: {prod.get('marketing_status')}")
                print(f"        Active Ingredients: {active_ing}")
                
            # Print Submissions
            submissions = rec.get("submissions", [])
            if submissions:
                latest_sub = submissions[0]
                print(f"    Latest Submission : Type {latest_sub.get('submission_type')} - Status {latest_sub.get('submission_status')} ({latest_sub.get('submission_status_date')})")
            print("-" * 70)

if __name__ == "__main__":
    main()
