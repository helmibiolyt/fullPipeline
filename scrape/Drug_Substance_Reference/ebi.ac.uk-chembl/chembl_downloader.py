#!/usr/bin/env python3
"""
chembl_downloader.py
====================
A robust, professional script to search and extract data from the ChEMBL database.

Features:
  1. molecule: Search compounds by name, ID, or structure (substructure/similarity search).
  2. target: Search biological targets by name, accession, or ChEMBL ID.
  3. activity: Retrieve bioactivity data (IC50, Ki, etc.) filtered by molecule, target, or type.
  4. download-db: Automate downloading the official, full offline SQLite database dump.

This script uses the ChEMBL REST API with pagination, rate-limiting, and automatic
retries with back-off to ensure stable and polite execution.

Requirements:
    pip install requests beautifulsoup4
"""

import os
import sys
import time
import argparse
import csv
import json
import logging
import tarfile
from pathlib import Path
from urllib.parse import quote, urljoin
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Optional progress indicator for downloads
try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
API_BASE_URL = "https://www.ebi.ac.uk/chembl/api/data"
FTP_LATEST_URL = "https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/latest/"
DEFAULT_DELAY_S = 0.15           # seconds between API calls to prevent rate-limiting
REQUEST_TIMEOUT = 30             # seconds per request
MAX_RETRIES = 5                  # retries for transient errors
PAGE_SIZE = 1000                 # maximum allowed by ChEMBL API to minimize requests

# Resolve output paths relative to this script, never the current working dir.
BASE_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------
def setup_logging(output_dir: Path) -> logging.Logger:
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / "chembl_downloader.log"
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("ChEMBLDownloader")

# ---------------------------------------------------------------------------
# HTTP Session Builder
# ---------------------------------------------------------------------------
def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=MAX_RETRIES,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "Accept": "application/json",
        "User-Agent": "BiolytInternChEMBLDownloader/1.0 (Python; requests)"
    })
    return session

SESSION = build_session()

# ---------------------------------------------------------------------------
# Core API Requests & Pagination
# ---------------------------------------------------------------------------
def get_json(url: str, params: dict = None, log: logging.Logger = None) -> dict | None:
    """GET a JSON endpoint and return the parsed dict, or None on error."""
    try:
        resp = SESSION.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as e:
        if log:
            log.warning(f"HTTP error for {url}: {e}")
    except requests.exceptions.ConnectionError as e:
        if log:
            log.warning(f"Connection error for {url}: {e}")
    except requests.exceptions.Timeout:
        if log:
            log.warning(f"Timeout for {url}")
    except json.JSONDecodeError:
        if log:
            log.warning(f"JSON decode error for {url}")
    except Exception as e:
        if log:
            log.warning(f"Unexpected error for {url}: {e}")
    return None

def paginate_api(url: str, params: dict, list_key: str, log: logging.Logger) -> list:
    """
    Paginates through a ChEMBL endpoint to retrieve all records.
    Uses the next link in page_meta or manually increments offset.
    """
    results = []
    current_url = url
    current_params = params.copy() if params else {}
    
    # Ensure limit is set to optimize network requests
    if "limit" not in current_params:
        current_params["limit"] = PAGE_SIZE
    if "offset" not in current_params:
        current_params["offset"] = 0
        
    log.info(f"Starting query to {url} with params: {current_params}")
    
    page_count = 0
    total_count = None
    
    while current_url:
        page_count += 1
        log.info(f"Fetching page {page_count}...")
        
        # Rate-limiting delay to be a good citizen
        if page_count > 1:
            time.sleep(DEFAULT_DELAY_S)
            
        data = get_json(current_url, params=current_params, log=log)
        if not data:
            log.error("Failed to retrieve page data. Stopping pagination.")
            break
            
        # Extract items
        items = data.get(list_key, [])
        results.extend(items)
        
        # Get metadata
        meta = data.get("page_meta", {})
        total_count = meta.get("total_count")
        offset = meta.get("offset", 0)
        limit = meta.get("limit", PAGE_SIZE)
        
        if total_count is not None:
            log.info(f"  Retrieved {len(items)} items. Total progress: {len(results)}/{total_count}")
        else:
            log.info(f"  Retrieved {len(items)} items. Total progress: {len(results)}")
            
        # Determine next page
        next_link = meta.get("next")
        if next_link:
            # If next_link is relative, join it with the base URL
            if next_link.startswith("http"):
                current_url = next_link
            else:
                current_url = urljoin(API_BASE_URL, next_link)
            # Parameters are now embedded in the next_link URL, so clear params dict
            current_params = None
        else:
            # Fallback manual offset increment if next link is missing but we expect more
            if total_count is not None and (offset + limit) < total_count and len(items) > 0:
                current_params["offset"] = offset + limit
                log.info(f"Manual pagination fallback: next offset {current_params['offset']}")
            else:
                current_url = None
                
    log.info(f"Pagination completed. Retrieved {len(results)} records in total.")
    return results

