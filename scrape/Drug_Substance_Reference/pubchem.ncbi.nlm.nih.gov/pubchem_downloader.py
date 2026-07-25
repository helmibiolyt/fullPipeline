#!/usr/bin/env python3
"""
pubchem_downloader.py
=====================
A robust, professional script to search, extract, and download data from PubChem.

Features:
  1. property: Fetch chemical properties (SMILES, InChIKey, IUPAC Name, Formula, etc.)
               for a list of CIDs or chemical names. Supports automatic batching.
  2. search: Perform structure-based similarity or substructure searches using a
             SMILES query, resolving matching CIDs to properties.
  3. download-bulk: Stream and download massive whole-database identifier mapping files
                    (e.g., CID-SMILES, CID-InChI-Key, CID-Parent) from PubChem FTP.

This script uses the PubChem PUG-REST API with automatic retries, back-off, polite
throttling, and adaptive rate-limiting using the server's X-Throttling-Control headers.

Requirements:
    pip install requests
"""

import os
import sys
import time
import argparse
import csv
import json
import logging
import gzip
import shutil
from pathlib import Path
from urllib.parse import quote
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Optional progress indicator
try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PUG_REST_BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
FTP_BASE_URL = "https://ftp.ncbi.nlm.nih.gov/pubchem/Compound/Extras"
DEFAULT_DELAY_S = 0.2           # Polite delay (5 requests per second limit)
REQUEST_TIMEOUT = 45             # 45 seconds timeout for heavy queries
MAX_RETRIES = 5

# Resolve output paths relative to this script, never the current working dir.
BASE_DIR = Path(__file__).resolve().parent

DEFAULT_PROPERTIES = [
    "CanonicalSMILES", "IsomericSMILES", "InChI", "InChIKey",
    "IUPACName", "MolecularFormula", "MolecularWeight", "XLogP",
    "TPSA", "Charge", "Complexity"
]

# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------
def setup_logging(output_dir: Path) -> logging.Logger:
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / "pubchem_downloader.log"
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("PubChemDownloader")

# ---------------------------------------------------------------------------
# HTTP Session Builder
# ---------------------------------------------------------------------------
def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=MAX_RETRIES,
        backoff_factor=2.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "Accept": "application/json",
        "User-Agent": "BiolytInternPubChemDownloader/1.0 (Python; requests)"
    })
    return session

SESSION = build_session()

# ---------------------------------------------------------------------------
# Adaptive Throttling and Request Wrapper
# ---------------------------------------------------------------------------
def handle_throttling(headers: dict, log: logging.Logger) -> None:
    """
    Inspects PubChem's X-Throttling-Control header and applies adaptive sleep.
    Example: 'Request Count status: Green (0%), Request Time status: Green (0%), Service status: Yellow (55%)'
    """
    throttle_header = headers.get("X-Throttling-Control") or headers.get("x-throttling-control")
    if not throttle_header:
        return

    normalized = throttle_header.lower()
    if "black" in normalized:
        log.warning(f"Throttling BLACK alert! PubChem is blocking requests. Backing off for 30 seconds. Header: {throttle_header}")
        time.sleep(30.0)
    elif "red" in normalized:
        log.warning(f"Throttling RED alert! Nearing limits. Backing off for 10 seconds. Header: {throttle_header}")
        time.sleep(10.0)
    elif "yellow" in normalized:
        log.info(f"Throttling YELLOW warning. Backing off for 2 seconds. Header: {throttle_header}")
        time.sleep(2.0)

def execute_request(url: str, method: str = "GET", data: dict = None, params: dict = None, log: logging.Logger = None) -> dict | None:
    """Sends a request to PubChem and handles rate-limiting, headers, and parsing."""
    if log:
        log.debug(f"Sending {method} request to: {url}")
        
    try:
        if method.upper() == "POST":
            resp = SESSION.post(url, data=data, params=params, timeout=REQUEST_TIMEOUT)
        else:
            resp = SESSION.get(url, params=params, timeout=REQUEST_TIMEOUT)
            
        # Parse throttling headers in any case
        if log:
            handle_throttling(resp.headers, log)
            
        # Check status
        if resp.status_code == 404:
            # Not found is a common API response when a query is invalid or empty (e.g. name not found)
            return None
            
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
            log.warning(f"Failed to decode JSON response from {url}")
    except Exception as e:
        if log:
            log.warning(f"Unexpected error for {url}: {e}")
    return None

