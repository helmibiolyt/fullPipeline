#!/usr/bin/env python3
"""
NCI Thesaurus (NCIt) Downloader and Parser
Author: Antigravity AI Coding Assistant
Description: Programmatically retrieves NCI Thesaurus (NCIt) terminology files,
             CUI mappings, ontology mappings, neoplasm core data, and drug lists
             from the official NCI EVS server. It parses these files into clean,
             structured CSVs, with robust download resumption and progress tracking.
"""

import os
import sys
import csv
import json
import zipfile
import argparse
import logging
import time
import shutil
from pathlib import Path
import requests

# Resolve all output paths relative to this script's folder (pipeline contract)
BASE_DIR = Path(__file__).resolve().parent

# Logger setup
logger = logging.getLogger("nci_thesaurus_downloader")

# HTTP headers to play nice with EVS servers
HEADERS = {
    "User-Agent": "BiolytInternNCItDownloader/1.0 (Python; requests; Antigravity)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Default configuration of files to download and parse
DEFAULT_TARGETS = {
    "core_flat": {
        "url": "https://evs.nci.nih.gov/ftp1/NCI_Thesaurus/Thesaurus.FLAT.zip",
        "filename": "Thesaurus.FLAT.zip",
        "category": "core",
        "type": "zip",
        "dest_csv": "nci_thesaurus_concepts.csv"
    },
    "cui_map": {
        "url": "https://evs.nci.nih.gov/ftp1/NCI_Thesaurus/nci_code_cui_map.dat",
        "filename": "nci_code_cui_map.dat",
        "category": "core",
        "type": "dat",
        "dest_csv": "nci_code_cui_map.csv"
    },
    "mapping_go": {
        "url": "https://evs.nci.nih.gov/ftp1/NCI_Thesaurus/Mappings/GO-NCIt_Mapping.txt",
        "filename": "GO-NCIt_Mapping.txt",
        "category": "mappings",
        "type": "txt",
        "dest_csv": "mapping_go_ncit.csv"
    },
    "mapping_chebi": {
        "url": "https://evs.nci.nih.gov/ftp1/NCI_Thesaurus/Mappings/NCIt-ChEBI_Mapping.txt",
        "filename": "NCIt-ChEBI_Mapping.txt",
        "category": "mappings",
        "type": "txt",
        "dest_csv": "mapping_ncit_chebi.csv"
    },
    "mapping_hgnc": {
        "url": "https://evs.nci.nih.gov/ftp1/NCI_Thesaurus/Mappings/NCIt-HGNC_Mapping.txt",
        "filename": "NCIt-HGNC_Mapping.txt",
        "category": "mappings",
        "type": "txt",
        "dest_csv": "mapping_ncit_hgnc.csv"
    },
    "mapping_swissprot": {
        "url": "https://evs.nci.nih.gov/ftp1/NCI_Thesaurus/Mappings/NCIt-SwissProt_Mapping.txt",
        "filename": "NCIt-SwissProt_Mapping.txt",
        "category": "mappings",
        "type": "txt",
        "dest_csv": "mapping_ncit_swissprot.csv"
    },
    "neoplasm_core": {
        "url": "https://evs.nci.nih.gov/ftp1/NCI_Thesaurus/Neoplasm/Neoplasm_Core.csv",
        "filename": "Neoplasm_Core.csv",
        "category": "neoplasm",
        "type": "csv",
        "dest_csv": "neoplasm_core.csv"
    },
    "neoplasm_molecular": {
        "url": "https://evs.nci.nih.gov/ftp1/NCI_Thesaurus/Neoplasm/Neoplasm_Core_Rels_NCIt_Molecular.csv",
        "filename": "Neoplasm_Core_Rels_NCIt_Molecular.csv",
        "category": "neoplasm",
        "type": "csv",
        "dest_csv": "neoplasm_core_rels_ncit_molecular.csv"
    },
    "antineoplastic_agent": {
        "url": "https://evs.nci.nih.gov/ftp1/NCI_Thesaurus/Drug_or_Substance/Antineoplastic_Agent.txt",
        "filename": "Antineoplastic_Agent.txt",
        "category": "drugs",
        "type": "txt",
        "dest_csv": "antineoplastic_agents.csv"
    }
}


def setup_logging(verbose: bool):
    """Configures the logging output level."""
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)]
    )


