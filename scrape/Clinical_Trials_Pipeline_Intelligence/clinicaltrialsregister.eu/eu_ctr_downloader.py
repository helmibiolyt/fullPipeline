#!/usr/bin/env python3
"""
eu_ctr_downloader.py
====================
Downloads full clinical trial details from the EU Clinical Trials Register (clinicaltrialsregister.eu)
by iterating through search pages and downloading the text files directly.

Features:
- Auto-detects total page count dynamically.
- Resumes downloads (skips already downloaded pages).
- Retries with exponential backoff on HTTP/network errors.
- Respectful delays between downloads to prevent getting blocked.
- Combines all downloaded files into a consolidated text file.

Usage:
  python eu_ctr_downloader.py --limit 5
  python eu_ctr_downloader.py --delay 2.0 --output-dir my_trials --merge
"""

import os
import sys
import time
import re
import csv
import argparse
from concurrent.futures import ProcessPoolExecutor
import requests
from bs4 import BeautifulSoup
from pathlib import Path

# --- Configuration Constants ---
BASE_DIR = Path(__file__).resolve().parent
BASE_URL = "https://www.clinicaltrialsregister.eu"
SEARCH_PATH = "/ctr-search/search"
DOWNLOAD_PATH = "/ctr-search/rest/download/full"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


def get_total_pages(session, query):
    """
    Fetches the first page of search results and parses the total page count.
    """
    search_url = f"{BASE_URL}{SEARCH_PATH}"
    params = {"query": query, "page": 1}
    
    print(f"Connecting to {search_url} to detect total pages...")
    try:
        resp = session.get(search_url, params=params, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"Error fetching search page to detect total pages: {e}")
        return None
        
    soup = BeautifulSoup(resp.text, 'html.parser')
    
    # Try to find "Displaying page 1 of X" in the text
    text_matches = soup.find_all(string=re.compile(r"Displaying page \d+ of ([\d,]+)", re.IGNORECASE))
    if text_matches:
        match = re.search(r"Displaying page \d+ of ([\d,]+)", text_matches[0], re.IGNORECASE)
        if match:
            total_pages = int(match.group(1).replace(",", ""))
            print(f"[OK] Detected total pages: {total_pages:,}")
            return total_pages
            
    # Fallback: check pagination links (specifically the 'Last' link)
    last_link = soup.find('a', string=re.compile(r"Last", re.IGNORECASE))
    if last_link and last_link.get('href'):
        href = last_link.get('href')
        match = re.search(r"page=(\d+)", href)
        if match:
            total_pages = int(match.group(1))
            print(f"[OK] Detected total pages from pagination links: {total_pages:,}")
            return total_pages
            
    print("Warning: Could not detect total page count. Defaulting to 1.")
    return 1


def download_page(session, page, query, output_file, max_retries=5):
    """
    Downloads the full details text file for a single page.
    """
    download_url = f"{BASE_URL}{DOWNLOAD_PATH}"
    params = {
        "query": query,
        "page": page,
        "mode": "current_page"
    }
    
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(download_url, params=params, headers=HEADERS, timeout=45)
            
            # Handle rate limiting
            if resp.status_code == 429:
                wait_time = 10 * attempt
                print(f"  [!] Rate limited (429) on page {page}. Waiting {wait_time}s before retry...")
                time.sleep(wait_time)
                continue
                
            # Handle server/connection issues
            if resp.status_code != 200:
                wait_time = 3 * attempt
                print(f"  [!] Received status code {resp.status_code} on page {page}. Waiting {wait_time}s before retry...")
                time.sleep(wait_time)
                continue
                
            # Verify the response is actually the text download and not HTML error page
            content_type = resp.headers.get("Content-Type", "")
            first_chars = resp.text[:100].strip()
            if "html" in content_type.lower() or first_chars.startswith("<!DOCTYPE") or first_chars.startswith("<html"):
                wait_time = 3 * attempt
                print(f"  [!] Received HTML instead of data file on page {page}. Waiting {wait_time}s before retry...")
                time.sleep(wait_time)
                continue
                
            # Save response text (UTF-8 format)
            with open(output_file, "w", encoding="utf-8", errors="ignore") as f:
                f.write(resp.text)
                
            print(f"  [OK] Downloaded page {page} (~{len(resp.content)/1024:.1f} KB) -> {output_file.name}")
            return True
            
        except Exception as e:
            wait_time = 3 * attempt
            print(f"  [!] Exception on page {page}: {e}. Waiting {wait_time}s before retry...")
            time.sleep(wait_time)
            
    print(f"  [X] Failed to download page {page} after {max_retries} attempts.")
    return False