# ---------------------------------------------------------------------------
# Resolve Names to CIDs
# ---------------------------------------------------------------------------
def resolve_name_to_cid(name: str, log: logging.Logger) -> int | None:
    """Resolves a single chemical name to a PubChem CID."""
    encoded_name = quote(name.strip())
    url = f"{PUG_REST_BASE}/compound/name/{encoded_name}/cids/JSON"
    
    time.sleep(DEFAULT_DELAY_S)
    data = execute_request(url, log=log)
    if not data:
        return None
        
    cids = data.get("IdentifierList", {}).get("CID", [])
    if cids:
        return cids[0]
    return None

# ---------------------------------------------------------------------------
# Retrieve Properties (Batch Support)
# ---------------------------------------------------------------------------
def fetch_properties_batch(cids: list, properties: list, log: logging.Logger) -> list:
    """Fetches requested properties for a batch of CIDs."""
    if not cids:
        return []
        
    # Filter out CID from API query since it is not a valid property tag
    # and is automatically returned by the API anyway.
    api_properties = [p for p in properties if p.upper() != "CID"]
    
    cid_str = ",".join(str(c) for c in cids)
    prop_str = ",".join(api_properties)
    url = f"{PUG_REST_BASE}/compound/cid/{cid_str}/property/{prop_str}/JSON"
    
    time.sleep(DEFAULT_DELAY_S)
    data = execute_request(url, log=log)
    if not data:
        return []
        
    return data.get("PropertyTable", {}).get("Properties", [])

def get_properties_for_cids(cids: list, properties: list, batch_size: int, log: logging.Logger) -> list:
    """Splits CID list into batches and retrieves properties with a progress indicator."""
    results = []
    total_cids = len(cids)
    log.info(f"Retrieving properties for {total_cids:,} CIDs in batches of {batch_size}...")
    
    iterator = range(0, total_cids, batch_size)
    if tqdm:
        iterator = tqdm(iterator, desc="Retrieving properties", unit="batch")
        
    for i in iterator:
        batch = cids[i:i + batch_size]
        batch_results = fetch_properties_batch(batch, properties, log)
        results.extend(batch_results)
        
    log.info(f"Successfully retrieved properties for {len(results):,}/{total_cids:,} CIDs.")
    return results

# ---------------------------------------------------------------------------
# Asynchronous Structure Search (Similarity / Substructure)
# ---------------------------------------------------------------------------
def run_structure_search(smiles: str, search_type: str, threshold: int, log: logging.Logger) -> list:
    """Submits a structure search job and polls the server until CIDs are returned."""
    encoded_smiles = quote(smiles.strip())
    
    if search_type == "similarity":
        url = f"{PUG_REST_BASE}/compound/similarity/smiles/{encoded_smiles}/JSON"
        params = {"Threshold": threshold}
    elif search_type == "substructure":
        url = f"{PUG_REST_BASE}/compound/substructure/smiles/{encoded_smiles}/JSON"
        params = None
    else:
        raise ValueError(f"Unknown search type: {search_type}")
        
    log.info(f"Submitting {search_type} search for SMILES: {smiles}")
    data = execute_request(url, method="POST", params=params, log=log)
    if not data:
        log.error("Failed to submit structure search job.")
        return []
        
    # Check if we got a ListKey (asynchronous job)
    waiting = data.get("Waiting", {})
    listkey = waiting.get("ListKey")
    
    if not listkey:
        # Sometimes small queries return immediate results
        cids = data.get("IdentifierList", {}).get("CID", [])
        log.info(f"Immediate results returned: {len(cids):,} CIDs.")
        return cids
        
    log.info(f"Job submitted successfully. ListKey: {listkey}. Polling for status...")
    
    # Polling loop
    poll_url = f"{PUG_REST_BASE}/compound/listkey/{listkey}/cids/JSON"
    poll_delay = 3.0
    max_polls = 30
    
    for attempt in range(1, max_polls + 1):
        time.sleep(poll_delay)
        # Increase delay slightly to avoid spamming
        poll_delay = min(poll_delay * 1.2, 15.0)
        
        log.info(f"Polling attempt {attempt}/{max_polls}...")
        poll_data = execute_request(poll_url, log=log)
        if not poll_data:
            continue
            
        # Check if still waiting
        if "Waiting" in poll_data:
            log.info("Job is still running. Waiting...")
            continue
            
        # Check for results
        cids = poll_data.get("IdentifierList", {}).get("CID", [])
        log.info(f"Search completed. Found {len(cids):,} matching CIDs.")
        return cids
        
    log.error(f"Search timed out after {max_polls} polling attempts.")
    return []

