#!/usr/bin/env python3
"""
ctri_downloader.py
==================
An intelligent, high-performance, and resilient utility to download and parse 
clinical trial registration data from the Clinical Trials Registry - India (CTRI) 
website: https://ctri.nic.in

Features:
  1. CAPTCHA Bypass: Accesses trial details pages directly using base64-encoded 
     database IDs, bypassing the main search form's CSRF/CAPTCHA controls.
  2. Resiliency & Resume Support: Saves raw HTML files locally. Existing files 
     are skipped on startup. Invalid trial IDs are saved as dummy placeholder 
     files to prevent redundant future network requests.
  3. Concurrent Operations: Uses ThreadPoolExecutor for rapid downloading. 
     Thread count, range limits, retries, and timeouts are fully configurable.
  4. Robust Parsing: Handles nested key-value tables, tabular lists, commented 
     columns, and summary headers using BeautifulSoup, converting raw HTML into 
     structured JSON representation.
  5. Consolidated Output: Parses local HTML caches into structured JSON Lines (.jsonl) 
     and flattened, analysis-ready CSV formats.

Requirements:
  pip install requests beautifulsoup4

Usage:
  python ctri_downloader.py download --start 1 --end 1000 --workers 10
  python ctri_downloader.py parse --html-dir Clinical Trials & Pipeline Intelligence/ctri_trials/html
"""

import os
import sys
import time
import base64
import re
import json
import csv
import argparse
import logging
import traceback
import queue
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from bs4 import BeautifulSoup

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("ctri_collector")

# Resolve all output paths relative to this script's folder
BASE_DIR = Path(__file__).resolve().parent

# Base configurations
BASE_URL = "https://ctri.nic.in/Clinicaltrials/pmaindet2.php"
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive"
}

# ==============================================================================
# DOWNLOAD & STREAMING COMPONENT
# ==============================================================================

