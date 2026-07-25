#!/usr/bin/env python3
"""
CDISC Trial Data Standards Downloader and Parser
Author: Antigravity AI Coding Assistant
Description: Systematically crawls, downloads, and processes CDISC Controlled Terminology
             from NCI EVS and CDISC Therapeutic Area User Guides from cdisc.org.
             It converts these resources into structured CSV files under
             'Ontologies & Standards/CDISC/data/' with robust progress tracking.
"""

import os
import sys
import re
import csv
import json
import logging
import argparse
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path

# Constants
BASE_NCI_URL = "https://evs.nci.nih.gov/ftp1/CDISC/"
BASE_CDISC_URL = "https://www.cdisc.org/standards/therapeutic-areas"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
}

# Directories
# Resolve all output paths relative to this script's folder (pipeline contract)
BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR
CDISC_DIR = BASE_DIR / "CDISC"
DATA_DIR = CDISC_DIR / "data"
RAW_DIR = CDISC_DIR / "raw"
TRACKER_PATH = CDISC_DIR / "tracker.json"

# Logger
logger = logging.getLogger("cdisc_downloader")

def setup_logging(verbose: bool):
    """Configures the logging output level."""
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)]
    )

def create_directories():
    """Ensures that all output directories exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

def load_tracker() -> dict:
    """Loads tracker.json if it exists, otherwise returns a new structure."""
    if TRACKER_PATH.exists():
        try:
            with open(TRACKER_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to read tracker.json, starting fresh. Error: {e}")
    return {
        "therapeutic_areas": {
            "status": "pending",
            "last_updated": None,
            "url": BASE_CDISC_URL
        },
        "standards": {}
    }

def save_tracker(tracker: dict):
    """Saves the tracking data to tracker.json."""
    try:
        with open(TRACKER_PATH, "w", encoding="utf-8") as f:
            json.dump(tracker, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save tracker.json: {e}")

def fetch_url_content(url: str) -> str:
    """Fetches text content from a URL with standard browser headers."""
    logger.debug(f"Fetching URL: {url}")
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.read().decode("utf-8", errors="ignore")

def download_file_stream(url: str, dest_path: Path) -> bool:
    """Downloads a file in chunks with logging."""
    logger.info(f"Downloading file: {url}")
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            total_size = int(response.headers.get("content-length", 0))
            downloaded = 0
            chunk_size = 1024 * 1024  # 1MB
            
            with open(dest_path, "wb") as f:
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        percent = (downloaded / total_size) * 100
                        logger.info(f"  Progress: {downloaded / (1024*1024):.1f} MB / {total_size / (1024*1024):.1f} MB ({percent:.1f}%)")
                    else:
                        logger.info(f"  Downloaded: {downloaded / (1024*1024):.1f} MB")
            return True
    except Exception as e:
        logger.error(f"Error downloading {url}: {e}")
        if dest_path.exists():
            dest_path.unlink()  # Clean up partial downloads
        return False

# NCI EVS migrated the /ftp1/CDISC/ directory index to a JavaScript SPA, so the old
# HTML-listing scrape returns no links. The terminology .txt files are still served
# directly, so we address them explicitly. Each entry is confirmed reachable (200 +
# real tab-separated content); a non-existent path returns the SPA shell (~2.9 KB),
# which download_and_convert guards against. Extend this map as EVS exposes more.
KNOWN_STANDARDS = {
    "SDTM": ["SDTM Terminology.txt"],
    "SEND": ["SEND Terminology.txt"],
    "Protocol": ["Protocol Terminology.txt"],
    "Define-XML": ["Define-XML Terminology.txt"],
    "DDF": ["DDF Terminology.txt"],
    "TMF": ["TMF Terminology.txt"],
}


def discover_nci_standards() -> list:
    """Return the known CDISC standard directories on NCI EVS.

    The EVS FTP directory index is now a JS SPA with no scrapable links, so the set
    is maintained explicitly (see KNOWN_STANDARDS) instead of parsed from a listing.
    """
    logger.info("Using %d known CDISC standard directories on NCI EVS...", len(KNOWN_STANDARDS))
    return list(KNOWN_STANDARDS.keys())

def discover_terminology_files(standard_dir: str) -> list:
    """Return URL-encoded terminology filenames for a known standard directory."""
    return [urllib.parse.quote(f) for f in KNOWN_STANDARDS.get(standard_dir, [])]

def convert_txt_to_csv(txt_path: Path, csv_path: Path, standard_name: str) -> bool:
    """Parses a tab-separated terminology txt file and writes a clean CSV."""
    logger.info(f"Converting terminology {txt_path.name} to CSV...")
    try:
        with open(txt_path, "r", encoding="utf-8", errors="ignore") as infile:
            reader = csv.reader(infile, delimiter="\t")
            
            # Read header
            try:
                headers = next(reader)
            except StopIteration:
                logger.error(f"File {txt_path.name} is empty.")
                return False
            
            # Standardize header names
            expected_headers = [
                "Code", "Codelist Code", "Codelist Extensible (Yes/No)", "Codelist Name",
                "CDISC Submission Value", "CDISC Synonym(s)", "CDISC Definition", "NCI Preferred Term"
            ]
            
            # Ensure we have the base headers, adding "Standard" as metadata
            out_headers = expected_headers + ["Standard"]
            
            with open(csv_path, "w", newline="", encoding="utf-8") as outfile:
                writer = csv.writer(outfile)
                writer.writerow(out_headers)
                
                rows_count = 0
                for row in reader:
                    if not row or not row[0].strip():
                        continue  # Skip empty rows
                    
                    # Pad row if it has fewer than 8 elements
                    while len(row) < 8:
                        row.append("")
                    # Truncate row if it has more than 8 elements (to keep matching columns)
                    row = row[:8]
                    
                    # Append metadata standard name
                    row.append(standard_name)
                    writer.writerow(row)
                    rows_count += 1
                    
        logger.info(f"Successfully processed {rows_count} terminology rows into {csv_path.name}")
        return True
    except Exception as e:
        logger.error(f"Failed to parse and write CSV for {txt_path.name}: {e}")
        return False

def crawl_therapeutic_areas() -> bool:
    """Crawls CDISC website for Therapeutic Area User Guides and saves them to CSV."""
    logger.info("Crawling CDISC Therapeutic Areas User Guides...")
    csv_path = DATA_DIR / "therapeutic_areas.csv"
    
    try:
        html = fetch_url_content(BASE_CDISC_URL)
        
        # Regex to find links matching /standards/therapeutic-areas/<disease>
        # The structure is: <a href="/standards/therapeutic-areas/disease-name">Disease Name</a>
        # We need to capture both the relative/absolute url and the inner text.
        pattern = r'<a\s+[^>]*href=["\'](https://www.cdisc.org)?/standards/therapeutic-areas/([^"\'#]+)["\'][^>]*>(.*?)</a>'
        
        taugs = []
        seen_urls = set()
        
        for m in re.finditer(pattern, html, re.DOTALL | re.IGNORECASE):
            path = m.group(2).strip()
            # Clean name (remove inner HTML tags if any, clean whitespace)
            name = re.sub(r'<[^>]+>', '', m.group(3)).strip()
            # Skip generic navigation or sub-pages
            if path in ('published-user-guides', 'disease-area') or not name:
                continue
            
            full_url = f"https://www.cdisc.org/standards/therapeutic-areas/{path}"
            if full_url not in seen_urls:
                seen_urls.add(full_url)
                taugs.append({
                    "Name": name,
                    "URL": full_url
                })
        
        if not taugs:
            logger.warning("No Therapeutic Area User Guides found. Checking alternative regex pattern...")
            # Fallback markdown-style link parser if page is simple markdown parsed
            for m in re.finditer(r'\[([^\]]+)\]\((https://www.cdisc.org)?/standards/therapeutic-areas/([^)]+)\)', html):
                name = m.group(1).strip()
                path = m.group(3).strip()
                full_url = f"https://www.cdisc.org/standards/therapeutic-areas/{path}"
                if full_url not in seen_urls:
                    seen_urls.add(full_url)
                    taugs.append({"Name": name, "URL": full_url})
        
        if taugs:
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["Name", "URL"])
                for t in taugs:
                    writer.writerow([t["Name"], t["URL"]])
            logger.info(f"Found and saved {len(taugs)} Therapeutic Area User Guides to: {csv_path.name}")
            return True
        else:
            logger.error("Failed to parse Therapeutic Areas from cdisc.org")
            return False
            
    except Exception as e:
        logger.error(f"Error crawling Therapeutic Areas: {e}")
        return False

def main():
    parser = argparse.ArgumentParser(description="CDISC standards downloader and parser.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print debug logs.")
    parser.add_argument("-f", "--force", action="store_true", help="Force re-download all resources.")
    args = parser.parse_args()
    
    setup_logging(args.verbose)
    create_directories()
    
    logger.info("Initializing CDISC Downloader...")
    tracker = load_tracker()
    
    # Task 1: Crawl Therapeutic Areas
    ta_tracker = tracker["therapeutic_areas"]
    if ta_tracker["status"] != "completed" or args.force:
        logger.info("Starting Therapeutic Areas crawl...")
        ta_tracker["status"] = "downloading"
        save_tracker(tracker)
        
        success = crawl_therapeutic_areas()
        if success:
            ta_tracker["status"] = "completed"
            ta_tracker["last_updated"] = datetime.utcnow().isoformat() + "Z"
        else:
            ta_tracker["status"] = "failed"
        save_tracker(tracker)
    else:
        logger.info("Therapeutic Areas crawl is already completed. Skipping (use --force to update).")

    # Task 2: Crawl and Process Terminology Standards from NCI EVS
    standards_dirs = discover_nci_standards()
    if not standards_dirs:
        logger.error("Could not discover standards from NCI EVS. Exiting.")
        sys.exit(1)
        
    logger.info(f"Discovered {len(standards_dirs)} standards directories: {', '.join(standards_dirs)}")
    
    for d in standards_dirs:
        files = discover_terminology_files(d)
        if not files:
            logger.warning(f"No terminology text files found in {d}")
            continue
            
        for f in files:
            # We use standard name + file basename as unique key
            # E.g., "SDTM/CDASH Terminology.txt" or "ADaM/ADaM Terminology.txt"
            # Replace %20 with space for readability
            standard_name = d
            filename = urllib.parse.unquote(f)
            resource_key = f"{standard_name}/{filename}"
            
            file_url = urllib.parse.urljoin(BASE_NCI_URL, f"{standard_name}/{f}")
            raw_path = RAW_DIR / filename
            csv_filename = filename.replace(".txt", ".csv")
            csv_path = DATA_DIR / csv_filename
            
            # Check tracker
            if resource_key not in tracker["standards"]:
                tracker["standards"][resource_key] = {
                    "url": file_url,
                    "status": "pending",
                    "last_updated": None
                }
                save_tracker(tracker)
                
            res_tracker = tracker["standards"][resource_key]
            
            if res_tracker["status"] == "completed" and not args.force:
                logger.info(f"Terminology {resource_key} already downloaded and parsed. Skipping.")
                continue
                
            # Download file
            logger.info(f"Processing terminology: {resource_key}")
            res_tracker["status"] = "downloading"
            save_tracker(tracker)
            
            download_success = download_file_stream(file_url, raw_path)
            if not download_success:
                res_tracker["status"] = "failed"
                save_tracker(tracker)
                continue

            # Guard: a non-existent EVS path returns the JS SPA shell (small HTML),
            # not terminology. Reject it so we never write a garbage CSV.
            head = raw_path.read_bytes()[:512].lstrip()
            if head[:1] == b"<" or b"\t" not in raw_path.read_bytes()[:4096]:
                logger.warning(f"{resource_key}: response is not tab-separated terminology "
                               f"(likely a moved/renamed file); skipping.")
                raw_path.unlink(missing_ok=True)
                res_tracker["status"] = "failed"
                save_tracker(tracker)
                continue

            # Convert to CSV
            parse_success = convert_txt_to_csv(raw_path, csv_path, standard_name)
            if parse_success:
                res_tracker["status"] = "completed"
                res_tracker["last_updated"] = datetime.utcnow().isoformat() + "Z"
                res_tracker["file_size_bytes"] = raw_path.stat().st_size
            else:
                res_tracker["status"] = "failed"
            save_tracker(tracker)
            
    logger.info("CDISC download and conversion run finished.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.warning("\nExecution interrupted by user. Exiting.")
        sys.exit(0)