class ProgressTracker:
    """Manages the download and parsing state to support resuming on collapse."""
    def __init__(self, output_dir: Path):
        self.tracker_path = output_dir / "progress_tracker.json"
        self.output_dir = output_dir
        self.state = {
            "version": "1.0",
            "last_updated": "",
            "downloads": {},
            "processing": {}
        }
        self.load()

    def load(self):
        """Loads state from the progress tracker JSON file if it exists."""
        if self.tracker_path.exists():
            try:
                with open(self.tracker_path, "r", encoding="utf-8") as f:
                    self.state = json.load(f)
                logger.info(f"Loaded existing progress state from {self.tracker_path}")
            except Exception as e:
                logger.warning(f"Failed to parse progress tracker {self.tracker_path} ({e}). Initializing fresh.")

    def save(self):
        """Saves current state to the progress tracker JSON file."""
        self.state["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            with open(self.tracker_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save progress state to {self.tracker_path}: {e}")

    def get_download_status(self, key: str) -> dict:
        """Gets download status for a key."""
        return self.state["downloads"].get(key, {"status": "pending"})

    def update_download_status(self, key: str, status: str, local_path: str = None, size_bytes: int = None, error: str = None):
        """Updates download status and saves state."""
        record = self.state["downloads"].setdefault(key, {})
        record["status"] = status
        if local_path:
            record["local_path"] = str(local_path)
        if size_bytes is not None:
            record["size_bytes"] = size_bytes
        if error:
            record["error"] = error
        elif "error" in record:
            del record["error"]
        self.save()

    def get_process_status(self, key: str) -> dict:
        """Gets processing status for a key."""
        return self.state["processing"].get(key, {"status": "pending"})

    def update_process_status(self, key: str, status: str, error: str = None):
        """Updates processing status and saves state."""
        record = self.state["processing"].setdefault(key, {})
        record["status"] = status
        if error:
            record["error"] = error
        elif "error" in record:
            del record["error"]
        self.save()


def download_file_with_resume(url: str, dest_path: Path, tracker: ProgressTracker, target_key: str, max_retries: int = 3) -> bool:
    """Downloads a file supporting resumption via HTTP Range headers."""
    logger.info(f"Checking download for: {url}")
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Check if download is already complete according to tracker and file exists
    status_info = tracker.get_download_status(target_key)
    if status_info.get("status") == "completed" and dest_path.exists():
        expected_size = status_info.get("size_bytes")
        actual_size = dest_path.stat().st_size
        if expected_size is None or actual_size == expected_size:
            logger.info(f"File '{dest_path.name}' is already complete on disk. Skipping download.")
            return True
        else:
            logger.warning(f"File size mismatch for '{dest_path.name}': expected {expected_size}, got {actual_size}. Redownloading/resuming...")

    for attempt in range(1, max_retries + 1):
        try:
            # 1. Fetch remote metadata using HEAD request to get total size and range support
            head_resp = requests.head(url, headers=HEADERS, timeout=30)
            head_resp.raise_for_status()
            
            total_size = int(head_resp.headers.get("content-length", 0))
            accept_ranges = head_resp.headers.get("accept-ranges", "").lower()
            supports_resume = "bytes" in accept_ranges or head_resp.status_code == 206
            
            tracker.update_download_status(target_key, "downloading", local_path=dest_path, size_bytes=total_size)
            
            # 2. Determine if we can resume
            current_size = dest_path.stat().st_size if dest_path.exists() else 0
            
            req_headers = HEADERS.copy()
            open_mode = "wb"
            
            if 0 < current_size < total_size and supports_resume:
                logger.info(f"Resuming download for '{dest_path.name}' at byte {current_size}/{total_size} (Attempt {attempt}/{max_retries})")
                req_headers["Range"] = f"bytes={current_size}-"
                open_mode = "ab"
            elif current_size == total_size:
                logger.info(f"File '{dest_path.name}' is already complete ({total_size} bytes).")
                tracker.update_download_status(target_key, "completed", size_bytes=total_size)
                return True
            else:
                logger.info(f"Starting fresh download for '{dest_path.name}' ({total_size / (1024*1024):.2f} MB) (Attempt {attempt}/{max_retries})")
                current_size = 0
            
            # 3. Perform download
            response = requests.get(url, headers=req_headers, stream=True, timeout=60)
            
            # If server doesn't honor range, fallback to fresh download
            if response.status_code == 200 and open_mode == "ab":
                logger.warning("Server ignored Range header. Restarting download from beginning.")
                open_mode = "wb"
                current_size = 0
                
            response.raise_for_status()
            
            chunk_size = 1024 * 1024  # 1 MB chunk size
            downloaded = current_size
            last_log_time = time.time()
            
            with open(dest_path, open_mode) as f:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        
                        # Throttled logging (at most once every 3 seconds)
                        if total_size > 0 and time.time() - last_log_time > 3:
                            percent = (downloaded / total_size) * 100
                            logger.info(f"  '{dest_path.name}': {downloaded / (1024*1024):.2f} MB / {total_size / (1024*1024):.2f} MB ({percent:.1f}%)")
                            last_log_time = time.time()
                            
            logger.info(f"Finished downloading '{dest_path.name}' ({downloaded} bytes)")
            tracker.update_download_status(target_key, "completed", size_bytes=downloaded)
            return True
            
        except Exception as e:
            logger.error(f"Error downloading {url} (Attempt {attempt}/{max_retries}): {e}")
            tracker.update_download_status(target_key, "failed", error=str(e))
            if attempt < max_retries:
                time.sleep(2 * attempt)
            else:
                return False
    return False