def load_completed_ids(jsonl_path, csv_path, invalid_ids_path):
    """
    Loads already processed and invalid trial IDs to skip them on resume.
    """
    completed = set()
    
    # 1. Load from JSONL (primary source of truth)
    if jsonl_path.exists():
        try:
            with open(jsonl_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            record = json.loads(line)
                            if "db_id" in record:
                                completed.add(int(record["db_id"]))
                        except Exception:
                            pass
            logger.info(f"Loaded {len(completed)} completed trial IDs from {jsonl_path}")
        except Exception as e:
            logger.warning(f"Error reading completed IDs from {jsonl_path}: {e}")
            
    # 2. Load from CSV (fallback)
    if len(completed) == 0 and csv_path.exists():
        try:
            with open(csv_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if "db_id" in row and row["db_id"]:
                        try:
                            completed.add(int(row["db_id"]))
                        except Exception:
                            pass
            logger.info(f"Loaded {len(completed)} completed trial IDs from {csv_path}")
        except Exception as e:
            logger.warning(f"Error reading completed IDs from {csv_path}: {e}")

    # 3. Load from invalid_ids.txt
    invalid_count = 0
    if invalid_ids_path.exists():
        try:
            with open(invalid_ids_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            val = int(line)
                            completed.add(val)
                            invalid_count += 1
                        except Exception:
                            pass
            logger.info(f"Loaded {invalid_count} invalid trial IDs from {invalid_ids_path}")
        except Exception as e:
            logger.warning(f"Error reading invalid IDs from {invalid_ids_path}: {e}")
            
    return completed


def download_trial(trial_id, html_dir, session, invalid_lock, output_dir, timeout=15, max_retries=5):
    """
    Downloads a single trial detail page.
    Returns:
        status: 'downloaded', 'skipped', 'invalid', or 'failed'
    """
    file_path = html_dir / f"trial_{trial_id}.html"
    
    # Check if already downloaded on disk (e.g. from existing files in the workspace)
    if file_path.exists() and file_path.stat().st_size > 0:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                first_chars = f.read(10)
            if "INVALID" in first_chars:
                return "invalid"
        except Exception:
            pass
        return "skipped"

    # Encode trial ID to Base64
    id_str = str(trial_id)
    enc_id = base64.b64encode(id_str.encode("utf-8")).decode("utf-8")
    url = f"{BASE_URL}?EncHid={enc_id}"

    attempt = 0
    backoff = 2
    
    while attempt < max_retries:
        attempt += 1
        try:
            resp = session.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
            
            if resp.status_code == 429:
                sleep_time = backoff * 5
                logger.warning(f"[ID {trial_id}] Rate limited (429). Sleeping {sleep_time}s... (Attempt {attempt}/{max_retries})")
                time.sleep(sleep_time)
                continue
                
            if resp.status_code != 200:
                logger.warning(f"[ID {trial_id}] HTTP error {resp.status_code} (Attempt {attempt}/{max_retries})")
                time.sleep(backoff)
                backoff *= 2
                continue
            
            html = resp.text
            
            # Check if the page represents an invalid ID
            if "Invalid Request" in html or "invalid" in html or "window.location" in html or ("CTRI/" not in html and "CTRI Number" not in html):
                with invalid_lock:
                    with open(output_dir / "invalid_ids.txt", "a") as f:
                        f.write(f"{trial_id}\n")
                return "invalid"
            
            # Save the valid HTML content temporarily
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(html)
            return "downloaded"
            
        except requests.exceptions.RequestException as e:
            logger.warning(f"[ID {trial_id}] Network error: {e} (Attempt {attempt}/{max_retries})")
            time.sleep(backoff)
            backoff *= 2
            
    return "failed"


def consumer_worker(download_queue, csv_path, jsonl_path, stats, stats_lock):
    """
    Consumer thread: pulls downloaded HTML paths from the queue,
    parses them, appends to JSONL and CSV, and deletes the HTML files.
    """
    logger.info("Consumer thread started.")
    
    # Initialize files if they don't exist
    csv_exists = csv_path.exists() and csv_path.stat().st_size > 0
    
    # Fieldnames for CSV output
    sample_flat = flatten_trial_record({})
    csv_fieldnames = list(sample_flat.keys())
    
    try:
        with open(jsonl_path, "a", encoding="utf-8") as jsonl_f, \
             open(csv_path, "a", encoding="utf-8", newline="") as csv_f:
             
            csv_writer = csv.DictWriter(csv_f, fieldnames=csv_fieldnames)
            if not csv_exists:
                csv_writer.writeheader()
                csv_f.flush()
                
            while True:
                file_path = download_queue.get()
                if file_path is None:
                    download_queue.task_done()
                    break
                    
                try:
                    if file_path.exists():
                        with open(file_path, "r", encoding="utf-8") as f:
                            content = f.read()
                            
                        # Delete HTML file immediately to save space
                        file_path.unlink()
                        
                        if content.strip() == "INVALID":
                            continue
                            
                        record = parse_ctri_html(content, file_name=file_path.name)
                        if record:
                            # Write to JSONL
                            jsonl_f.write(json.dumps(record) + "\n")
                            jsonl_f.flush()
                            
                            # Flatten and write to CSV
                            flat_record = flatten_trial_record(record)
                            csv_writer.writerow(flat_record)
                            csv_f.flush()
                            
                            with stats_lock:
                                stats["parsed"] += 1
                        
                except Exception as e:
                    logger.error(f"Error in consumer processing {file_path.name if file_path else 'unknown'}: {e}")
                finally:
                    download_queue.task_done()
                    
    except Exception as e:
        logger.critical(f"Fatal error in consumer thread: {e}")
        
    logger.info("Consumer thread stopped.")


def run_downloader(args):
    """
    Main download loop running multi-threaded fetches and real-time parsing/cleanup.
    """
    start_id = args.start
    end_id = args.end
    workers = args.workers
    timeout = args.timeout
    retries = args.retries
    
    output_dir = Path(args.output_dir)
    html_dir = output_dir / "html"
    html_dir.mkdir(parents=True, exist_ok=True)
    
    csv_path = output_dir / "ctri_trials.csv"
    jsonl_path = output_dir / "ctri_trials.jsonl"
    invalid_ids_path = output_dir / "invalid_ids.txt"
    
    logger.info("=" * 60)
    logger.info("  Clinical Trials Registry - India (CTRI) Streaming Pipeline")
    logger.info(f"  Target Range : {start_id} to {end_id}")
    logger.info(f"  Workers      : {workers} threads")
    logger.info(f"  Output Dir   : {output_dir}")
    logger.info(f"  Output CSV   : {csv_path}")
    logger.info(f"  Output JSONL : {jsonl_path}")
    logger.info("=" * 60)

    # 1. Load progress
    completed_ids = load_completed_ids(jsonl_path, csv_path, invalid_ids_path)
    
    # 2. Setup Queue and Locks
    download_queue = queue.Queue(maxsize=100)
    invalid_lock = threading.Lock()
    stats_lock = threading.Lock()
    
    # Track statistics
    stats = {
        "downloaded": 0,
        "skipped": 0,
        "invalid": 0,
        "failed": 0,
        "parsed": 0
    }
    
    failed_ids = []
    
    # Start Consumer Thread
    consumer = threading.Thread(
        target=consumer_worker,
        args=(download_queue, csv_path, jsonl_path, stats, stats_lock),
        daemon=True
    )
    consumer.start()
    
    # Filter IDs
    trial_ids = [tid for tid in range(start_id, end_id + 1) if tid not in completed_ids]
    total_ids = len(trial_ids)
    
    logger.info(f"Completed/Skipped: {len(completed_ids)} IDs | Remaining to process: {total_ids}")
    
    if total_ids == 0:
        logger.info("All IDs in range are already processed. Exiting.")
        download_queue.put(None)
        consumer.join()
        return

    session = requests.Session()
    logger.info(f"Starting downloader and parser pipeline...")
    
    start_time = time.time()
    
    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_id = {}
            for tid in trial_ids:
                # Check if file already exists on disk
                file_path = html_dir / f"trial_{tid}.html"
                if file_path.exists() and file_path.stat().st_size > 0:
                    stats["skipped"] += 1
                    download_queue.put(file_path)
                else:
                    future = executor.submit(
                        download_trial, tid, html_dir, session, invalid_lock, output_dir, timeout, retries
                    )
                    future_to_id[future] = tid
            
            processed = 0
            total_tasks = len(future_to_id)
            
            if total_tasks > 0:
                for future in as_completed(future_to_id):
                    tid = future_to_id[future]
                    processed += 1
                    try:
                        status = future.result()
                        stats[status] += 1
                        
                        if status == "downloaded":
                            file_path = html_dir / f"trial_{tid}.html"
                            download_queue.put(file_path)
                        elif status == "failed":
                            failed_ids.append(tid)
                            
                        # Log progress
                        if processed % 100 == 0 or processed == total_tasks:
                            elapsed = time.time() - start_time
                            rate = processed / elapsed if elapsed > 0 else 0
                            logger.info(
                                f"Progress: {processed}/{total_tasks} ({processed/total_tasks*100:.1f}%) | "
                                f"Downloaded: {stats['downloaded']} | Skipped: {stats['skipped']} | "
                                f"Invalid: {stats['invalid']} | Failed: {stats['failed']} | "
                                f"Parsed: {stats['parsed']} | Speed: {rate:.1f} ids/sec"
                            )
                            
                    except Exception as exc:
                        logger.error(f"[ID {tid}] Generated an unhandled exception: {exc}")
                        stats["failed"] += 1
                        failed_ids.append(tid)
                        
    except KeyboardInterrupt:
        logger.warning("KeyboardInterrupt received. Stopping threads...")
    finally:
        # Stop consumer cleanly
        download_queue.put(None)
        logger.info("Waiting for consumer thread to finish writing remaining queue...")
        consumer.join()
        
    elapsed_total = time.time() - start_time
    logger.info("=" * 60)
    logger.info("  Streaming Session Completed!")
    logger.info(f"  Total Time   : {elapsed_total/60:.2f} minutes")
    logger.info(f"  Downloaded   : {stats['downloaded']}")
    logger.info(f"  Skipped      : {stats['skipped']}")
    logger.info(f"  Invalid/Gaps : {stats['invalid']}")
    logger.info(f"  Failed       : {stats['failed']}")
    logger.info(f"  Total Parsed : {stats['parsed']}")
    logger.info("=" * 60)
    
    if failed_ids:
        failed_log_path = output_dir / "failed_ids.txt"
        with open(failed_log_path, "w") as f:
            for fid in sorted(failed_ids):
                f.write(f"{fid}\n")
        logger.warning(f"Failed trial IDs written to: {failed_log_path}")


# ==============================================================================
# PARSING & COMPILATION COMPONENT
# ==============================================================================

def clean_text(text):
    if not text:
        return ""
    # Clean whitespace, unicode spaces, newlines
    text = text.replace("\xa0", " ").replace("\r", " ").replace("\n", " ")
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def parse_kv_table(table):
    """
    Parses a key-value style table.
    """
    data = {}
    for tr in table.find_all("tr", recursive=True):
        tds = tr.find_all("td", recursive=False)
        if len(tds) == 2:
            key = clean_text(tds[0].get_text())
            key = re.sub(r'[:\s]+$', '', key).lower().replace(" ", "_")
            val = clean_text(tds[1].get_text())
            data[key] = val
    return data


def parse_header_table(table):
    """
    Parses a tabular table with header and rows. 
    Correctly identifies shifts due to title summary rows (e.g. "No of Sites = 1").
    """
    rows = table.find_all("tr", recursive=True)
    if not rows:
        return []
    
    # Determine the actual header row
    header_idx = 0
    if len(rows) > 1:
        cells_r0 = len(rows[0].find_all(["td", "th"], recursive=False))
        cells_r1 = len(rows[1].find_all(["td", "th"], recursive=False))
        # If the first row is a summary title (1 column) and row 1 has multiple columns, row 1 is the header
        if cells_r0 == 1 and cells_r1 > 1:
            header_idx = 1
            
    header_tr = rows[header_idx]
    headers = []
    for td in header_tr.find_all(["td", "th"]):
        header_text = clean_text(td.get_text())
        header_name = re.sub(r'[^a-zA-Z0-9_\s]', '', header_text).strip().lower().replace(" ", "_")
        headers.append(header_name)
        
    data_list = []
    for tr in rows[header_idx + 1:]:
        tds = tr.find_all("td", recursive=False)
        # Skip commented columns or summary row dividers
        if len(tds) == 1 and len(headers) > 1:
            continue
        if len(tds) == len(headers):
            row_data = {}
            for i, td in enumerate(tds):
                row_data[headers[i]] = clean_text(td.get_text())
            data_list.append(row_data)
        elif len(tds) > 0:
            row_text = " | ".join([clean_text(td.get_text()) for td in tds])
            data_list.append({"raw_row": row_text})
            
    return data_list


def parse_simple_list(table):
    """
    Parses a single-column list.
    """
    items = []
    for td in table.find_all("td"):
        text = clean_text(td.get_text())
        if text and text not in ["NIL", "None", "N/A"]:
            items.append(text)
    return items


def parse_ctri_html(html_content, file_name=""):
    """
    Main parser extracting all fields from trial HTML content.
    """
    if not html_content or html_content.strip() == "INVALID":
        return None
        
    soup = BeautifulSoup(html_content, "html.parser")
    
    # Verify the page contains the main table
    main_table = soup.find("table", {"bordercolor": "lightgrey"})
    if not main_table:
        main_table = soup.find("table", {"border": "1"})
        
    if not main_table:
        return None
        
    trial_data = {}
    
    # Parse source file information
    match_id = re.search(r"trial_(\d+)\.html", file_name)
    if match_id:
        trial_data["db_id"] = int(match_id.group(1))
        
    rows = main_table.find_all("tr", recursive=False)
    for tr in rows:
        tds = tr.find_all("td", recursive=False)
        if len(tds) != 2:
            continue
            
        label_cell, value_cell = tds[0], tds[1]
        label = clean_text(label_cell.get_text())
        
        # Clean label key
        key = re.sub(r'[^a-zA-Z0-9_\s]', '', label).strip().lower().replace(" ", "_")
        if not key:
            continue
            
        # Check for nested tables
        nested_table = value_cell.find("table")
        if nested_table:
            # Classify table based on key
            if key in [
                "details_of_principal_investigator_or_overall_trial_coordinator_multicenter_study",
                "details_of_contact_person_scientific_query",
                "details_of_contact_personscientific_query",
                "details_of_contact_person_public_query",
                "details_of_contact_personpublic_query",
                "primary_sponsor",
                "inclusion_criteria",
                "exclusion_criteria",
                "exclusioncriteria"
            ]:
                # Force key normalization for query contacts and criteria
                norm_key = key
                if "scientific" in key:
                    norm_key = "details_of_contact_person_scientific_query"
                elif "public" in key:
                    norm_key = "details_of_contact_person_public_query"
                elif "exclusion" in key:
                    norm_key = "exclusion_criteria"
                    
                trial_data[norm_key] = parse_kv_table(nested_table)
                
            elif key in [
                "secondary_ids_if_any",
                "details_of_secondary_sponsor",
                "sites_of_study",
                "details_of_ethics_committee",
                "regulatory_clearance_status_from_dcgi",
                "intervention_comparator_agent",
                "primary_outcome",
                "secondary_outcome",
                "health_condition_problems_studied",
                "health_condition__problems_studied"
            ]:
                norm_key = key
                if "health_condition" in key:
                    norm_key = "health_condition_problems_studied"
                    
                trial_data[norm_key] = parse_header_table(nested_table)
                
            elif key in [
                "source_of_monetary_or_material_support"
            ]:
                trial_data[key] = parse_simple_list(nested_table)
            else:
                # Fallback heuristic
                first_tr = nested_table.find("tr")
                if first_tr and (first_tr.get("bgcolor") or first_tr.find("th")):
                    trial_data[key] = parse_header_table(nested_table)
                else:
                    trial_data[key] = parse_kv_table(nested_table)
        else:
            # Simple text value
            val = clean_text(value_cell.get_text())
            trial_data[key] = val
            
            # Special parsing: extract details from the CTRI Number cell
            if key == "ctri_number":
                match_reg = re.search(r"\[Registered on:\s*([^\]]+)\]", val)
                if match_reg:
                    trial_data["registration_date"] = match_reg.group(1).strip()
                
                if "prospectively" in val.lower():
                    trial_data["registration_type"] = "Prospective"
                elif "retrospectively" in val.lower():
                    trial_data["registration_type"] = "Retrospective"
                else:
                    trial_data["registration_type"] = "Unknown"
                    
                match_ctri = re.search(r"CTRI/\d{4}/\d{2}/\d+", val)
                if match_ctri:
                    trial_data["ctri_id"] = match_ctri.group(0)
                    
    return trial_data


def flatten_trial_record(record):
    """
    Flattens nested structures in a parsed trial record to write to a flat CSV.
    """
    flat = {}
    
    # 1. Direct key-value strings
    flat["db_id"] = record.get("db_id", "")
    flat["ctri_id"] = record.get("ctri_id", "")
    flat["ctri_number"] = record.get("ctri_number", "")
    flat["registration_date"] = record.get("registration_date", "")
    flat["registration_type"] = record.get("registration_type", "")
    flat["last_modified_on"] = record.get("last_modified_on", "")
    flat["post_graduate_thesis"] = record.get("post_graduate_thesis", "")
    flat["type_of_trial"] = record.get("type_of_trial", "")
    flat["type_of_study"] = record.get("type_of_study", "")
    flat["study_design"] = record.get("study_design", "")
    flat["public_title_of_study"] = record.get("public_title_of_study", "")
    flat["scientific_title_of_study"] = record.get("scientific_title_of_study", "")
    flat["trial_acronym"] = record.get("trial_acronym", "")
    flat["countries_of_recruitment"] = record.get("countries_of_recruitment", "")
    flat["method_of_generating_random_sequence"] = record.get("method_of_generating_random_sequence", "")
    flat["method_of_concealment"] = record.get("method_of_concealment", "")
    flat["blindingmasking"] = record.get("blindingmasking", "")
    flat["target_sample_size"] = record.get("target_sample_size", "")
    
    # 2. Principal Investigator Details (Nested Dict)
    pi = record.get("details_of_principal_investigator_or_overall_trial_coordinator_multicenter_study", {})
    flat["pi_name"] = pi.get("name", "")
    flat["pi_designation"] = pi.get("designation", "")
    flat["pi_affiliation"] = pi.get("affiliation", "")
    flat["pi_address"] = pi.get("address", "")
    flat["pi_phone"] = pi.get("phone", "")
    flat["pi_email"] = pi.get("email", "")
    
    # 3. Contact Person Scientific Query (Nested Dict)
    c_sci = record.get("details_of_contact_person_scientific_query", {})
    flat["contact_scientific_name"] = c_sci.get("name", "")
    flat["contact_scientific_designation"] = c_sci.get("designation", "")
    flat["contact_scientific_affiliation"] = c_sci.get("affiliation", "")
    flat["contact_scientific_address"] = c_sci.get("address", "")
    flat["contact_scientific_phone"] = c_sci.get("phone", "")
    flat["contact_scientific_email"] = c_sci.get("email", "")
    
    # 4. Contact Person Public Query (Nested Dict)
    c_pub = record.get("details_of_contact_person_public_query", {})
    flat["contact_public_name"] = c_pub.get("name", "")
    flat["contact_public_designation"] = c_pub.get("designation", "")
    flat["contact_public_affiliation"] = c_pub.get("affiliation", "")
    flat["contact_public_address"] = c_pub.get("address", "")
    flat["contact_public_phone"] = c_pub.get("phone", "")
    flat["contact_public_email"] = c_pub.get("email", "")
    
    # 5. Primary Sponsor (Nested Dict)
    sp = record.get("primary_sponsor", {})
    flat["primary_sponsor_name"] = sp.get("name", "")
    flat["primary_sponsor_address"] = sp.get("address", "")
    flat["primary_sponsor_type"] = sp.get("type_of_sponsor", "")
    
    # 6. Inclusion Criteria (Nested Dict)
    inc = record.get("inclusion_criteria", {})
    flat["inclusion_age_from"] = inc.get("age_from", "")
    flat["inclusion_age_to"] = inc.get("age_to", "")
    flat["inclusion_gender"] = inc.get("gender", "")
    flat["inclusion_details"] = inc.get("details", "")
    
    # 7. Exclusion Criteria (Nested Dict)
    exc = record.get("exclusion_criteria", {})
    flat["exclusion_details"] = exc.get("details", "")
    
    # 8. Lists of Dicts & Custom Lists (Serialized to JSON strings in CSV)
    flat["secondary_ids"] = json.dumps(record.get("secondary_ids_if_any", []))
    flat["source_of_monetary_support"] = json.dumps(record.get("source_of_monetary_or_material_support", []))
    flat["secondary_sponsors"] = json.dumps(record.get("details_of_secondary_sponsor", []))
    flat["sites_of_study"] = json.dumps(record.get("sites_of_study", []))
    flat["ethics_committees"] = json.dumps(record.get("details_of_ethics_committee", []))
    flat["regulatory_clearance_status"] = json.dumps(record.get("regulatory_clearance_status_from_dcgi", []))
    flat["health_conditions"] = json.dumps(record.get("health_condition_problems_studied", []))
    flat["interventions"] = json.dumps(record.get("intervention__comparator_agent", []))
    flat["primary_outcomes"] = json.dumps(record.get("primary_outcome", []))
    flat["secondary_outcomes"] = json.dumps(record.get("secondary_outcome", []))
    
    return flat


def run_parser(args):
    """
    Main compilation loop scanning, parsing, and exporting cached HTMLs.
    """
    html_dir = Path(args.html_dir)
    csv_path = Path(args.output_csv)
    jsonl_path = Path(args.output_jsonl)
    
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    
    logger.info("=" * 60)
    logger.info("  Clinical Trials Registry - India (CTRI) Parser & Compiler")
    logger.info(f"  Source HTML Dir : {html_dir}")
    logger.info(f"  Output CSV      : {csv_path}")
    logger.info(f"  Output JSONL    : {jsonl_path}")
    logger.info("=" * 60)
    
    if not html_dir.exists():
        logger.error(f"HTML directory does not exist: {html_dir}")
        sys.exit(1)
        
    html_files = list(html_dir.glob("trial_*.html"))
    total_files = len(html_files)
    logger.info(f"Found {total_files} local trial HTML cache files to parse.")
    
    if total_files == 0:
        logger.warning("No files found to parse. Exiting.")
        return
        
    parsed_records = []
    parsed_count = 0
    skipped_invalid = 0
    
    logger.info("Parsing HTML caches...")
    start_time = time.time()
    
    # Fieldnames for CSV output
    sample_flat = flatten_trial_record({})
    csv_fieldnames = list(sample_flat.keys())
    
    with open(jsonl_path, "w", encoding="utf-8") as jsonl_f, \
         open(csv_path, "w", encoding="utf-8", newline="") as csv_f:
         
        csv_writer = csv.DictWriter(csv_f, fieldnames=csv_fieldnames)
        csv_writer.writeheader()
        
        for idx, file_path in enumerate(html_files, 1):
            try:
                # Read content
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()
                    
                if content.strip() == "INVALID":
                    skipped_invalid += 1
                    continue
                    
                record = parse_ctri_html(content, file_name=file_path.name)
                if not record:
                    skipped_invalid += 1
                    continue
                
                # Write to JSONL
                jsonl_f.write(json.dumps(record) + "\n")
                
                # Flatten and write to CSV
                flat_record = flatten_trial_record(record)
                csv_writer.writerow(flat_record)
                
                parsed_count += 1
                
                if parsed_count % 1000 == 0 or idx == total_files:
                    elapsed = time.time() - start_time
                    rate = idx / elapsed if elapsed > 0 else 0
                    logger.info(
                        f"Parsed: {idx}/{total_files} ({idx/total_files*100:.1f}%) | "
                        f"Exported: {parsed_count} | Invalid Skipped: {skipped_invalid} | "
                        f"Speed: {rate:.1f} files/sec"
                    )
                    
            except Exception as e:
                logger.error(f"Error parsing file {file_path.name}: {e}")
                logger.debug(traceback.format_exc())
                
    # Clean up intermediate JSONL file upon completing CSV export
    try:
        if jsonl_path.exists():
            jsonl_path.unlink()
            logger.info(f"Cleaned up intermediate JSONL file: {jsonl_path.name}")
    except Exception as e:
        logger.warning(f"Could not remove {jsonl_path}: {e}")

    elapsed_total = time.time() - start_time
    logger.info("=" * 60)
    logger.info("  Parsing & Compilation Completed!")
    logger.info(f"  Total Time   : {elapsed_total:.2f} seconds")
    logger.info(f"  Total Scanned: {total_files}")
    logger.info(f"  Exported     : {parsed_count} clinical trials")
    logger.info(f"  Gaps Skipped : {skipped_invalid}")
    logger.info(f"  Outputs      : [CSV] {csv_path}")
    logger.info("=" * 60)


# ==============================================================================
# CLI ENTRY POINT
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Intelligent and resilient clinical trial data collector for the Indian Clinical Registry (CTRI)."
    )
    subparsers = parser.add_subparsers(dest="command", required=True, help="Subcommand to run")
    
    # Download subcommand
    dl_parser = subparsers.add_parser("download", help="Concurrently download trial HTML detail pages")
    dl_parser.add_argument("--start", type=int, default=1, help="Start trial database ID (default: 1)")
    dl_parser.add_argument("--end", type=int, default=140300, help="End trial database ID (default: 140300)")
    dl_parser.add_argument("--workers", type=int, default=5, help="Number of parallel thread workers (default: 5)")
    dl_parser.add_argument("--output-dir", type=str, default=str(BASE_DIR / "ctri_trials"), help="Root folder for downloaded cache (default: <script_dir>/ctri_trials)")
    dl_parser.add_argument("--timeout", type=int, default=15, help="Connection and read timeout in seconds (default: 15)")
    dl_parser.add_argument("--retries", type=int, default=5, help="Max retries per request with exponential backoff (default: 5)")
    
    # Parse subcommand
    pr_parser = subparsers.add_parser("parse", help="Compile downloaded HTML files into structured CSV & JSONL")
    pr_parser.add_argument("--html-dir", type=str, default=str(BASE_DIR / "ctri_trials" / "html"), help="Path to local HTML folder (default: <script_dir>/ctri_trials/html)")
    pr_parser.add_argument("--output-csv", type=str, default=str(BASE_DIR / "ctri_trials" / "ctri_trials.csv"), help="Consolidated CSV file output (default: <script_dir>/ctri_trials/ctri_trials.csv)")
    pr_parser.add_argument("--output-jsonl", type=str, default=str(BASE_DIR / "ctri_trials" / "ctri_trials.jsonl"), help="Consolidated JSON Lines file output (default: <script_dir>/ctri_trials/ctri_trials.jsonl)")
    
    args = parser.parse_args()
    
    if args.command == "download":
        run_downloader(args)
    elif args.command == "parse":
        run_parser(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