# ---------------------------------------------------------------------------
# Data Exporters
# ---------------------------------------------------------------------------
def export_data(data: list, fields: list, filename: Path, fmt: str, log: logging.Logger):
    """Saves records to CSV or JSON format."""
    if not data:
        log.warning("No data to export.")
        return
        
    if fmt == "json":
        try:
            with open(filename, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
            log.info(f"Successfully exported {len(data)} records to JSON: {filename}")
        except Exception as e:
            log.error(f"Failed to export to JSON: {e}")
    else:
        try:
            with open(filename, "w", encoding="utf-8", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(fields)
                
                for row in data:
                    writer.writerow([row.get(f, "") for f in fields])
            log.info(f"Successfully exported {len(data)} records to CSV: {filename}")
        except Exception as e:
            log.error(f"Failed to export to CSV: {e}")

# ---------------------------------------------------------------------------
# Subcommand: Molecule
# ---------------------------------------------------------------------------
def handle_molecule(args, log: logging.Logger, output_dir: Path):
    """Handles searching and downloading compound/molecule data."""
    log.info("Starting molecule search process...")
    
    url = f"{API_BASE_URL}/molecule.json"
    params = {}
    
    # 1. Structure Searches (Similarity or Substructure)
    if args.smiles:
        encoded_smiles = quote(args.smiles)
        if args.type == "similarity":
            cutoff = args.cutoff if args.cutoff else 80
            url = f"{API_BASE_URL}/similarity/{encoded_smiles}/{cutoff}.json"
            log.info(f"Executing Tanimoto similarity search for SMILES '{args.smiles}' (threshold: {cutoff}%)")
        elif args.type == "substructure":
            url = f"{API_BASE_URL}/substructure/{encoded_smiles}.json"
            log.info(f"Executing substructure search for SMILES '{args.smiles}'")
    
    # 2. Filtering by ID or Name
    elif args.id:
        params["molecule_chembl_id"] = args.id
        log.info(f"Filtering by ChEMBL ID: {args.id}")
    elif args.name:
        params["pref_name__icontains"] = args.name
        log.info(f"Filtering by preferred name (contains): {args.name}")
        
    # 3. Apply additional filters
    if args.phase is not None:
        params["max_phase"] = args.phase
        log.info(f"Filtering by max clinical phase: {args.phase}")
        
    # Run query
    raw_data = paginate_api(url, params, "molecules", log)
    
    # Process and flatten records
    processed = []
    for item in raw_data:
        # Extract properties
        props = item.get("molecule_properties") or {}
        structs = item.get("molecule_structures") or {}
        
        # Extract synonyms
        synonyms = []
        if item.get("molecule_synonyms"):
            for syn in item["molecule_synonyms"]:
                syn_name = syn.get("molecule_synonym")
                if syn_name:
                    synonyms.append(syn_name)
        synonyms_str = "|".join(sorted(list(set(synonyms))))
        
        row = {
            "chembl_id": item.get("molecule_chembl_id") or item.get("chembl_id"),
            "pref_name": item.get("pref_name"),
            "molecule_type": item.get("molecule_type"),
            "max_phase": item.get("max_phase"),
            "mw_freebase": props.get("mw_freebase"),
            "alogp": props.get("alogp"),
            "hba": props.get("hba"),
            "hbd": props.get("hbd"),
            "canonical_smiles": structs.get("canonical_smiles"),
            "standard_inchi_key": structs.get("standard_inchi_key"),
            "synonyms": synonyms_str,
            "similarity": item.get("similarity"),  # Included in similarity searches
        }
        processed.append(row)
        
    # Define CSV fields
    fields = [
        "chembl_id", "pref_name", "molecule_type", "max_phase",
        "mw_freebase", "alogp", "hba", "hbd", "canonical_smiles",
        "standard_inchi_key", "synonyms"
    ]
    if args.smiles and args.type == "similarity":
        fields.append("similarity")
        
    # Export
    prefix = "molecule_search"
    if args.id:
        prefix = f"molecule_{args.id}"
    elif args.name:
        prefix = f"molecule_{args.name.replace(' ', '_')}"
    elif args.smiles:
        prefix = f"molecule_{args.type}"
        
    output_file = output_dir / f"{prefix}.{args.format}"
    export_data(processed, fields, output_file, args.format, log)

# ---------------------------------------------------------------------------
# Subcommand: Target
# ---------------------------------------------------------------------------
def handle_target(args, log: logging.Logger, output_dir: Path):
    """Handles searching biological targets."""
    log.info("Starting target search process...")
    
    url = f"{API_BASE_URL}/target.json"
    params = {}
    
    if args.id:
        params["target_chembl_id"] = args.id
        log.info(f"Filtering target by ChEMBL ID: {args.id}")
    elif args.name:
        params["pref_name__icontains"] = args.name
        log.info(f"Filtering target by preferred name (contains): {args.name}")
        
    raw_data = paginate_api(url, params, "targets", log)
    
    processed = []
    for item in raw_data:
        # Extract components (accessions)
        accessions = []
        if item.get("target_components"):
            for comp in item["target_components"]:
                acc = comp.get("accession")
                if acc:
                    accessions.append(acc)
        accessions_str = "|".join(sorted(list(set(accessions))))
        
        row = {
            "target_chembl_id": item.get("target_chembl_id"),
            "pref_name": item.get("pref_name"),
            "target_type": item.get("target_type"),
            "organism": item.get("organism"),
            "accessions": accessions_str,
        }
        processed.append(row)
        
    fields = ["target_chembl_id", "pref_name", "target_type", "organism", "accessions"]
    
    prefix = "target_search"
    if args.id:
        prefix = f"target_{args.id}"
    elif args.name:
        prefix = f"target_{args.name.replace(' ', '_')}"
        
    output_file = output_dir / f"{prefix}.{args.format}"
    export_data(processed, fields, output_file, args.format, log)

# ---------------------------------------------------------------------------
# Subcommand: Activity
# ---------------------------------------------------------------------------
def handle_activity(args, log: logging.Logger, output_dir: Path):
    """Handles retrieving bioactivities (IC50, Ki, etc.) for molecules/targets."""
    log.info("Starting bioactivity retrieval process...")
    
    if not args.molecule_id and not args.target_id:
        log.error("Error: You must provide either --molecule-id or --target-id to query activities.")
        sys.exit(1)
        
    url = f"{API_BASE_URL}/activity.json"
    params = {}
    
    if args.molecule_id:
        params["molecule_chembl_id"] = args.molecule_id
        log.info(f"Retrieving activities for molecule: {args.molecule_id}")
    if args.target_id:
        params["target_chembl_id"] = args.target_id
        log.info(f"Retrieving activities for target: {args.target_id}")
        
    # Additional filters
    if args.type:
        params["type__iexact"] = args.type
        log.info(f"Filtering activity by type: {args.type}")
    if args.relation:
        params["relation"] = args.relation
        log.info(f"Filtering activity by relation: {args.relation}")
        
    raw_data = paginate_api(url, params, "activities", log)
    
    processed = []
    for item in raw_data:
        row = {
            "activity_id": item.get("activity_id"),
            "molecule_chembl_id": item.get("molecule_chembl_id"),
            "target_chembl_id": item.get("target_chembl_id"),
            "assay_chembl_id": item.get("assay_chembl_id"),
            "type": item.get("type"),
            "relation": item.get("relation"),
            "value": item.get("value"),
            "units": item.get("units"),
            "canonical_data_validity_comment": item.get("canonical_data_validity_comment"),
            "document_chembl_id": item.get("document_chembl_id"),
            "uo_units": item.get("uo_units"),
            "standard_type": item.get("standard_type"),
            "standard_relation": item.get("standard_relation"),
            "standard_value": item.get("standard_value"),
            "standard_units": item.get("standard_units"),
        }
        processed.append(row)
        
    fields = [
        "activity_id", "molecule_chembl_id", "target_chembl_id", "assay_chembl_id",
        "type", "relation", "value", "units", "canonical_data_validity_comment",
        "document_chembl_id", "uo_units", "standard_type", "standard_relation",
        "standard_value", "standard_units"
    ]
    
    prefix = "activity"
    if args.molecule_id:
        prefix += f"_mol_{args.molecule_id}"
    if args.target_id:
        prefix += f"_target_{args.target_id}"
    if args.type:
        prefix += f"_{args.type}"
        
    output_file = output_dir / f"{prefix}.{args.format}"
    export_data(processed, fields, output_file, args.format, log)

# ---------------------------------------------------------------------------
# Subcommand: Download DB
# ---------------------------------------------------------------------------
def handle_download_db(args, log: logging.Logger, output_dir: Path):
    """
    Automates fetching the latest SQLite database dump from ChEMBL FTP.
    It scrapes the FTP latest index page, identifies the *_sqlite.tar.gz file,
    downloads it, and extracts the database.
    """
    log.info(f"Accessing EBI ChEMBL FTP directory: {FTP_LATEST_URL}")
    
    try:
        resp = SESSION.get(FTP_LATEST_URL, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        log.error(f"Failed to access ChEMBL FTP directory: {e}")
        log.info("You can try downloading manually from: https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/latest/")
        sys.exit(1)
        
    # Parse HTML using BeautifulSoup
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(resp.text, "html.parser")
    
    # Search for SQLite tarball link
    sqlite_file = None
    for link in soup.find_all("a"):
        href = link.get("href", "")
        if href.endswith("_sqlite.tar.gz"):
            sqlite_file = href
            break
            
    if not sqlite_file:
        log.error("Could not locate the latest SQLite tarball (*_sqlite.tar.gz) in the FTP index.")
        log.info("Directory contents parsed: " + ", ".join([a.get("href", "") for a in soup.find_all("a") if a.get("href")][:10]))
        sys.exit(1)
        
    download_url = urljoin(FTP_LATEST_URL, sqlite_file)
    log.info(f"Found latest SQLite archive: {sqlite_file}")
    log.info(f"Download URL: {download_url}")
    
    target_archive = output_dir / sqlite_file
    
    # Check if a partial file already exists to support download resumption
    initial_byte = 0
    mode = "wb"
    headers = {}
    download_completed = False
    
    if target_archive.exists():
        initial_byte = target_archive.stat().st_size
        if initial_byte > 0:
            log.info(f"Found existing file with size {initial_byte / (1024*1024):.1f}MB. Checking if we can resume...")
            try:
                head_resp = SESSION.head(download_url, timeout=REQUEST_TIMEOUT)
                head_resp.raise_for_status()
                total_size = int(head_resp.headers.get("content-length", 0))
                
                if initial_byte >= total_size and total_size > 0:
                    log.info(f"Local file size ({initial_byte} bytes) matches or exceeds remote size ({total_size} bytes). Skipping download and proceeding to extraction.")
                    download_completed = True
                elif head_resp.headers.get("accept-ranges") == "bytes":
                    log.info(f"Server supports range requests. Resuming download from byte {initial_byte}...")
                    headers["Range"] = f"bytes={initial_byte}-"
                    mode = "ab"
                else:
                    log.info("Server does not support range requests or size mismatch. Re-downloading from scratch.")
                    mode = "wb"
                    initial_byte = 0
            except Exception as e:
                log.warning(f"Could not verify file status with HEAD request: {e}. Re-downloading from scratch.")
                mode = "wb"
                initial_byte = 0

    # 1. Download file with progress indicator
    if not download_completed:
        log.info(f"Downloading archive to: {target_archive} (mode: '{mode}')")
        try:
            with SESSION.get(download_url, headers=headers, stream=True, timeout=REQUEST_TIMEOUT) as r:
                # If we sent a Range header, expect 206 Partial Content, otherwise 200 OK
                if r.status_code == 206:
                    content_range = r.headers.get("Content-Range", "")
                    if "/" in content_range:
                        total_size = int(content_range.split("/")[-1])
                    else:
                        total_size = int(r.headers.get("content-length", 0)) + initial_byte
                elif r.status_code == 200:
                    # Server ignored Range header, download from scratch
                    mode = "wb"
                    initial_byte = 0
                    total_size = int(r.headers.get("content-length", 0))
                else:
                    r.raise_for_status()
                    total_size = int(r.headers.get("content-length", 0))

                chunk_size = 1024 * 1024  # 1MB chunks
                
                if tqdm and total_size > 0:
                    with tqdm(total=total_size, initial=initial_byte, unit="B", unit_scale=True, desc=sqlite_file) as pbar:
                        with open(target_archive, mode) as f:
                            for chunk in r.iter_content(chunk_size=chunk_size):
                                f.write(chunk)
                                pbar.update(len(chunk))
                else:
                    # Simple text progress indicator if tqdm not installed
                    downloaded = initial_byte
                    last_reported = int((downloaded / total_size) * 100) if total_size > 0 else 0
                    log.info(f"  Starting download progress: {downloaded / (1024*1024):.1f}MB / {total_size / (1024*1024):.1f}MB ({last_reported}%)")
                    
                    with open(target_archive, mode) as f:
                        for chunk in r.iter_content(chunk_size=chunk_size):
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total_size > 0:
                                percent = int((downloaded / total_size) * 100)
                                if percent >= last_reported + 5:
                                    log.info(f"  Downloaded {downloaded / (1024*1024):.1f}MB / {total_size / (1024*1024):.1f}MB ({percent}%)")
                                    last_reported = percent
                            else:
                                log.info(f"  Downloaded {downloaded / (1024*1024):.1f}MB (size unknown)")
                                
            log.info("Download completed successfully.")
        except Exception as e:
            log.error(f"Failed to download ChEMBL database: {e}")
            log.info("If the download failed due to network issues, you can run this command again to resume.")
            # Note: We do not delete the file here. Leaving the partial file allows resumption on the next run.
            sys.exit(1)
            
    # 2. Extraction
    log.info(f"Extracting SQLite archive: {target_archive}")
    try:
        # Create sub-directory for extraction
        extract_path = output_dir / sqlite_file.replace(".tar.gz", "")
        extract_path.mkdir(parents=True, exist_ok=True)
        
        with tarfile.open(target_archive, "r:gz") as tar:
            # We want to extract only the SQLite file if possible, or all files
            members = tar.getmembers()
            log.info(f"Found {len(members)} files in archive.")
            
            for m in members:
                log.info(f"  - {m.name} ({m.size / (1024*1024):.1f} MB)")
                
            tar.extractall(path=extract_path)
            
        log.info(f"Extraction completed. Data saved to: {extract_path}")
        
        log.info(f"Cleaning up archive file: {target_archive}")
        target_archive.unlink()
        log.info("Cleanup completed. Database is ready for local query.")
        
    except Exception as e:
        log.error(f"Failed to extract ChEMBL database: {e}")
        sys.exit(1)

# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Search, extract, and download drug, compound, target, and bioactivity data from ChEMBL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Search for Aspirin molecules and export to CSV
  python chembl_downloader.py molecule --name aspirin

  # Find molecules similar to a SMILES structure (minimum 85% Tanimoto similarity)
  python chembl_downloader.py molecule --smiles "CC(=O)Oc1ccccc1C(=O)O" --type similarity --cutoff 85

  # Search targets containing the term "HERG"
  python chembl_downloader.py target --name HERG

  # Retrieve all IC50 bioactivity measurements for the target CHEMBL240 (HERG)
  python chembl_downloader.py activity --target-id CHEMBL240 --type IC50

  # Download and extract the full offline ChEMBL SQLite database dump
  python chembl_downloader.py download-db
"""
    )
    
    subparsers = parser.add_subparsers(dest="command", required=True, help="Subcommand to run")
    
    # 1. Molecule Subparser
    p_mol = subparsers.add_parser("molecule", help="Search and extract compound/substance data")
    g_mol = p_mol.add_mutually_exclusive_group(required=True)
    g_mol.add_argument("-i", "--id", help="ChEMBL Molecule ID (e.g. CHEMBL25)")
    g_mol.add_argument("-n", "--name", help="Preferred name of molecule (contains match)")
    g_mol.add_argument("-s", "--smiles", help="SMILES string for structure search")
    
    p_mol.add_argument("-t", "--type", choices=["similarity", "substructure"], default="similarity",
                       help="Structure search type (only with --smiles, default: similarity)")
    p_mol.add_argument("-c", "--cutoff", type=int, default=80,
                       help="Similarity Tanimoto cutoff percentage (0-100, default: 80)")
    p_mol.add_argument("-p", "--phase", type=int, choices=[0, 1, 2, 3, 4],
                       help="Filter by maximum clinical phase (0 to 4)")
    p_mol.add_argument("-o", "--output-dir", default=str(BASE_DIR / "chembl_data"),
                       help="Directory to save retrieved data (default: Drug & Substance Reference/chembl_data)")
    p_mol.add_argument("-f", "--format", choices=["csv", "json"], default="csv",
                       help="Output format (default: csv)")
                       
    # 2. Target Subparser
    p_trg = subparsers.add_parser("target", help="Search and extract biological targets")
    g_trg = p_trg.add_mutually_exclusive_group(required=True)
    g_trg.add_argument("-i", "--id", help="ChEMBL Target ID (e.g. CHEMBL240)")
    g_trg.add_argument("-n", "--name", help="Preferred name of target (contains match)")
    
    p_trg.add_argument("-o", "--output-dir", default=str(BASE_DIR / "chembl_data"),
                       help="Directory to save retrieved data (default: Drug & Substance Reference/chembl_data)")
    p_trg.add_argument("-f", "--format", choices=["csv", "json"], default="csv",
                       help="Output format (default: csv)")
                       
    # 3. Activity Subparser
    p_act = subparsers.add_parser("activity", help="Retrieve bioactivity data")
    p_act.add_argument("-m", "--molecule-id", help="Filter by ChEMBL Molecule ID (e.g. CHEMBL25)")
    p_act.add_argument("-t", "--target-id", help="Filter by ChEMBL Target ID (e.g. CHEMBL240)")
    p_act.add_argument("--type", help="Filter by activity type (e.g. IC50, Ki, EC50)")
    p_act.add_argument("--relation", help="Filter by value relation (e.g. '=', '<', '>')")
    
    p_act.add_argument("-o", "--output-dir", default=str(BASE_DIR / "chembl_data"),
                       help="Directory to save retrieved data (default: Drug & Substance Reference/chembl_data)")
    p_act.add_argument("-f", "--format", choices=["csv", "json"], default="csv",
                       help="Output format (default: csv)")
                       
    # 4. Download DB Subparser
    p_db = subparsers.add_parser("download-db", help="Download the latest offline ChEMBL SQLite database dump")
    p_db.add_argument("-o", "--output-dir", default=str(BASE_DIR / "chembl_data"),
                      help="Directory to save the database dump (default: Drug & Substance Reference/chembl_data)")
                      
    args = parser.parse_args()
    
    output_dir = Path(args.output_dir)
    log = setup_logging(output_dir)
    
    log.info("====================================================================")
    log.info("ChEMBL Downloader Utility Started")
    log.info("====================================================================")
    
    try:
        if args.command == "molecule":
            handle_molecule(args, log, output_dir)
        elif args.command == "target":
            handle_target(args, log, output_dir)
        elif args.command == "activity":
            handle_activity(args, log, output_dir)
        elif args.command == "download-db":
            handle_download_db(args, log, output_dir)
            
        log.info("Process completed successfully.")
        
    except KeyboardInterrupt:
        log.warning("Process interrupted by user.")
        sys.exit(130)
    except Exception as e:
        log.critical(f"Unhandled exception occurred: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