# ---------------------------------------------------------------------------
# Bulk File Downloader
# ---------------------------------------------------------------------------
def download_bulk_file(filename: str, output_dir: Path, decompress: bool, limit_bytes: int | None, log: logging.Logger) -> None:
    """Streams and downloads a bulk mapping file from the PubChem FTP/HTTPS repository with resume support."""
    url = f"{FTP_BASE_URL}/{filename}"
    dest_path = output_dir / filename
    
    log.info(f"Starting bulk download from: {url}")
    log.info(f"Destination: {dest_path}")
    
    # Check if file already exists and get its size for potential resume
    initial_bytes = 0
    if dest_path.exists():
        initial_bytes = dest_path.stat().st_size
        log.info(f"Local file already exists with size: {initial_bytes:,} bytes. Attempting to resume download...")
        
    headers = SESSION.headers.copy()
    if initial_bytes > 0:
        headers["Range"] = f"bytes={initial_bytes}-"
        
    try:
        resp = SESSION.get(url, stream=True, headers=headers, timeout=REQUEST_TIMEOUT)
        
        # Check if the server accepted the range request (HTTP 206 Partial Content)
        if initial_bytes > 0 and resp.status_code == 206:
            log.info("Server accepted Range request. Resuming download...")
            write_mode = "ab"  # Append binary
            total_size = initial_bytes + int(resp.headers.get("content-length", 0))
        else:
            if initial_bytes > 0:
                log.info("Server did not accept Range request (or file changed). Starting download from scratch.")
            write_mode = "wb"  # Write binary
            initial_bytes = 0
            resp.raise_for_status()
            total_size = int(resp.headers.get("content-length", 0))
            
        chunk_size = 1024 * 64  # 64 KB chunks
        written_bytes = initial_bytes
        
        # Setup progress indicator
        if tqdm:
            progress = tqdm(total=total_size, initial=initial_bytes, unit="B", unit_scale=True, desc=filename)
        else:
            progress = None
            log.info(f"Downloading file... (currently at {written_bytes:,} / {total_size:,} bytes)")
            
        with open(dest_path, write_mode) as fh:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                fh.write(chunk)
                written_bytes += len(chunk)
                
                if progress:
                    progress.update(len(chunk))
                    
                # Apply artificial byte limit (useful for dry runs / testing)
                if limit_bytes and written_bytes >= limit_bytes:
                    log.info(f"Byte limit reached ({limit_bytes} bytes). Stopping download.")
                    break
                    
        if progress:
            progress.close()
            
        log.info(f"Successfully downloaded {written_bytes:,} bytes to {dest_path}")
        
        # Decompress if requested and not limit-interrupted
        if decompress:
            if limit_bytes and written_bytes >= limit_bytes:
                log.warning("Skipping decompression because download was truncated by byte limit.")
                return
                
            decompressed_path = output_dir / dest_path.stem  # strips .gz
            log.info(f"Decompressing {dest_path} to {decompressed_path}...")
            
            with gzip.open(dest_path, "rb") as f_in:
                with open(decompressed_path, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
                        
            log.info(f"Decompression completed: {decompressed_path}")
            
    except Exception as e:
        log.error(f"Error occurred during bulk download: {e}")
        log.info("The incomplete file has been preserved. You can run the command again to resume the download.")

# ---------------------------------------------------------------------------
# Bulk File Extractor (Tab to CSV)
# ---------------------------------------------------------------------------
def parse_chunk(lines: list) -> list:
    """Parses a chunk of tab-separated lines into a list of tuples."""
    results = []
    for line in lines:
        parts = line.split('\t')
        if len(parts) >= 2:
            results.append((parts[0].strip(), parts[1].strip()))
    return results

def handle_extract_bulk(args, output_dir: Path, log: logging.Logger):
    """Extracts and converts a bulk mapping file to CSV, then deletes the source files."""
    input_name = args.file
    output_name = args.output
    threads = args.threads
    
    input_path = output_dir / input_name
    
    # Fallback checks (check if compressed/uncompressed counterpart exists if primary is missing)
    if not input_path.exists():
        if input_name.endswith(".gz"):
            uncompressed_path = output_dir / input_name[:-3]
            if uncompressed_path.exists():
                input_path = uncompressed_path
        else:
            compressed_path = output_dir / (input_name + ".gz")
            if compressed_path.exists():
                input_path = compressed_path
                
    if not input_path.exists():
        log.error(f"Source file not found: {output_dir / input_name} (checked compressed/uncompressed counterparts as well).")
        sys.exit(1)
        
    output_csv = output_dir / output_name
    log.info(f"Extracting data from {input_path} -> {output_csv} (threads={threads})...")
    
    # Determine CSV headers dynamically
    if "SMILES" in input_path.name:
        headers = ["CID", "SMILES"]
    elif "Parent" in input_path.name:
        headers = ["CID", "Parent_CID"]
    elif "InChI-Key" in input_path.name:
        headers = ["CID", "InChIKey"]
    else:
        headers = ["CID", "Value"]
        
    start_time = time.time()
    
    # Open file (handle gzip streaming directly if needed)
    if input_path.suffix == ".gz":
        infile = gzip.open(input_path, "rt", encoding="utf-8", errors="ignore")
    else:
        infile = open(input_path, "r", encoding="utf-8", errors="ignore")
        
    try:
        import concurrent.futures
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
            futures = []
            chunk = []
            chunk_size = 100000
            max_active_futures = threads * 3
            total_lines = 0
            
            with open(output_csv, "w", newline="", encoding="utf-8") as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow(headers)
                
                for line in infile:
                    chunk.append(line)
                    total_lines += 1
                    
                    if len(chunk) >= chunk_size:
                        # Throttle submissions to prevent memory bloat
                        while len(futures) >= max_active_futures:
                            done, not_done = concurrent.futures.wait(
                                futures, return_when=concurrent.futures.FIRST_COMPLETED
                            )
                            for f in done:
                                writer.writerows(f.result())
                                futures.remove(f)
                                
                        futures.append(executor.submit(parse_chunk, chunk))
                        chunk = []
                        
                        if total_lines % 5000000 == 0:
                            elapsed = time.time() - start_time
                            log.info(f"  Processed {total_lines:,} lines... ({total_lines/elapsed:.1f} lines/sec)")
                            
                # Submit remaining lines
                if chunk:
                    futures.append(executor.submit(parse_chunk, chunk))
                    
                # Process remaining tasks
                for f in concurrent.futures.as_completed(futures):
                    writer.writerows(f.result())
                    
        log.info(f"Extraction complete! Total lines processed: {total_lines:,}")
        log.info(f"Results saved to: {output_csv}")
        
        infile.close()
        
        # Cleanup source files
        log.info("Cleaning up source files...")
        if input_path.exists():
            input_path.unlink()
            log.info(f"  Deleted source file: {input_path.name}")
            
        # Delete counterpart if exists (e.g. delete the .gz if we read the uncompressed, or vice versa)
        if input_name.endswith(".gz"):
            counterpart = output_dir / input_name[:-3]
        else:
            counterpart = output_dir / (input_name + ".gz")
            
        if counterpart.exists():
            counterpart.unlink()
            log.info(f"  Deleted counterpart file: {counterpart.name}")
            
        log.info("Cleanup complete!")
        
    except Exception as e:
        log.error(f"Extraction failed: {e}")
        if 'infile' in locals():
            infile.close()
        sys.exit(1)

# ---------------------------------------------------------------------------
# Main CLI Entrypoint
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="PubChem Downloader - Fetch structures, identifiers, and bulk mappings from PubChem.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  1. Fetch properties for list of CIDs:
     python pubchem_downloader.py property --cids 2244,1983,3672
     
  2. Fetch properties for a list of chemical names:
     python pubchem_downloader.py property --names "Aspirin,Acetaminophen,Ibuprofen"
     
  3. Search similar compounds using SMILES:
     python pubchem_downloader.py search --smiles "CC(=O)OC1=CC=CC=C1C(=O)O" --type similarity --threshold 90
     
  4. Download a bulk mapping file from FTP (dry run, first 1MB):
     python pubchem_downloader.py download-bulk --file CID-Parent.gz --limit-bytes 1048576
"""
    )
    
    subparsers = parser.add_subparsers(dest="command", required=True, help="Subcommands")
    
    # 1. Subcommand: property
    parser_prop = subparsers.add_parser("property", help="Retrieve chemical properties for CIDs or chemical names.")
    group_input = parser_prop.add_mutually_exclusive_group(required=True)
    group_input.add_argument("--cids", type=str, help="Comma-separated list of compound CIDs.")
    group_input.add_argument("--names", type=str, help="Comma-separated list of chemical names.")
    group_input.add_argument("--input-file", type=str, help="Path to text file containing CIDs or names (one per line).")
    
    parser_prop.add_argument("--input-type", type=str, choices=["cids", "names"], default=None,
                             help="Type of input in input-file (cids or names). Required if --input-file is used.")
    parser_prop.add_argument("--properties", type=str, default=",".join(DEFAULT_PROPERTIES),
                             help=f"Comma-separated list of properties to retrieve. Default: {','.join(DEFAULT_PROPERTIES)}")
    parser_prop.add_argument("--batch-size", type=int, default=100,
                             help="Batch size for PUG-REST requests (max 100-200 is optimal). Default: 100")
    parser_prop.add_argument("--output", type=str, default="pubchem_properties.csv",
                             help="Output CSV filename. Default: pubchem_properties.csv")
    
    # 2. Subcommand: search
    parser_search = subparsers.add_parser("search", help="Perform structure similarity or substructure search.")
    parser_search.add_argument("--smiles", type=str, required=True, help="Query chemical SMILES structure.")
    parser_search.add_argument("--type", type=str, choices=["similarity", "substructure"], default="similarity",
                               help="Type of structure search. Default: similarity")
    parser_search.add_argument("--threshold", type=int, default=90,
                               help="Similarity threshold percentage (for similarity search only, e.g., 90). Default: 90")
    parser_search.add_argument("--properties", type=str, default=",".join(DEFAULT_PROPERTIES),
                               help=f"Comma-separated properties to retrieve for matches. Default: {','.join(DEFAULT_PROPERTIES)}")
    parser_search.add_argument("--output", type=str, default="pubchem_search_results.csv",
                               help="Output CSV filename. Default: pubchem_search_results.csv")
    
    # 3. Subcommand: download-bulk
    parser_bulk = subparsers.add_parser("download-bulk", help="Download bulk whole-database mapping files from PubChem FTP.")
    parser_bulk.add_argument("--file", type=str, required=True,
                             choices=["CID-SMILES.gz", "CID-InChI-Key.gz", "CID-Synonym-filtered.gz", "CID-Parent.gz"],
                             help="Name of the mapping file to download.")
    parser_bulk.add_argument("--no-decompress", action="store_true", help="Do not automatically decompress the downloaded gzip archive.")
    parser_bulk.add_argument("--limit-bytes", type=int, default=None,
                             help="Limit download to first N bytes (useful for dry runs and testing).")
                             
    # 4. Subcommand: extract-bulk
    parser_extract = subparsers.add_parser("extract-bulk", help="Extract and convert downloaded bulk mapping files to CSV format, then delete source files.")
    parser_extract.add_argument("--file", type=str, required=True,
                                 choices=["CID-SMILES.gz", "CID-SMILES", "CID-InChI-Key.gz", "CID-Parent.gz"],
                                 help="Name of the bulk file to extract.")
    parser_extract.add_argument("--output", type=str, default="pubchem_extracted_mappings.csv",
                                 help="Output CSV filename. Default: pubchem_extracted_mappings.csv")
    parser_extract.add_argument("--threads", type=int, default=4,
                                 help="Number of concurrent threads for parsing. Default: 4")
                                 
    args = parser.parse_args()
    
    # Base directories
    default_dir = str(BASE_DIR / "pubchem_data")
    output_dir = Path(default_dir)
    log = setup_logging(output_dir)
    
    log.info("=" * 60)
    log.info("  PubChem Data Collector and Search Utility")
    log.info("=" * 60)
    
    # -----------------------------------------------------------------------
    # Action: property
    # -----------------------------------------------------------------------
    if args.command == "property":
        # Parse inputs
        cids = []
        names = []
        
        if args.cids:
            cids = [int(c.strip()) for c in args.cids.split(",") if c.strip().isdigit()]
        elif args.names:
            names = [n.strip() for n in args.names.split(",") if n.strip()]
        elif args.input_file:
            if not args.input_type:
                log.error("Error: --input-type (cids or names) must be specified when using --input-file.")
                sys.exit(1)
                
            input_path = Path(args.input_file)
            if not input_path.exists():
                log.error(f"Error: Input file does not exist: {input_path}")
                sys.exit(1)
                
            with open(input_path, "r", encoding="utf-8") as fh:
                items = [line.strip() for line in fh if line.strip()]
                
            if args.input_type == "cids":
                cids = [int(item) for item in items if item.isdigit()]
            else:
                names = items
                
        # Resolve names to CIDs if necessary
        if names:
            log.info(f"Resolving {len(names):,} names to PubChem CIDs...")
            resolved_mapping = {}
            for name in names:
                cid = resolve_name_to_cid(name, log)
                if cid:
                    resolved_mapping[name] = cid
                    cids.append(cid)
                    log.info(f"  Resolved: '{name}' -> CID {cid}")
                else:
                    log.warning(f"  Could not resolve name: '{name}'")
                    
            if not cids:
                log.error("No names could be resolved to CIDs. Exiting.")
                sys.exit(0)
                
        # Retrieve properties
        properties_list = [p.strip() for p in args.properties.split(",") if p.strip()]
        properties_data = get_properties_for_cids(cids, properties_list, args.batch_size, log)
        
        if not properties_data:
            log.warning("No properties retrieved.")
            sys.exit(0)
            
        # Write properties to CSV
        output_csv = output_dir / args.output
        log.info(f"Writing properties to CSV file: {output_csv}")
        
        # Determine CSV columns (ensure CID is first if requested)
        columns = [p for p in properties_list if p in properties_data[0]]
        # Catch any keys in properties_data that might not be in our list
        for k in properties_data[0].keys():
            if k not in columns:
                columns.append(k)
                
        with open(output_csv, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=columns)
            writer.writeheader()
            for row in properties_data:
                # Clean nested dictionaries or lists if any (standard PUG-REST is usually flat for properties)
                cleaned_row = {}
                for col in columns:
                    val = row.get(col, "")
                    if isinstance(val, (dict, list)):
                        cleaned_row[col] = json.dumps(val)
                    else:
                        cleaned_row[col] = val
                writer.writerow(cleaned_row)
                
        log.info(f"Property collection complete. Results saved to {output_csv}")
        
    # -----------------------------------------------------------------------
    # Action: search
    # -----------------------------------------------------------------------
    elif args.command == "search":
        matching_cids = run_structure_search(args.smiles, args.type, args.threshold, log)
        
        if not matching_cids:
            log.warning("No matching compounds found.")
            sys.exit(0)
            
        # Retrieve properties for matches
        properties_list = [p.strip() for p in args.properties.split(",") if p.strip()]
        properties_data = get_properties_for_cids(matching_cids, properties_list, 100, log)
        
        if not properties_data:
            log.warning("Could not retrieve properties for matching compounds.")
            sys.exit(0)
            
        # Write to CSV
        output_csv = output_dir / args.output
        log.info(f"Writing search results to: {output_csv}")
        
        columns = [p for p in properties_list if p in properties_data[0]]
        for k in properties_data[0].keys():
            if k not in columns:
                columns.append(k)
                
        with open(output_csv, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=columns)
            writer.writeheader()
            for row in properties_data:
                cleaned_row = {}
                for col in columns:
                    val = row.get(col, "")
                    if isinstance(val, (dict, list)):
                        cleaned_row[col] = json.dumps(val)
                    else:
                        cleaned_row[col] = val
                writer.writerow(cleaned_row)
                
        log.info(f"Structure search complete. Results saved to {output_csv}")
        
    # -----------------------------------------------------------------------
    # Action: download-bulk
    # -----------------------------------------------------------------------
    elif args.command == "download-bulk":
        decompress = not args.no_decompress
        download_bulk_file(args.file, output_dir, decompress, args.limit_bytes, log)
        
    # -----------------------------------------------------------------------
    # Action: extract-bulk
    # -----------------------------------------------------------------------
    elif args.command == "extract-bulk":
        handle_extract_bulk(args, output_dir, log)

if __name__ == "__main__":
    main()
