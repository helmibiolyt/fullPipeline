#!/usr/bin/env python3
"""
gsrs_downloader.py
==================
Authoritative substance data collector for the FDA Global Substance Registration System (GSRS).
Fetches substance records from the official NCATS GSRS REST API (https://gsrs.ncats.nih.gov/ginas/app/api/v1/substances).

Features:
  1. Full record retrieval: Uses the 'view=full' parameter to retrieve complete nested name and code lists.
  2. Batch-based concurrency: Downloads multiple pages in parallel using a thread pool.
  3. Ordered and safe writing: Processed in batches to guarantee sequence integrity and prevent data gaps.
  4. Dual output formats: Saves to a flattened CSV for easy analysis and a raw JSON lines (.jsonl) file for lossless storage.
  5. State-based resumability: Auto-detects progress and resumes seamlessly from interruptions.
  6. Polite crawling: Includes configurable request delays and retry backoffs to be a good internet citizen.
  7. Clear telemetry: Shows real-time download speed, progress percentage, and estimated time remaining (ETA).
"""

import os
import sys
import time
import json
import csv
import logging
import argparse
from pathlib import Path
import concurrent.futures
from urllib3.util import Retry
import requests
from requests.adapters import HTTPAdapter

# -- Configuration & Constants -----------------------------------------------
# Resolve output paths relative to this script, never the current working dir.
BASE_DIR = Path(__file__).resolve().parent

API_BASE_URL = "https://gsrs.ncats.nih.gov/ginas/app/api/v1/substances"
DEFAULT_PAGE_SIZE = 500
DEFAULT_THREADS = 2
DEFAULT_DELAY = 0.5
DEFAULT_OUTPUT_DIR = str(BASE_DIR / "gsrs_data")

CSV_FIELDS = [
    "uuid",
    "unii",
    "preferred_name",
    "substance_class",
    "status",
    "definition_type",
    "definition_level",
    "deprecated",
    "cas_number",
    "synonyms"
]

# -- Logging Setup -----------------------------------------------------------
log = logging.getLogger("gsrs_downloader")

def setup_logging(output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / "gsrs_downloader.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout)
        ]
    )

# -- HTTP Session Configuration ----------------------------------------------
def build_session(retries: int = 5, backoff_factor: float = 2.0) -> requests.Session:
    """Builds a requests.Session with an HTTPAdapter configured for retries."""
    session = requests.Session()
    retry_strategy = Retry(
        total=retries,
        backoff_factor=backoff_factor,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })
    return session

# -- Data Extraction & Flattening Logic --------------------------------------
def extract_substance_fields(substance: dict) -> dict:
    """Extracts key analytical fields from a raw substance JSON object."""
    # 1. Base Fields
    uuid = substance.get("uuid", "").strip()
    unii = substance.get("approvalID", "").strip()
    preferred_name = substance.get("_name", "").strip()
    substance_class = substance.get("substanceClass", "").strip()
    status = substance.get("status", "").strip()
    definition_type = substance.get("definitionType", "").strip()
    definition_level = substance.get("definitionLevel", "").strip()
    deprecated = bool(substance.get("deprecated", False))

    # 2. CAS Number Extraction (find codeSystem == "CAS")
    cas_numbers = []
    for code_obj in substance.get("codes", []):
        if code_obj.get("codeSystem") == "CAS":
            cas_val = code_obj.get("code", "").strip()
            if cas_val and cas_val not in cas_numbers:
                cas_numbers.append(cas_val)
    cas_number = "|".join(cas_numbers)

    # 3. Synonyms Extraction (names not equal to the preferred name)
    syns = []
    pref_name_upper = preferred_name.upper()
    for name_obj in substance.get("names", []):
        name_str = name_obj.get("name", "").strip()
        if name_str and name_str.upper() != pref_name_upper:
            if name_str not in syns:
                syns.append(name_str)
    synonyms = "|".join(syns)

    return {
        "uuid": uuid,
        "unii": unii,
        "preferred_name": preferred_name,
        "substance_class": substance_class,
        "status": status,
        "definition_type": definition_type,
        "definition_level": definition_level,
        "deprecated": deprecated,
        "cas_number": cas_number,
        "synonyms": synonyms
    }