def decode_bytes_with_fallback(content_bytes: bytes) -> str:
    """Decodes raw bytes, attempting UTF-8 first, falling back to Latin-1."""
    try:
        return content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return content_bytes.decode("latin-1")


def parse_core_thesaurus(zip_path: Path, output_csv: Path) -> bool:
    """Extracts and parses the Thesaurus.txt flat file inside Thesaurus.FLAT.zip."""
    logger.info(f"Parsing NCI Thesaurus core concepts from {zip_path} -> {output_csv}")
    
    if not zip_path.exists():
        logger.error(f"ZIP file not found at {zip_path}")
        return False
        
    try:
        with zipfile.ZipFile(zip_path) as z:
            # Locate Thesaurus.txt inside the archive
            txt_filename = None
            for name in z.namelist():
                if name.endswith("Thesaurus.txt"):
                    txt_filename = name
                    break
                    
            if not txt_filename:
                logger.error("Could not find 'Thesaurus.txt' inside the zip archive.")
                return False
                
            logger.info(f"Extracting and parsing '{txt_filename}' from ZIP...")
            
            # Setup CSV Writer
            with open(output_csv, "w", newline="", encoding="utf-8") as f_out:
                writer = csv.writer(f_out)
                writer.writerow([
                    "code", 
                    "concept_iri", 
                    "parents", 
                    "preferred_name", 
                    "synonyms", 
                    "definition", 
                    "display_name", 
                    "concept_status", 
                    "semantic_types", 
                    "concept_in_subsets"
                ])
                
                # Streaming read from ZIP file
                count = 0
                with z.open(txt_filename) as f_in:
                    for line_bytes in f_in:
                        line = decode_bytes_with_fallback(line_bytes).strip("\r\n")
                        if not line:
                            continue
                            
                        # Tab-delimited fields
                        parts = line.split("\t")
                        
                        # Pad with empty strings if there are fewer columns than expected (9 columns)
                        if len(parts) < 9:
                            parts = parts + [""] * (9 - len(parts))
                        elif len(parts) > 9:
                            # If there are more columns, join the trailing columns to subset
                            parts[8] = "|".join(parts[8:])
                            parts = parts[:9]
                            
                        code = parts[0].strip()
                        concept_iri = parts[1].strip()
                        parents = parts[2].strip()
                        synonyms_str = parts[3].strip()
                        definition = parts[4].strip()
                        display_name = parts[5].strip()
                        concept_status = parts[6].strip()
                        semantic_types = parts[7].strip()
                        concept_in_subsets = parts[8].strip()
                        
                        # The first synonym is the preferred name
                        preferred_name = ""
                        if synonyms_str:
                            syns = [s.strip() for s in synonyms_str.split("|") if s.strip()]
                            if syns:
                                preferred_name = syns[0]
                                
                        writer.writerow([
                            code,
                            concept_iri,
                            parents,
                            preferred_name,
                            synonyms_str,
                            definition,
                            display_name,
                            concept_status,
                            semantic_types,
                            concept_in_subsets
                        ])
                        
                        count += 1
                        if count % 20000 == 0:
                            logger.info(f"  Processed {count} concepts...")
                            
            logger.info(f"Successfully processed {count} concepts. Saved to {output_csv}")
            return True
            
    except Exception as e:
        logger.error(f"Error parsing core thesaurus zip: {e}")
        return False