def merge_files(output_dir, merged_file_path):
    """
    Merges all downloaded page text files in output_dir into a single consolidated file.
    Separates pages clearly and strips redundant headers from pages > 1.
    """
    print(f"\nMerging page files in {output_dir} into: {merged_file_path}")
    page_files = sorted(list(output_dir.glob("page_*.txt")))
    
    if not page_files:
        print("No files found to merge.")
        return
        
    header_to_strip = (
        "This file contains full details on each clinical trial selected for download. "
        "Where multi-state trials have been downloaded full information for each of the "
        "member states/countries involved in the trial are included separately."
    ).strip()
    
    success_count = 0
    with open(merged_file_path, "w", encoding="utf-8") as outfile:
        for idx, file_path in enumerate(page_files):
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as infile:
                    content = infile.read()
                    
                # If subsequent file, strip duplicate instructions header
                if idx > 0:
                    content_stripped = content.strip()
                    if content_stripped.startswith(header_to_strip):
                        content_stripped = content_stripped[len(header_to_strip):].strip()
                    
                    # Add distinct separator for pages
                    outfile.write("\n\n" + "="*80 + f"\nPAGE {file_path.stem.split('_')[1]}\n" + "="*80 + "\n\n")
                    outfile.write(content_stripped)
                else:
                    outfile.write(content)
                success_count += 1
            except Exception as e:
                print(f"  [X] Error reading {file_path.name} during merge: {e}")
                
    if success_count > 0:
        print(f"[OK] Successfully merged {success_count} files into {merged_file_path.name} ({merged_file_path.stat().st_size / (1024*1024):.2f} MB)")
    else:
        print("[X] Merging failed (no files successfully processed).")


def is_standard_field(field_name):
    """
    Determines if a parsed field name matches the standard structure of a EudraCT field.
    Allows filtering out general text content/bullets that contain colons.
    """
    name = field_name
    if " - " in field_name:
        parts = field_name.split(" - ", 1)
        name = parts[1]
        
    summary_fields = {
        "eudract number", "sponsor's protocol code number", "national competent authority",
        "clinical trial type", "trial status", "date on which this record was first entered in the eudract database",
        "date on which this record was first entered in the eudract database (eudract)", "link"
    }
    name_lower = name.lower()
    for f in summary_fields:
        if name_lower.startswith(f):
            return True
            
    # Regex: Letter A-P, followed by a dot, and then optional numbers/subfields, followed by space or end of string
    pattern = r'^([A-P]\.(?:\d+|imp|placebo)?(?:\.\d+)*(?:\s+and\s+[A-P]\.(?:\d+|imp|placebo)?(?:\.\d+)*)*(?:\s+|$))'
    match = re.match(pattern, name, re.IGNORECASE)
    if match:
        return True
        
    return False


def stream_trials(source):
    """
    Generator that processes raw page text files or a merged text file line-by-line
    and yields parsed trial dictionaries. Kept memory-efficient for large files.
    """
    paths = source if isinstance(source, list) else [source]
    
    current_trial = {}
    current_key = None
    
    current_sponsor = ""
    current_imp = ""
    current_placebo = ""
    
    for path in paths:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line_str = line.strip()
                if not line_str:
                    continue
                    
                # Skip page headers if processing merged file
                if line_str.startswith("=") or re.match(r"^PAGE \d+$", line_str):
                    continue
                    
                if line_str == "Summary":
                    if current_trial and "EudraCT Number" in current_trial:
                        yield current_trial
                        current_trial = {}
                        current_key = None
                        current_sponsor = ""
                        current_imp = ""
                        current_placebo = ""
                    continue
                    
                if line_str == "Summary" or re.match(r"^[A-P]\.\s+", line_str):
                    current_sponsor = ""
                    current_imp = ""
                    current_placebo = ""
                    
                sponsor_match = re.match(r"^Sponsor\s+(\d+)$", line_str, re.IGNORECASE)
                if sponsor_match:
                    current_sponsor = f"Sponsor {sponsor_match.group(1)}"
                    current_imp = ""
                    current_placebo = ""
                    continue
                    
                imp_match = re.match(r"^D\.IMP:\s*(\d+)$", line_str, re.IGNORECASE)
                if imp_match:
                    current_imp = f"IMP {imp_match.group(1)}"
                    current_sponsor = ""
                    current_placebo = ""
                    
                placebo_match = re.match(r"^D\.8\s+Placebo:\s*(\d+)$", line_str, re.IGNORECASE)
                if placebo_match:
                    current_placebo = f"Placebo {placebo_match.group(1)}"
                    current_sponsor = ""
                    current_imp = ""
                    
                kv_match = re.match(r"^([A-Z0-9a-z_#'\.\-\s\(\)/&\t]+):\s*(.*)$", line_str)
                is_kv = False
                if kv_match:
                    key = kv_match.group(1).strip()
                    val = kv_match.group(2).strip()
                    
                    prefix = ""
                    if current_sponsor:
                        prefix = f"{current_sponsor} - "
                    elif current_imp:
                        prefix = f"{current_imp} - "
                    elif current_placebo:
                        prefix = f"{current_placebo} - "
                        
                    full_key = f"{prefix}{key}"
                    
                    if is_standard_field(full_key):
                        current_trial[full_key] = val
                        current_key = full_key
                        is_kv = True
                        
                if not is_kv:
                    if current_key and current_key in current_trial:
                        current_trial[current_key] += " " + line_str
                        
    if current_trial and "EudraCT Number" in current_trial:
        yield current_trial