# -- API Retrieval -----------------------------------------------------------
def fetch_page_data(session: requests.Session, skip: int, top: int, timeout: int = 60) -> dict | None:
    """Fetches a single page of substance records from the GSRS API."""
    params = {
        "top": top,
        "skip": skip,
        "view": "full"
    }
    try:
        resp = session.get(API_BASE_URL, params=params, timeout=timeout)
        if resp.status_code == 200:
            return resp.json()
        log.warning(f"Non-200 response for skip={skip}: HTTP {resp.status_code}. Response: {resp.text[:200]}")
    except Exception as e:
        log.warning(f"Request exception for skip={skip}: {e}")
    return None

# -- Progress & Resumability Management --------------------------------------
def load_progress_state(progress_file: Path, jsonl_file: Path) -> int:
    """Loads the progress state and returns the skip offset to resume from."""
    # Scenario 1: Progress file exists
    if progress_file.is_file():
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            resume_offset = state.get("last_skip", 0)
            log.info(f"Loaded progress state from {progress_file.name}. Resuming from skip={resume_offset}")
            return resume_offset
        except Exception as e:
            log.warning(f"Could not parse progress file {progress_file.name}: {e}. Falling back to file scan.")

    # Scenario 2: Fall back to scanning the JSONL file to count records
    if jsonl_file.is_file() and jsonl_file.stat().st_size > 0:
        log.info(f"Scanning {jsonl_file.name} to count existing records...")
        try:
            record_count = 0
            with open(jsonl_file, "r", encoding="utf-8") as f:
                for _ in f:
                    record_count += 1
            log.info(f"Found {record_count:,} existing records in JSONL. Resuming from skip={record_count}")
            return record_count
        except Exception as e:
            log.warning(f"Error scanning JSONL file: {e}")

    log.info("No previous progress found. Starting a fresh download.")
    return 0