def parse_cui_map(dat_path: Path, output_csv: Path) -> bool:
    """Parses the nci_code_cui_map.dat file (pipe-separated) to a structured CSV."""
    logger.info(f"Parsing CUI mappings from {dat_path} -> {output_csv}")
    if not dat_path.exists():
        logger.error(f"DAT file not found at {dat_path}")
        return False
        
    try:
        count = 0
        with open(dat_path, "rb") as f_bytes, open(output_csv, "w", newline="", encoding="utf-8") as f_out:
            writer = csv.writer(f_out)
            writer.writerow(["code", "cui"])
            
            for line_bytes in f_bytes:
                line = decode_bytes_with_fallback(line_bytes).strip()
                if not line or line.startswith("#"):
                    continue
                # Format: code|CUI|
                parts = [p.strip() for p in line.split("|") if p.strip()]
                if len(parts) >= 2:
                    writer.writerow([parts[0], parts[1]])
                    count += 1
                    
        logger.info(f"Successfully parsed {count} CUI mappings to {output_csv}")
        return True
    except Exception as e:
        logger.error(f"Error parsing CUI map: {e}")
        return False


def parse_simple_mapping(txt_path: Path, output_csv: Path, mapping_name: str) -> bool:
    """Parses standard simple mapping text files (no headers, tab-delimited)."""
    logger.info(f"Parsing {mapping_name} mapping from {txt_path} -> {output_csv}")
    if not txt_path.exists():
        logger.error(f"Mapping file not found at {txt_path}")
        return False
        
    try:
        count = 0
        with open(txt_path, "rb") as f_bytes, open(output_csv, "w", newline="", encoding="utf-8") as f_out:
            writer = csv.writer(f_out)
            
            # Map headers appropriately
            if "go" in mapping_name.lower():
                writer.writerow(["go_id", "ncit_code"])
            elif "chebi" in mapping_name.lower():
                writer.writerow(["ncit_code", "chebi_id"])
            elif "hgnc" in mapping_name.lower():
                writer.writerow(["ncit_code", "hgnc_id"])
            else:
                writer.writerow(["column_1", "column_2"])
                
            for line_bytes in f_bytes:
                line = decode_bytes_with_fallback(line_bytes).strip()
                if not line:
                    continue
                parts = [p.strip() for p in line.split("\t") if p.strip()]
                if len(parts) >= 2:
                    writer.writerow([parts[0], parts[1]])
                    count += 1
                    
        logger.info(f"Successfully parsed {count} rows of {mapping_name} to {output_csv}")
        return True
    except Exception as e:
        logger.error(f"Error parsing {mapping_name} mapping: {e}")
        return False


def parse_swissprot_mapping(txt_path: Path, output_csv: Path) -> bool:
    """Parses SwissProt mapping text file (has header, tab-delimited)."""
    logger.info(f"Parsing SwissProt mapping from {txt_path} -> {output_csv}")
    if not txt_path.exists():
        logger.error(f"SwissProt mapping file not found at {txt_path}")
        return False
        
    try:
        count = 0
        with open(txt_path, "rb") as f_bytes:
            content_bytes = f_bytes.read()
        content = decode_bytes_with_fallback(content_bytes)
        
        reader = csv.reader(content.splitlines(), delimiter="\t")
        with open(output_csv, "w", newline="", encoding="utf-8") as f_out:
            writer = csv.writer(f_out)
            
            header = next(reader, None)
            if header:
                writer.writerow(["ncit_code", "swissprot_id", "ncit_preferred_name"])
                
            for row in reader:
                if not row:
                    continue
                row_cleaned = [col.strip() for col in row]
                if len(row_cleaned) >= 2:
                    if len(row_cleaned) < 3:
                        row_cleaned = row_cleaned + [""] * (3 - len(row_cleaned))
                    writer.writerow(row_cleaned[:3])
                    count += 1
                    
        logger.info(f"Successfully parsed {count} SwissProt mappings to {output_csv}")
        return True
    except Exception as e:
        logger.error(f"Error parsing SwissProt mapping: {e}")
        return False