def parse_single_file(file_path):
    """
    Parses a single file path and returns a list of trial dicts.
    Required as a top-level function for multiprocessing pickling.
    """
    return list(stream_trials(file_path))


def convert_to_csv_and_cleanup(output_dir, cleanup=True):
    """
    Coordinates the single-pass parallel CSV conversion and clean up of raw text files.
    """
    print("\n" + "=" * 60)
    print("  CSV CONVERSION & CLEANUP PROCESS")
    print(f"  Target Directory : {output_dir}")
    print(f"  Cleanup Txt Files: {cleanup}")
    print("=" * 60)
    
    page_files = sorted(list(output_dir.glob("page_*.txt")))
    merged_file = output_dir / "eu_ctr_all_trials.txt"
    
    csv_file = output_dir / "eu_ctr_all_trials.csv"
    start_time = time.time()
    
    # Try importing tqdm for visual progress bars
    try:
        from tqdm import tqdm
    except ImportError:
        def tqdm(iterable, *args, **kwargs):
            return iterable
            
    if page_files:
        print(f"Found {len(page_files)} page text files. Parsing in parallel...")
        try:
            with ProcessPoolExecutor() as executor:
                # Wrap executor.map with tqdm to show real-time progress of parsing page files
                results = list(tqdm(
                    executor.map(parse_single_file, page_files, chunksize=20),
                    total=len(page_files),
                    desc="Parsing Page Files"
                ))
            all_trials = [t for page_trials in results for t in page_trials]
        except Exception as e:
            print(f"[X] Parallel parsing failed: {e}. Falling back to sequential parsing...")
            all_trials = list(tqdm(stream_trials(page_files), desc="Parsing Page Files"))
    elif merged_file.exists():
        print(f"No page files found, but merged file '{merged_file.name}' exists. Parsing sequentially...")
        all_trials = list(tqdm(stream_trials(merged_file), desc="Parsing Merged File"))
    else:
        print("[X] No source text files (page_*.txt or eu_ctr_all_trials.txt) found for CSV conversion.")
        return False
        
    if not all_trials:
        print("[X] No trials parsed. CSV generation skipped.")
        return False
        
    try:
        # Collect and sort columns
        print("Collecting and sorting columns...")
        start_col_time = time.time()
        all_keys = set()
        for t in tqdm(all_trials, desc="Scanning Columns"):
            all_keys.update(t.keys())
            
        sorted_headers = sorted(list(all_keys))
        # Place key identifying columns at the beginning
        for first_key in reversed(["EudraCT Number", "Link", "National Competent Authority", "Trial Status"]):
            if first_key in sorted_headers:
                sorted_headers.remove(first_key)
                sorted_headers.insert(0, first_key)
        print(f"[OK] Found {len(sorted_headers)} columns (column sorting complete in {time.time() - start_col_time:.2f}s).")
        
        # Write to CSV
        print(f"Writing to CSV: {csv_file.name}...")
        start_csv_time = time.time()
        
        with open(csv_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=sorted_headers)
            writer.writeheader()
            # Wrap the iteration with tqdm to show progress row by row
            for trial in tqdm(all_trials, desc="Writing CSV Rows"):
                writer.writerow(trial)
                
        print(f"[OK] CSV writing complete in {time.time() - start_csv_time:.2f}s. Wrote {len(all_trials):,} trials.")
        print(f"[OK] CSV file generated successfully: {csv_file.name} ({csv_file.stat().st_size / (1024*1024):.2f} MB)")
        print(f"[OK] Total processing time: {time.time() - start_time:.2f} seconds.")
        
        # Cleanup: delete all txt files if requested
        if cleanup:
            print("Cleaning up raw text files...")
            deleted_count = 0
            for fpath in page_files:
                try:
                    fpath.unlink()
                    deleted_count += 1
                except Exception as e:
                    print(f"  [!] Failed to delete {fpath.name}: {e}")
                    
            if merged_file.exists():
                try:
                    merged_file.unlink()
                    deleted_count += 1
                    print(f"  Deleted merged file: {merged_file.name}")
                except Exception as e:
                    print(f"  [!] Failed to delete {merged_file.name}: {e}")
                    
            print(f"[OK] Deleted {deleted_count} raw text files.")
            
        return True
        
    except Exception as e:
        print(f"[X] CSV conversion failed: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Download trials data page-by-page from EU Clinical Trials Register.")
    parser.add_argument("--output-dir", "-o", default=str(BASE_DIR / "eu_ctr_trials"), help="Directory to save downloaded page text files.")
    parser.add_argument("--query", "-q", default="", help="Search query string (default: empty for all trials).")
    parser.add_argument("--start-page", "-s", type=int, default=1, help="Page number to start downloading from (default: 1).")
    parser.add_argument("--end-page", "-e", type=int, default=None, help="Page number to end downloading (default: last page).")
    parser.add_argument("--limit", "-l", type=int, default=None, help="Maximum number of pages to download in this run.")
    parser.add_argument("--delay", "-d", type=float, default=1.0, help="Delay in seconds between page requests (default: 1.0s).")
    parser.add_argument("--retries", "-r", type=int, default=5, help="Maximum retries per failed page (default: 5).")
    parser.add_argument("--merge", action="store_true", default=True, help="Merge all page files after finishing (default: True).")
    parser.add_argument("--no-merge", dest="merge", action="store_false", help="Do not merge page files after finishing.")
    parser.add_argument("--csv-only", action="store_true", help="Skip downloading/merging and only convert existing txt files to CSV format and cleanup.")
    parser.add_argument("--no-cleanup", dest="cleanup", action="store_false", default=True, help="Do not delete raw page and merged text files after CSV conversion.")
    
    args = parser.parse_args()
    
    # Ensure output directory exists
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    if args.csv_only:
        success = convert_to_csv_and_cleanup(output_dir, cleanup=args.cleanup)
        sys.exit(0 if success else 1)
        
    session = requests.Session()
    
    # Detect total pages dynamically if not specified
    total_pages = get_total_pages(session, args.query)
    if not total_pages:
        print("[X] Could not communicate with server or retrieve total pages. Exiting.")
        sys.exit(1)
        
    start_page = max(1, args.start_page)
    end_page = total_pages if args.end_page is None else min(total_pages, args.end_page)
    
    # Respect limit if specified
    if args.limit is not None:
        end_page = min(end_page, start_page + args.limit - 1)
        
    print("=" * 60)
    print("  EU Clinical Trials Register Downloader")
    print(f"  Query          : '{args.query}'")
    print(f"  Pages to Fetch : {start_page} to {end_page} (Total: {end_page - start_page + 1})")
    print(f"  Output Dir     : {output_dir}")
    print(f"  Delay          : {args.delay} seconds")
    print("=" * 60)
    
    success_count = 0
    skip_count = 0
    fail_count = 0
    
    for page in range(start_page, end_page + 1):
        file_path = output_dir / f"page_{page:04d}.txt"
        
        # Check if already exists for resume capability
        if file_path.exists():
            print(f"[{page}/{end_page}] Page file {file_path.name} already exists. Skipping.")
            skip_count += 1
            continue
            
        print(f"[{page}/{end_page}] Processing page {page}...")
        success = download_page(session, page, args.query, file_path, max_retries=args.retries)
        
        if success:
            success_count += 1
        else:
            fail_count += 1
            
        # Polite delay between page requests
        if page < end_page:
            time.sleep(args.delay)
            
    print("\n" + "=" * 60)
    print("  DOWNLOAD PROCESS FINISHED")
    print(f"  Successfully Downloaded : {success_count}")
    print(f"  Skipped (Existing)      : {skip_count}")
    print(f"  Failed                  : {fail_count}")
    print("=" * 60)
    
    # Merge if flag is set
    if args.merge:
        merged_file = output_dir / "eu_ctr_all_trials.txt"
        merge_files(output_dir, merged_file)
        
    # Convert to CSV and clean up text files
    convert_to_csv_and_cleanup(output_dir, cleanup=args.cleanup)


if __name__ == "__main__":
    main()