def save_progress_state(progress_file: Path, last_skip: int, total_records: int, completed: bool):
    """Saves the current download progress state to the progress JSON file."""
    state = {
        "last_skip": last_skip,
        "total_records": total_records,
        "completed": completed,
        "timestamp": time.time()
    }
    try:
        with open(progress_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log.warning(f"Could not write progress file: {e}")

# -- Main Execution Pipeline -------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(
        description="GSRS Substance Downloader: Scrapes and stores the full FDA UNII / GSRS database.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("-o", "--output-dir", type=str, default=DEFAULT_OUTPUT_DIR, help="Output directory for data and logs.")
    parser.add_argument("-p", "--page-size", type=int, default=DEFAULT_PAGE_SIZE, help="Number of records to fetch per page.")
    parser.add_argument("-t", "--threads", type=int, default=DEFAULT_THREADS, help="Number of concurrent download threads.")
    parser.add_argument("-d", "--delay", type=float, default=DEFAULT_DELAY, help="Delay in seconds between batch requests.")
    parser.add_argument("-l", "--limit", type=int, default=None, help="Maximum number of records to download (for testing).")
    parser.add_argument("--no-resume", action="store_true", help="Do not resume; overwrite existing output files and start fresh.")
    return parser.parse_args()

def main():
    args = parse_args()
    output_dir = Path(args.output_dir)
    setup_logging(output_dir)

    log.info("=" * 70)
    log.info("      FDA UNII / GSRS SUBSTANCE DOWNLOADER")
    log.info("=" * 70)
    log.info(f"Output Directory : {output_dir.resolve()}")
    log.info(f"Page Size        : {args.page_size}")
    log.info(f"Concurrent Threads: {args.threads}")
    log.info(f"Polite Delay     : {args.delay} seconds")
    if args.limit:
        log.info(f"Download Limit   : {args.limit:,} records")

    csv_path = output_dir / "gsrs_substances.csv"
    jsonl_path = output_dir / "gsrs_substances.jsonl"
    progress_path = output_dir / "gsrs_progress.json"

    session = build_session()

    # 1. Fetch the total count of substance records from the API
    log.info("Connecting to GSRS API to retrieve total substance count...")
    initial_data = fetch_page_data(session, skip=0, top=1)
    if not initial_data:
        log.error("Failed to make initial connection to the GSRS API. Please check your internet connection. Aborting.")
        sys.exit(1)

    total_api_records = initial_data.get("total", 0)
    log.info(f"GSRS API reports a total of {total_api_records:,} substance records available.")

    # 2. Handle Resumability / Offset Determination
    if args.no_resume:
        log.info("Fresh download requested. Overwriting any existing files.")
        start_offset = 0
        file_mode = "w"
    else:
        start_offset = load_progress_state(progress_path, jsonl_path)
        file_mode = "a" if start_offset > 0 else "w"
        # The progress file survives the pipeline's post-commit wipe but the CSV
        # does not, so it is restored by the `hydrate` step. If it is absent we
        # would append to nothing and emit a headerless, partial dataset — start
        # over instead, which is slower but always correct.
        if start_offset > 0 and not csv_path.exists():
            log.warning("Resume offset %s but %s is missing — restarting from 0.",
                        f"{start_offset:,}", csv_path.name)
            start_offset = 0
            file_mode = "w"

    target_total = min(args.limit, total_api_records) if args.limit is not None else total_api_records

    if start_offset >= target_total:
        log.info(f"Target record count ({target_total:,}) has already been reached. Nothing to download.")
        save_progress_state(progress_path, start_offset, total_api_records, completed=True)
        sys.exit(0)

    # 3. Initialize Output Files
    write_header = (file_mode == "w")
    
    csv_file = open(csv_path, file_mode, newline="", encoding="utf-8")
    csv_writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
    if write_header:
        csv_writer.writeheader()
        csv_file.flush()

    jsonl_file = open(jsonl_path, file_mode, encoding="utf-8")

    log.info(f"Starting download loop from skip={start_offset:,} to {target_total:,}...")
    
    current_offset = start_offset
    session_start_time = time.time()
    records_downloaded_session = 0

    # 4. Thread Pool Concurrency and Batch Loop
    # We download pages in batches of size `args.threads`. This guarantees we process pages
    # in parallel, but write and commit them to disk in strict sequence.
    batch_size = args.threads
    page_size = args.page_size

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.threads) as executor:
        while current_offset < target_total:
            batch_offsets = []
            # Calculate offsets for the current batch
            temp_offset = current_offset
            while temp_offset < target_total and len(batch_offsets) < batch_size:
                # Calculate the exact number of records to request for this page
                remaining = target_total - temp_offset
                page_limit = min(page_size, remaining)
                batch_offsets.append((temp_offset, page_limit))
                temp_offset += page_limit

            log.info(f"Submitting batch of {len(batch_offsets)} page requests starting at skip={current_offset:,}...")
            
            # Submit batch to ThreadPoolExecutor
            future_to_offset = {
                executor.submit(fetch_page_data, session, skip, limit): (skip, limit)
                for skip, limit in batch_offsets
            }

            # Gather results in order of offset
            batch_results = {}
            failed_offset = None
            
            for future in concurrent.futures.as_completed(future_to_offset):
                skip, limit = future_to_offset[future]
                try:
                    res_json = future.result()
                    if res_json and "content" in res_json:
                        batch_results[skip] = res_json["content"]
                    else:
                        log.error(f"Page fetch returned empty or invalid data for skip={skip}")
                        failed_offset = skip
                except Exception as exc:
                    log.error(f"Page fetch generated an exception for skip={skip}: {exc}")
                    failed_offset = skip

            # If any page in the batch failed to download after all session retries, we abort to preserve state integrity.
            if failed_offset is not None:
                log.error(f"Download batch failed at skip={failed_offset}. Saving progress and exiting.")
                break

            # Process and write the batch results in strict ascending order to guarantee no gaps in output files
            success = True
            for skip, limit in batch_offsets:
                substances_list = batch_results.get(skip)
                if substances_list is None:
                    log.error(f"Critical error: missing batch data in results dictionary for skip={skip}")
                    success = False
                    break

                # Write to files
                for sub_json in substances_list:
                    # Write Raw JSONL
                    jsonl_file.write(json.dumps(sub_json) + "\n")
                    
                    # Write Flat CSV
                    flat_fields = extract_substance_fields(sub_json)
                    csv_writer.writerow(flat_fields)

                records_downloaded_session += len(substances_list)
                current_offset += len(substances_list)

            # Flush writers to prevent data loss on unexpected OS crashes
            csv_file.flush()
            jsonl_file.flush()

            if not success:
                break

            # Save progress after a successful batch
            save_progress_state(progress_path, current_offset, total_api_records, completed=(current_offset >= target_total))

            # Calculate and display telemetry
            elapsed_session = time.time() - session_start_time
            avg_speed = records_downloaded_session / elapsed_session if elapsed_session > 0 else 0
            pct_complete = (current_offset / target_total) * 100
            
            remaining_records = target_total - current_offset
            eta_seconds = remaining_records / avg_speed if avg_speed > 0 else 0
            
            if eta_seconds > 3600:
                eta_str = f"{eta_seconds / 3600:.2f} hours"
            elif eta_seconds > 60:
                eta_str = f"{eta_seconds / 60:.2f} minutes"
            else:
                eta_str = f"{eta_seconds:.0f} seconds"

            log.info(
                f"Progress: {current_offset:,} / {target_total:,} ({pct_complete:.2f}%) | "
                f"Session Speed: {avg_speed:.1f} recs/s | "
                f"ETA: {eta_str}"
            )

            # Polite throttling delay between batches
            if current_offset < target_total and args.delay > 0:
                time.sleep(args.delay)

    # 5. Cleanup & Final Status Report
    csv_file.close()
    jsonl_file.close()

    total_downloaded = current_offset - start_offset
    total_elapsed = time.time() - session_start_time
    log.info("=" * 70)
    if current_offset >= target_total:
        log.info("      DOWNLOAD COMPLETED SUCCESSFULLY")
        log.info("=" * 70)
        log.info(f"Total Substances Downloaded: {current_offset:,}")
        log.info(f"CSV Dataset Saved to       : {csv_path.name}")
        # Clean up JSONL and progress files upon completion
        try:
            if jsonl_path.exists():
                jsonl_path.unlink()
                log.info(f"Cleaned up intermediate raw file: {jsonl_path.name}")
            if progress_file.exists():
                progress_file.unlink()
                log.info(f"Cleaned up progress file: {progress_file.name}")
        except Exception as e:
            log.warning(f"Could not clean up intermediate JSON files: {e}")
    else:
        log.warning("      DOWNLOAD INTERRUPTED / TERMINATED PREMATURELY")
        log.warning("=" * 70)
        log.warning(f"Records scraped in this session: {records_downloaded_session:,}")
        log.warning(f"Current progress offset        : {current_offset:,}")
        log.warning("You can run the script again to resume downloading from this point.")
    
    log.info(f"Session download count         : {records_downloaded_session:,}")
    log.info(f"Session elapsed time           : {total_elapsed:.1f} seconds")
    log.info("=" * 70)

    # The GSRS API gateway-times-out (504) on deep pagination, so a single run
    # legitimately may not reach the end of 173k records. A run that ADVANCED is
    # still publishable: the CSV is hydrated then appended to, so its output is a
    # strict superset of what is live, and successive runs converge.
    # A run that fetched nothing, however, is a real failure and must not be
    # reported as success — that is what silently published a 621 KB dataset.
    if records_downloaded_session == 0:
        log.error("No records downloaded this session — failing so the run is retried.")
        sys.exit(1)

if __name__ == "__main__":
    main()