def parse_neoplasm_file(csv_path: Path, output_csv: Path) -> bool:
    """Sanitizes and reformats downloaded Neoplasm CSV files to ensure standard quoting and encoding."""
    logger.info(f"Formatting Neoplasm CSV from {csv_path} -> {output_csv}")
    if not csv_path.exists():
        logger.error(f"Neoplasm file not found at {csv_path}")
        return False
        
    try:
        count = 0
        with open(csv_path, "rb") as f_bytes:
            content_bytes = f_bytes.read()
        content = decode_bytes_with_fallback(content_bytes)
        
        reader = csv.reader(content.splitlines())
        with open(output_csv, "w", newline="", encoding="utf-8") as f_out:
            writer = csv.writer(f_out)
            
            for row in reader:
                if not row:
                    continue
                writer.writerow([col.strip() for col in row])
                count += 1
                
        logger.info(f"Successfully cleaned and saved {count} rows of neoplasm data to {output_csv}")
        return True
    except Exception as e:
        logger.error(f"Error copying/formatting neoplasm file: {e}")
        return False


def parse_drugs_file(txt_path: Path, output_csv: Path) -> bool:
    """Parses Drug/Substance text lists (e.g. Antineoplastic_Agent.txt) which may or may not have headers."""
    logger.info(f"Parsing Drugs/Substances from {txt_path} -> {output_csv}")
    if not txt_path.exists():
        logger.error(f"Drugs file not found at {txt_path}")
        return False
        
    try:
        count = 0
        rows = []
        max_cols = 0
        
        # Read raw bytes and decode with fallback
        with open(txt_path, "rb") as f_bytes:
            content_bytes = f_bytes.read()
        content = decode_bytes_with_fallback(content_bytes)
        
        for line in content.splitlines():
            line = line.strip("\r\n")
            if not line:
                continue
            parts = [p.strip() for p in line.split("\t")]
            rows.append(parts)
            if len(parts) > max_cols:
                max_cols = len(parts)
                    
        if not rows:
            logger.warning("No rows found in drugs list.")
            return False
            
        # Determine headers
        first_row = rows[0]
        has_header = any(word in first_row[0].lower() for word in ["code", "name", "id", "concept", "preferred"])
        
        with open(output_csv, "w", newline="", encoding="utf-8") as f_out:
            writer = csv.writer(f_out)
            
            if has_header:
                headers = [h if h else f"col_{i+1}" for i, h in enumerate(first_row)]
                writer.writerow(headers)
                data_rows = rows[1:]
            else:
                # Generate generic headers
                headers = [f"column_{i+1}" for i in range(max_cols)]
                if max_cols >= 2:
                    headers[0] = "ncit_code"
                    headers[1] = "preferred_name"
                writer.writerow(headers)
                data_rows = rows
                
            for row in data_rows:
                # Pad to max_cols
                if len(row) < max_cols:
                    row = row + [""] * (max_cols - len(row))
                writer.writerow(row[:max_cols])
                count += 1
                
        logger.info(f"Successfully parsed {count} drug/substance records to {output_csv}")
        return True
    except Exception as e:
        logger.error(f"Error parsing drugs file: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Download and parse NCI Thesaurus terminology database and subsets.")
    parser.add_argument(
        "--output-dir", 
        default=str(BASE_DIR / "nci_thesaurus_data"),
        help="Directory to save downloaded files and CSV outputs (default: Ontologies & Standards/nci_thesaurus_data)"
    )
    parser.add_argument(
        "--keep-raw", 
        action="store_true", 
        help="Keep raw download files (ZIP, TXT, DAT, CSV) after parsing (default: delete/cleanup)"
    )
    parser.add_argument(
        "--download-only", 
        action="store_true", 
        help="Only download the raw database files, do not parse them"
    )
    parser.add_argument(
        "--process-only", 
        action="store_true", 
        help="Only parse local raw database files if already downloaded, do not fetch from server"
    )
    parser.add_argument(
        "--skip-mappings", 
        action="store_true", 
        help="Skip downloading and processing external mappings (HGNC, ChEBI, SwissProt, GO)"
    )
    parser.add_argument(
        "--skip-neoplasm", 
        action="store_true", 
        help="Skip downloading and processing Neoplasm Core subsets"
    )
    parser.add_argument(
        "--skip-drugs", 
        action="store_true", 
        help="Skip downloading and processing Drug/Substance subsets"
    )
    parser.add_argument(
        "--verbose", 
        action="store_true", 
        help="Enable verbose DEBUG logging"
    )
    args = parser.parse_args()
    
    setup_logging(args.verbose)
    
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Use a raw/ subdirectory for downloaded files to avoid case-insensitive collisions on Windows
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    
    tracker = ProgressTracker(output_dir)
    
    # Filter targets based on user-supplied options
    active_targets = {}
    for key, target in DEFAULT_TARGETS.items():
        category = target["category"]
        if category == "mappings" and args.skip_mappings:
            continue
        if category == "neoplasm" and args.skip_neoplasm:
            continue
        if category == "drugs" and args.skip_drugs:
            continue
        active_targets[key] = target
        
    logger.info("=" * 60)
    logger.info("  NCI Thesaurus Downloader and Parser")
    logger.info(f"  Output Directory : {output_dir}")
    logger.info(f"  Raw Files Subdir : {raw_dir}")
    logger.info(f"  Keep Raw Files   : {args.keep_raw}")
    logger.info(f"  Active Categories: {list(set(t['category'] for t in active_targets.values()))}")
    logger.info("=" * 60)
    
    # 1. Download phase
    if not args.process_only:
        logger.info("--- Starting Download Phase ---")
        for key, target in active_targets.items():
            dest_file = raw_dir / target["filename"]
            success = download_file_with_resume(target["url"], dest_file, tracker, key)
            if not success:
                logger.error(f"Failed to download required file: {target['filename']}")
                tracker.update_download_status(key, "failed", error="Download failed")
            else:
                logger.info(f"Download complete: {target['filename']}")
                
    # 2. Parsing phase
    if not args.download_only:
        logger.info("--- Starting Parsing/Processing Phase ---")
        for key, target in active_targets.items():
            raw_path = raw_dir / target["filename"]
            csv_path = output_dir / target["dest_csv"]
            
            # Check if file has already been parsed successfully
            proc_status = tracker.get_process_status(key)
            if proc_status.get("status") == "completed" and csv_path.exists():
                logger.info(f"CSV for '{key}' is already parsed. Skipping.")
                continue
                
            if not raw_path.exists():
                logger.error(f"Cannot parse '{key}' because raw file is missing: {raw_path}")
                tracker.update_process_status(key, "failed", error="Raw file missing")
                continue
                
            tracker.update_process_status(key, "parsing")
            
            success = False
            if key == "core_flat":
                success = parse_core_thesaurus(raw_path, csv_path)
            elif key == "cui_map":
                success = parse_cui_map(raw_path, csv_path)
            elif key in ["mapping_go", "mapping_chebi", "mapping_hgnc"]:
                success = parse_simple_mapping(raw_path, csv_path, key)
            elif key == "mapping_swissprot":
                success = parse_swissprot_mapping(raw_path, csv_path)
            elif key in ["neoplasm_core", "neoplasm_molecular"]:
                success = parse_neoplasm_file(raw_path, csv_path)
            elif key == "antineoplastic_agent":
                success = parse_drugs_file(raw_path, csv_path)
                
            if success:
                logger.info(f"Completed parsing for '{key}' -> {csv_path.name}")
                tracker.update_process_status(key, "completed")
            else:
                logger.error(f"Failed parsing for '{key}'")
                tracker.update_process_status(key, "failed", error="Parsing failed")
                
        # 3. Cleanup phase (delete raw files if not keep_raw)
        if not args.keep_raw:
            logger.info("--- Starting Cleanup Phase ---")
            if raw_dir.exists():
                try:
                    shutil.rmtree(raw_dir)
                    logger.info("Removed temporary raw downloads subdirectory.")
                except Exception as e:
                    logger.warning(f"Failed to remove raw subdirectory: {e}")
            
            # Clean up residual text mapping files and progress tracker
            for txt_file in output_dir.glob("*.txt"):
                try:
                    txt_file.unlink()
                    logger.info(f"Cleaned up intermediate text file: {txt_file.name}")
                except Exception as e:
                    logger.warning(f"Failed to remove {txt_file.name}: {e}")
            
            tracker_file = output_dir / "progress_tracker.json"
            if tracker_file.exists():
                try:
                    tracker_file.unlink()
                    logger.info(f"Cleaned up progress tracker: {tracker_file.name}")
                except Exception as e:
                    logger.warning(f"Failed to remove tracker file: {e}")
                        
    logger.info("=" * 60)
    logger.info("  NCI THESAURUS PROCESS COMPLETE")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
