#!/usr/bin/env python3
"""
canada_dpd_downloader.py
========================
A resilient, multi-threaded, and resumable data collector for the Health Canada 
Drug Product Database (DPD). Downloads bulk extracts, compiles them into a 
fully indexed local SQLite database, generates consolidated master datasets, 
and features a concurrent, rate-limit-aware API crawler.

Modes of Operation:
  1. download-bulk: Downloads all four status-specific ZIP archives (Marketed, 
                    Approved, Cancelled, Dormant) using HTTP Range headers to 
                    support resumption. Extracts them into isolated directories 
                    to prevent file overwrites, compiles them into a unified 
                    local SQLite database (tracking completion state), and 
                    exports joined master CSV and JSON datasets.
  2. api-enrich:    Queries the DPD REST API concurrently using a thread pool 
                    to enrich local records with real-time JSON details, 
                    featuring exponential backoff for rate limits and stateful 
                    resumption.
  3. search:        Queries the compiled local database to search for drug 
                    products by brand name, DIN, or active ingredient.

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
import sqlite3
import zipfile
import threading
from pathlib import Path
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
DPD_BASE_URL = "https://www.canada.ca/content/dam/hc-sc/documents/services/drug-product-database/"
API_BASE_URL = "https://health-products.canada.ca/api/drug/"

BULK_FILES = {
    "marketed": "allfiles.zip",
    "approved": "allfiles_ap.zip",
    "inactive": "allfiles_ia.zip",
    "dormant": "allfiles_dr.zip"
}

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = str(BASE_DIR / "canada_dpd_data")
MAX_RETRIES = 5
CHUNK_SIZE = 8192
DEFAULT_THREADS = 4

# -- Relational Schemas ------------------------------------------------------
# Raw DPD text files do not contain header rows. The following definitions 
# map each file type to its exact column structure based on live database extracts.
SCHEMAS = {
    "drug": [
        "DRUG_CODE", "WEB_REFERRAL_FLAG", "PRODUCT_CATEGORIZATION", 
        "DRUG_IDENTIFICATION_NUMBER", "BRAND_NAME", "DESCRIPTOR", 
        "PEDIATRIC_FLAG", "ACCESS_DEMENTIA_FLAG", "THERAPEUTIC_CLASS_FLAG", 
        "LAST_UPDATE_DATE", "EXPIRY_DATE", "PRODUCT_CATEGORIZATION_FR", 
        "DESCRIPTOR_FR", "EXTERNAL_STATUS_CODE"
    ],
    "ingred": [
        "DRUG_CODE", "ACTIVE_INGREDIENT_CODE", "INGREDIENT_NAME", 
        "INGREDIENT_FLAG", "STRENGTH", "STRENGTH_UNIT", "STRENGTH_TYPE", 
        "DOSAGE_VALUE", "DOSAGE_UNIT", "RARE_INGREDIENT_FLAG", 
        "HISTORIC_FREEFORM", "INGREDIENT_NAME_FR", "STRENGTH_UNIT_FR", 
        "DOSAGE_UNIT_FR", "EXTRA_FLAG"
    ],
    "comp": [
        "DRUG_CODE", "MFR_CODE", "COMPANY_CODE", "COMPANY_NAME", 
        "COMPANY_TYPE", "ADDRESS_MAILING_FLAG", "ADDRESS_BILLING_FLAG", 
        "ADDRESS_NOTIFICATION_FLAG", "ADDRESS_OTHER_FLAG", "SUITE_NUMBER", 
        "STREET_NAME", "CITY_NAME", "PROVINCE_NAME", "COUNTRY_NAME", 
        "POSTAL_CODE", "POST_OFFICE_BOX", "PROVINCE_NAME_FR", "COUNTRY_NAME_FR"
    ],
    "route": [
        "DRUG_CODE", "ROUTE_OF_ADMINISTRATION_CODE", 
        "ROUTE_OF_ADMINISTRATION_EN", "ROUTE_OF_ADMINISTRATION_FR"
    ],
    "form": [
        "DRUG_CODE", "PHARMACEUTICAL_FORM_CODE", 
        "PHARMACEUTICAL_FORM_EN", "PHARMACEUTICAL_FORM_FR"
    ],
    "status": [
        "DRUG_CODE", "CURRENT_STATUS_FLAG", "STATUS_EN", 
        "HISTORY_DATE", "STATUS_FR", "EXTRA_FIELD_1", "EXTRA_FIELD_2"
    ],
    "ther": [
        "DRUG_CODE", "TC_ATC_NUMBER", "TC_ATC", "TC_AHFS_NUMBER"
    ],
    "schedule": [
        "DRUG_CODE", "SCHEDULE_EN", "SCHEDULE_FR"
    ],
    "package": [
        "DRUG_CODE", "UPC", "PACKAGE_SIZE_UNIT_EN", "PACKAGE_SIZE", 
        "PRODUCT_INFORMATION_EN", "PACKAGE_TYPE_EN", "PACKAGE_SIZE_UNIT_FR", 
        "PRODUCT_INFORMATION_FR"
    ],
    "vet": [
        "DRUG_CODE", "VETERINARY_SPECIES_EN", "VETERINARY_SPECIES_FR", 
        "EXTRA_VET_FIELD"
    ],
    "pharm": [
        "DRUG_CODE", "PHARMACEUTICAL_STD"
    ],
    "biosimilar": [
        "DRUG_CODE", "BIOSIMILAR_FLAG_EN", "BIOSIMILAR_FLAG_FR", "BIOSIMILAR_TYPE_CODE"
    ]
}

# -- Logging Setup -----------------------------------------------------------
log = logging.getLogger("canada_dpd_downloader")

def setup_logging(output_dir: Path):
    """Sets up file and console logging."""
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / "canada_dpd_downloader.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] (%(threadName)s) %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout)
        ]
    )

# -- HTTP Session Configuration ----------------------------------------------
def build_session() -> requests.Session:
    """Builds a requests.Session with retries and default headers."""
    session = requests.Session()
    retry_strategy = Retry(
        total=MAX_RETRIES,
        backoff_factor=2.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    
    session.headers.update({
        "User-Agent": "BiolytInternCanadaDPDDownloader/1.0 (Python; requests)"
    })
    return session

# -- Range-Based Resumable Downloader ----------------------------------------
def download_file_with_resume(session: requests.Session, url: str, dest_path: Path):
    """Downloads a file using HTTP Range headers to support resuming from interruptions."""
    temp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")
    
    # Send HEAD request to determine remote file size
    try:
        head = session.head(url, timeout=10, allow_redirects=True)
        remote_size = int(head.headers.get("content-length", 0))
    except Exception as e:
        log.warning(f"HEAD request failed for {url}: {e}. Proceeding without remote size verification.")
        remote_size = 0

    # If the target file already exists, check if its size matches the remote size to skip download
    if dest_path.exists():
        if remote_size and dest_path.stat().st_size == remote_size:
            log.info(f"File {dest_path.name} is already downloaded and complete. Skipping download.")
            return
        elif not remote_size:
            log.info(f"File {dest_path.name} already exists. Proceeding without remote size check.")
            return

    headers = {}
    initial_pos = 0
    if temp_path.exists():
        initial_pos = temp_path.stat().st_size
        if remote_size and initial_pos >= remote_size:
            if initial_pos == remote_size:
                log.info(f"Temporary file {temp_path.name} is already complete. Renaming to target.")
                if dest_path.exists():
                    dest_path.unlink()
                temp_path.rename(dest_path)
                return
            else:
                log.warning(f"Temporary file {temp_path.name} is larger than expected ({initial_pos} > {remote_size}). Re-downloading.")
                temp_path.unlink()
                initial_pos = 0
        else:
            headers["Range"] = f"bytes={initial_pos}-"
            log.info(f"Resuming download of {dest_path.name} from byte {initial_pos}...")
            
    mode = "ab" if initial_pos > 0 else "wb"
    
    try:
        # (connect, read) timeouts: the DPD allfiles.zip is large and www.canada.ca's
        # CDN can be slow to send the first bytes; 30s read was too aggressive. The
        # download resumes via Range headers, so a dropped connection is recoverable.
        r = session.get(url, headers=headers, stream=True, timeout=(15, 180))
        r.raise_for_status()
        
        # If server ignores Range header and returns 200 instead of 206
        if r.status_code == 200 and initial_pos > 0:
            log.warning("Server does not support range requests. Restarting download from scratch.")
            mode = "wb"
            initial_pos = 0
            
        total_size = remote_size or (int(r.headers.get("content-length", 0)) + initial_pos)
        
        with open(temp_path, mode) as f:
            if tqdm and total_size:
                with tqdm(total=total_size, initial=initial_pos, unit="B", unit_scale=True, desc=dest_path.name) as pbar:
                    for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                        if chunk:
                            f.write(chunk)
                            pbar.update(len(chunk))
            else:
                bytes_written = initial_pos
                last_log_time = time.time()
                for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)
                        bytes_written += len(chunk)
                        if time.time() - last_log_time > 5:
                            pct = (bytes_written / total_size * 100) if total_size else 0
                            log.info(f"Downloading {dest_path.name}: {bytes_written}/{total_size} bytes ({pct:.1f}%)")
                            last_log_time = time.time()
                            
        # Rename temporary file to target
        if dest_path.exists():
            dest_path.unlink()
        temp_path.rename(dest_path)
        log.info(f"Successfully downloaded {dest_path.name}.")
    except Exception as e:
        log.error(f"Error downloading {url}: {e}")
        raise e

# -- Safe ZIP Extractor ------------------------------------------------------
def extract_zip_to_directory(zip_path: Path, extract_dir: Path):
    """Safely extracts a ZIP archive into an isolated subdirectory."""
    log.info(f"Extracting {zip_path.name} to {extract_dir}...")
    extract_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)
        log.info(f"Successfully extracted {zip_path.name}.")
    except Exception as e:
        log.error(f"Failed to extract {zip_path.name}: {e}")
        raise e

# -- Database Compilation & Schema -------------------------------------------
def create_database_schema(db_path: Path):
    """Creates the SQLite database schema with indexes and WAL mode enabled."""
    log.info(f"Initializing database schema at {db_path}...")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Enable Write-Ahead Logging (WAL) for optimized write performance
    cursor.execute("PRAGMA journal_mode=WAL;")
    
    # 1. drug_product (QRYM_DRUG_PRODUCT)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS drug_product (
        drug_code INTEGER,
        web_referral_flag TEXT,
        product_categorization TEXT,
        drug_identification_number TEXT,
        brand_name TEXT,
        descriptor TEXT,
        pediatric_flag TEXT,
        access_dementia_flag TEXT,
        therapeutic_class_flag TEXT,
        last_update_date TEXT,
        expiry_date TEXT,
        product_categorization_fr TEXT,
        descriptor_fr TEXT,
        external_status_code TEXT,
        status_category TEXT,
        PRIMARY KEY (drug_code, status_category)
    );
    """)
    
    # 2. active_ingredients (QRYM_ACTIVE_INGREDIENTS)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS active_ingredients (
        drug_code INTEGER,
        active_ingredient_code INTEGER,
        ingredient_name TEXT,
        ingredient_flag TEXT,
        strength TEXT,
        strength_unit TEXT,
        strength_type TEXT,
        dosage_value TEXT,
        dosage_unit TEXT,
        rare_ingredient_flag TEXT,
        historic_freeform TEXT,
        ingredient_name_fr TEXT,
        strength_unit_fr TEXT,
        dosage_unit_fr TEXT,
        extra_flag TEXT
    );
    """)
    
    # 3. companies (QRYM_COMPANIES)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS companies (
        drug_code INTEGER,
        mfr_code TEXT,
        company_code TEXT,
        company_name TEXT,
        company_type TEXT,
        address_mailing_flag TEXT,
        address_billing_flag TEXT,
        address_notification_flag TEXT,
        address_other_flag TEXT,
        suite_number TEXT,
        street_name TEXT,
        city_name TEXT,
        province_name TEXT,
        country_name TEXT,
        postal_code TEXT,
        post_office_box TEXT,
        province_name_fr TEXT,
        country_name_fr TEXT
    );
    """)
    
    # 4. route (QRYM_ROUTE)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS route (
        drug_code INTEGER,
        route_of_administration_code INTEGER,
        route_of_administration_en TEXT,
        route_of_administration_fr TEXT
    );
    """)
    
    # 5. form (QRYM_FORM)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS form (
        drug_code INTEGER,
        pharmaceutical_form_code INTEGER,
        pharmaceutical_form_en TEXT,
        pharmaceutical_form_fr TEXT
    );
    """)
    
    # 6. status (QRYM_STATUS)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS status (
        drug_code INTEGER,
        current_status_flag TEXT,
        status_en TEXT,
        history_date TEXT,
        status_fr TEXT,
        extra_field_1 TEXT,
        extra_field_2 TEXT
    );
    """)
    
    # 7. therapeutic_class (QRYM_THERAPEUTIC_CLASS)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS therapeutic_class (
        drug_code INTEGER,
        tc_atc_number TEXT,
        tc_atc TEXT,
        tc_ahfs_number TEXT
    );
    """)
    
    # 8. schedule (QRYM_SCHEDULE)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS schedule (
        drug_code INTEGER,
        schedule_en TEXT,
        schedule_fr TEXT
    );
    """)
    
    # 9. package (QRYM_PACKAGING)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS package (
        drug_code INTEGER,
        upc TEXT,
        package_size_unit_en TEXT,
        package_size TEXT,
        product_information_en TEXT,
        package_type_en TEXT,
        package_size_unit_fr TEXT,
        product_information_fr TEXT
    );
    """)
    
    # 10. veterinary_species (QRYM_VETERINARY_SPECIES)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS veterinary_species (
        drug_code INTEGER,
        veterinary_species_en TEXT,
        veterinary_species_fr TEXT,
        extra_vet_field TEXT
    );
    """)

    # 11. pharm (QRYM_PHARMACEUTICAL_STD)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS pharm (
        drug_code INTEGER,
        pharmaceutical_std TEXT
    );
    """)

    # 12. biosimilar (QRYM_BIOSIMILAR)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS biosimilar (
        drug_code INTEGER,
        biosimilar_flag_en TEXT,
        biosimilar_flag_fr TEXT,
        biosimilar_type_code TEXT
    );
    """)
    
    # Resumable parsing state tracker
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS parsing_state (
        file_key TEXT PRIMARY KEY,
        status TEXT,
        last_modified TEXT,
        row_count INTEGER
    );
    """)
    
    # API Enrichment cache
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS api_enrichment (
        drug_code INTEGER PRIMARY KEY,
        api_status TEXT,
        last_updated TEXT,
        raw_response TEXT
    );
    """)
    
    # Build robust indexes on keys and frequently searched attributes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_dp_din ON drug_product(drug_identification_number);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_dp_brand ON drug_product(brand_name);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ai_dc ON active_ingredients(drug_code);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ai_name ON active_ingredients(ingredient_name);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_comp_dc ON companies(drug_code);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_route_dc ON route(drug_code);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_form_dc ON form(drug_code);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_status_dc ON status(drug_code);")
    
    conn.commit()
    conn.close()
    log.info("Database schema and indexes set up.")

def get_schema_key(filename: str) -> str:
    """Extracts the base schema key from a filename by stripping suffixes and extensions."""
    name = filename.lower().replace('.txt', '').replace('.csv', '')
    for suffix in ['_ap', '_ia', '_dr']:
        if name.endswith(suffix):
            name = name[:-len(suffix)]
    return name

# -- Resilient Text Parser and Loader ----------------------------------------
def parse_and_load_file(db_path: Path, file_path: Path, schema_key: str, status_category: str) -> int:
    """Parses a comma-separated DPD extract file and bulk loads it into SQLite inside a transaction."""
    if schema_key not in SCHEMAS:
        log.warning(f"No schema mapping found for key '{schema_key}' (file: {file_path.name}). Skipping.")
        return 0
        
    columns = SCHEMAS[schema_key]
    is_drug_product = (schema_key == "drug")
    
    # Map schema key to exact database table
    table_name = "drug_product" if is_drug_product else schema_key
    if table_name == "ingred":
        table_name = "active_ingredients"
    elif table_name == "comp":
        table_name = "companies"
    elif table_name == "ther":
        table_name = "therapeutic_class"
    elif table_name == "sched":
        table_name = "schedule"
    elif table_name == "vet":
        table_name = "veterinary_species"
        
    # Relational data extracts are UTF-8 or Latin-1. Resiliently resolve encoding.
    encoding = 'utf-8'
    for enc in ['utf-8', 'latin-1', 'cp1252']:
        try:
            with open(file_path, 'r', encoding=enc) as f:
                f.readline()
            encoding = enc
            break
        except UnicodeDecodeError:
            continue
            
    rows_to_insert = []
    
    with open(file_path, 'r', encoding=encoding, errors='replace') as f:
        reader = csv.reader(f, delimiter=',', quotechar='"')
        for row in reader:
            if not row:
                continue
            
            # Realign row length to match schema expectations
            if len(row) < len(columns):
                row = row + [None] * (len(columns) - len(row))
            elif len(row) > len(columns):
                row = row[:len(columns)]
                
            # Append unified status category for drug products
            if is_drug_product:
                row.append(status_category)
                
            rows_to_insert.append(row)
            
    if not rows_to_insert:
        return 0
        
    col_names_str = ", ".join(columns)
    if is_drug_product:
        col_names_str += ", status_category"
        
    placeholders = ", ".join(["?"] * len(rows_to_insert[0]))
    query = f"INSERT OR REPLACE INTO {table_name} ({col_names_str}) VALUES ({placeholders})"
    
    # Bulk insert within a single transaction block for speed and reliability
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN TRANSACTION;")
        cursor.executemany(query, rows_to_insert)
        conn.commit()
        log.info(f"Loaded {len(rows_to_insert)} rows into '{table_name}' from {file_path.name}.")
    except Exception as e:
        conn.rollback()
        log.error(f"Failed loading {file_path.name} into database: {e}")
        conn.close()
        raise e
    conn.close()
    return len(rows_to_insert)

def process_extracted_files(db_path: Path, raw_dir: Path):
    """Processes extracted files across status subdirectories and tracks completion state."""
    status_dirs = {
        "marketed": raw_dir / "marketed",
        "approved": raw_dir / "approved",
        "inactive": raw_dir / "inactive",
        "dormant": raw_dir / "dormant"
    }
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT file_key, last_modified FROM parsing_state WHERE status='completed'")
    completed_states = dict(cursor.fetchall())
    conn.close()
    
    for status, s_dir in status_dirs.items():
        if not s_dir.exists():
            log.warning(f"Isolated directory for status '{status}' does not exist. Skipping.")
            continue
            
        log.info(f"Processing database files for status: {status}...")
        for file_path in s_dir.glob("*.txt"):
            schema_key = get_schema_key(file_path.name)
            file_key = f"{status}:{file_path.name}"
            
            # Check modification time to reload file if it changes on disk
            mtime = str(file_path.stat().st_mtime)
            if file_key in completed_states and completed_states[file_key] == mtime:
                log.info(f"Skipping already ingested file: {file_key}")
                continue
                
            try:
                row_count = parse_and_load_file(db_path, file_path, schema_key, status)
                
                # Commit successful ingestion state
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT OR REPLACE INTO parsing_state (file_key, status, last_modified, row_count)
                    VALUES (?, 'completed', ?, ?)
                """, (file_key, mtime, row_count))
                conn.commit()
                conn.close()
            except Exception as e:
                log.error(f"Ingestion failed for {file_key}: {e}. Will retry on subsequent runs.")

# -- CSV-Only Compiler and Cleanup --------------------------------------------
def compile_csv_only(output_dir: Path, raw_dir: Path):
    """Compiles all extracted DPD text files into unified CSV files by type and cleans up everything else."""
    log.info("Compiling DPD data into unified CSV files...")
    
    # Ensure the output directory exists
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # We will map each schema type to its output CSV path
    for schema_key, columns in SCHEMAS.items():
        csv_path = output_dir / f"{schema_key}.csv"
        log.info(f"Generating unified CSV for type '{schema_key}' -> {csv_path.name}")
        
        # Write headers
        headers = columns + ["STATUS_CATEGORY"]
        
        try:
            with open(csv_path, 'w', encoding='utf-8', newline='') as csv_out:
                writer = csv.writer(csv_out)
                writer.writerow(headers)
                
                # Iterate through each status and read the corresponding text file
                for status in ["marketed", "approved", "inactive", "dormant"]:
                    status_dir = raw_dir / status
                    if not status_dir.exists():
                        continue
                    
                    suffix_map = {
                        "marketed": "",
                        "approved": "_ap",
                        "inactive": "_ia",
                        "dormant": "_dr"
                    }
                    suffix = suffix_map[status]
                    file_name = f"{schema_key}{suffix}.txt"
                    
                    file_path = status_dir / file_name
                    if not file_path.exists():
                        continue
                    
                    # Resiliently resolve encoding
                    encoding = 'utf-8'
                    for enc in ['utf-8', 'latin-1', 'cp1252']:
                        try:
                            with open(file_path, 'r', encoding=enc) as f:
                                f.readline()
                            encoding = enc
                            break
                        except UnicodeDecodeError:
                            continue
                            
                    with open(file_path, 'r', encoding=encoding, errors='replace') as f_in:
                        reader = csv.reader(f_in, delimiter=',', quotechar='"')
                        for row in reader:
                            if not row:
                                continue
                            
                            # Realign row length to match schema expectations
                            if len(row) < len(columns):
                                row = row + [None] * (len(columns) - len(row))
                            elif len(row) > len(columns):
                                row = row[:len(columns)]
                                
                            row.append(status)
                            writer.writerow(row)
                            
            log.info(f"Successfully created unified CSV: {csv_path.name}")
        except Exception as e:
            log.error(f"Failed to compile CSV for {schema_key}: {e}")
            raise e

    # Perform cleanup of other files/folders
    cleanup_non_csv_files(output_dir)

def cleanup_non_csv_files(output_dir: Path):
    """Deletes the downloads/ and raw/ directories, and any .db or .json files in the output directory."""
    log.info("Cleaning up temporary and non-CSV files...")
    
    # 1. Delete downloads/ directory
    downloads_dir = output_dir / "downloads"
    if downloads_dir.exists():
        try:
            for f in downloads_dir.glob("*"):
                f.unlink()
            downloads_dir.rmdir()
            log.info("Deleted downloads directory.")
        except Exception as e:
            log.warning(f"Failed to delete downloads directory: {e}")
            
    # 2. Delete raw/ directory
    raw_dir = output_dir / "raw"
    if raw_dir.exists():
        try:
            for status in ["marketed", "approved", "inactive", "dormant"]:
                status_dir = raw_dir / status
                if status_dir.exists():
                    for f in status_dir.glob("*"):
                        f.unlink()
                    status_dir.rmdir()
            raw_dir.rmdir()
            log.info("Deleted raw directory.")
        except Exception as e:
            log.warning(f"Failed to delete raw directory: {e}")
            
    # 3. Delete SQLite database if it exists
    db_path = output_dir / "canada_dpd.db"
    if db_path.exists():
        try:
            db_path.unlink()
            log.info("Deleted SQLite database file.")
        except Exception as e:
            log.warning(f"Failed to delete SQLite database: {e}")
            
    # Delete SQLite WAL and shm files if they exist
    for suffix in ["-wal", "-shm"]:
        db_extra = output_dir / f"canada_dpd.db{suffix}"
        if db_extra.exists():
            try:
                db_extra.unlink()
            except Exception:
                pass
            
    # 4. Delete JSON master dataset if it exists
    json_path = output_dir / "canada_dpd_master.json"
    if json_path.exists():
        try:
            json_path.unlink()
            log.info("Deleted master JSON file.")
        except Exception as e:
            log.warning(f"Failed to delete master JSON: {e}")

    # 5. Delete master CSV dataset if it exists
    master_csv_path = output_dir / "canada_dpd_master.csv"
    if master_csv_path.exists():
        try:
            master_csv_path.unlink()
            log.info("Deleted master CSV file.")
        except Exception as e:
            log.warning(f"Failed to delete master CSV: {e}")

# -- Consolidated Exporter ---------------------------------------------------
def export_master_datasets(db_path: Path, output_dir: Path):
    """Generates optimized consolidated Master CSV and Master JSON datasets."""
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "canada_dpd_master.csv"
    json_path = output_dir / "canada_dpd_master.json"
    
    log.info("Generating unified master exports...")
    
    # 1. Compile 1:1 Unified CSV via correlated SQL aggregations (highly optimized)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    csv_query = """
    SELECT 
        dp.drug_code,
        dp.drug_identification_number as din,
        dp.brand_name,
        dp.product_categorization,
        dp.status_category,
        (SELECT group_concat(ingredient_name || ' (' || strength || ' ' || strength_unit || ')', '; ') 
         FROM active_ingredients WHERE drug_code = dp.drug_code) as active_ingredients,
        (SELECT group_concat(company_name || ' (' || company_type || ')', '; ') 
         FROM companies WHERE drug_code = dp.drug_code) as companies,
        (SELECT group_concat(route_of_administration_en, '; ') 
         FROM route WHERE drug_code = dp.drug_code) as routes,
        (SELECT group_concat(pharmaceutical_form_en, '; ') 
         FROM form WHERE drug_code = dp.drug_code) as forms,
        (SELECT group_concat(schedule_en, '; ') 
         FROM schedule WHERE drug_code = dp.drug_code) as schedules,
        (SELECT group_concat(veterinary_species_en, '; ') 
         FROM veterinary_species WHERE drug_code = dp.drug_code) as veterinary_species,
        (SELECT group_concat(pharmaceutical_std, '; ') 
         FROM pharm WHERE drug_code = dp.drug_code) as pharmaceutical_standards
    FROM drug_product dp
    """
    
    cursor.execute(csv_query)
    
    with open(csv_path, 'w', encoding='utf-8', newline='') as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow([
            "drug_code", "din", "brand_name", "product_categorization", 
            "status_category", "active_ingredients", 
            "companies", "routes", "forms", "schedules", "veterinary_species", 
            "pharmaceutical_standards"
        ])
        
        count = 0
        while True:
            rows = cursor.fetchmany(5000)
            if not rows:
                break
            for row in rows:
                writer.writerow(list(row))
                count += 1
                
    log.info(f"Consolidated master CSV exported: {csv_path.name} ({count} products).")
    
    # 2. Compile Memory-Safe Streaming Nested JSON Export
    log.info("Compiling nested JSON master dataset...")
    cursor.execute("SELECT * FROM drug_product")
    
    with open(json_path, 'w', encoding='utf-8') as json_file:
        json_file.write("[\n")
        
        first = True
        count = 0
        
        while True:
            dp_rows = cursor.fetchmany(1000)
            if not dp_rows:
                break
                
            drug_codes = [r['drug_code'] for r in dp_rows]
            placeholders = ",".join(["?"] * len(drug_codes))
            
            # Batch fetch nested items for sub-objects
            sub_conn = sqlite3.connect(db_path)
            sub_conn.row_factory = sqlite3.Row
            sub_cursor = sub_conn.cursor()
            
            # Ingredients
            sub_cursor.execute(f"SELECT * FROM active_ingredients WHERE drug_code IN ({placeholders})", drug_codes)
            ingredients_by_code = {}
            for r in sub_cursor.fetchall():
                ingredients_by_code.setdefault(r['drug_code'], []).append({
                    "ingredient_name_en": r['ingredient_name'],
                    "ingredient_name_fr": r['ingredient_name_fr'],
                    "ingredient_flag": r['ingredient_flag'],
                    "strength": r['strength'],
                    "strength_unit_en": r['strength_unit'],
                    "strength_unit_fr": r['strength_unit_fr'],
                    "dosage_value": r['dosage_value'],
                    "dosage_unit_en": r['dosage_unit'],
                    "dosage_unit_fr": r['dosage_unit_fr']
                })
                
            # Companies
            sub_cursor.execute(f"SELECT * FROM companies WHERE drug_code IN ({placeholders})", drug_codes)
            companies_by_code = {}
            for r in sub_cursor.fetchall():
                companies_by_code.setdefault(r['drug_code'], []).append({
                    "company_name": r['company_name'],
                    "company_type": r['company_type'],
                    "city": r['city_name'],
                    "province": r['province_name'],
                    "country": r['country_name'],
                    "postal_code": r['postal_code']
                })
                
            # Routes
            sub_cursor.execute(f"SELECT * FROM route WHERE drug_code IN ({placeholders})", drug_codes)
            routes_by_code = {}
            for r in sub_cursor.fetchall():
                routes_by_code.setdefault(r['drug_code'], []).append({
                    "route_en": r['route_of_administration_en'],
                    "route_fr": r['route_of_administration_fr']
                })
                
            # Forms
            sub_cursor.execute(f"SELECT * FROM form WHERE drug_code IN ({placeholders})", drug_codes)
            forms_by_code = {}
            for r in sub_cursor.fetchall():
                forms_by_code.setdefault(r['drug_code'], []).append({
                    "form_en": r['pharmaceutical_form_en'],
                    "form_fr": r['pharmaceutical_form_fr']
                })
                
            # Packaging
            sub_cursor.execute(f"SELECT * FROM package WHERE drug_code IN ({placeholders})", drug_codes)
            packaging_by_code = {}
            for r in sub_cursor.fetchall():
                packaging_by_code.setdefault(r['drug_code'], []).append({
                    "upc": r['upc'],
                    "package_size": r['package_size'],
                    "package_size_unit_en": r['package_size_unit_en'],
                    "package_size_unit_fr": r['package_size_unit_fr'],
                    "product_information_en": r['product_information_en'],
                    "product_information_fr": r['product_information_fr']
                })
                
            sub_conn.close()
            
            for dp in dp_rows:
                dc = dp['drug_code']
                record = {
                    "drug_code": dc,
                    "din": dp['drug_identification_number'],
                    "brand_name": dp['brand_name'],
                    "product_categorization_en": dp['product_categorization'],
                    "product_categorization_fr": dp['product_categorization_fr'],
                    "status_category": dp['status_category'],
                    "last_update_date": dp['last_update_date'],
                    "active_ingredients": ingredients_by_code.get(dc, []),
                    "companies": companies_by_code.get(dc, []),
                    "routes": routes_by_code.get(dc, []),
                    "forms": forms_by_code.get(dc, []),
                    "packaging": packaging_by_code.get(dc, [])
                }
                
                if not first:
                    json_file.write(",\n")
                first = False
                
                json.dump(record, json_file, indent=2)
                count += 1
                
    conn.close()
    log.info(f"Nested JSON master dataset exported: {json_path.name} ({count} products).")
    
    # Clean up intermediate master JSON file to retain only CSV output
    if json_path.exists():
        try:
            json_path.unlink()
            log.info("Cleaned up intermediate master JSON file.")
        except Exception as e:
            log.warning(f"Could not remove {json_path}: {e}")

# -- Concurrent, Rate-Limit-Aware API Crawler -------------------------------
def fetch_single_api_record(session: requests.Session, drug_code: int, db_path: Path, db_lock: threading.Lock) -> str:
    """Queries the REST API for a single product and writes it to cache, handling rate limits."""
    url = f"{API_BASE_URL}drugproduct/?id={drug_code}&lang=en&type=json"
    
    backoff = 1.0
    max_backoff = 30.0
    
    for attempt in range(5):
        try:
            r = session.get(url, timeout=15)
            
            # Respect rate limiting gracefully
            if r.status_code == 429:
                log.warning(f"Rate limit (429) hit on drug code {drug_code}. Backing off {backoff:.1f}s...")
                time.sleep(backoff)
                backoff = min(backoff * 2.0, max_backoff)
                continue
            elif r.status_code >= 500:
                log.warning(f"Server error ({r.status_code}) hit on drug code {drug_code}. Backing off {backoff:.1f}s...")
                time.sleep(backoff)
                backoff = min(backoff * 2.0, max_backoff)
                continue
                
            r.raise_for_status()
            
            # Parse and serialize response
            data = r.json()
            with db_lock:
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT OR REPLACE INTO api_enrichment (drug_code, api_status, last_updated, raw_response)
                    VALUES (?, 'success', datetime('now'), ?)
                """, (drug_code, json.dumps(data)))
                conn.commit()
                conn.close()
            return "success"
            
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                # 404 indicates record does not exist on API. Prevent redundant querying.
                with db_lock:
                    conn = sqlite3.connect(db_path)
                    cursor = conn.cursor()
                    cursor.execute("""
                        INSERT OR REPLACE INTO api_enrichment (drug_code, api_status, last_updated, raw_response)
                        VALUES (?, 'not_found', datetime('now'), NULL)
                    """, (drug_code,))
                    conn.commit()
                    conn.close()
                return "not_found"
            log.warning(f"HTTP Error for {drug_code} (attempt {attempt+1}): {e}")
        except Exception as e:
            log.warning(f"Error fetching {drug_code} (attempt {attempt+1}): {e}")
            
        time.sleep(backoff)
        backoff = min(backoff * 2.0, max_backoff)
        
    # Log failure state on exhausted attempts
    with db_lock:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO api_enrichment (drug_code, api_status, last_updated, raw_response)
            VALUES (?, 'failed', datetime('now'), NULL)
        """, (drug_code,))
        conn.commit()
        conn.close()
    return "failed"

def run_api_enrichment(db_path: Path, num_threads: int, limit: int = None):
    """Orchestrates multi-threaded crawling of DPD REST API for missing records."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Statefully select only records that have not been successfully crawled/resolved
    query = """
    SELECT drug_code FROM drug_product 
    WHERE drug_code NOT IN (SELECT drug_code FROM api_enrichment WHERE api_status IN ('success', 'not_found'))
    """
    if limit:
        query += f" LIMIT {limit}"
        
    cursor.execute(query)
    drug_codes = [r[0] for r in cursor.fetchall()]
    conn.close()
    
    if not drug_codes:
        log.info("All local products are already enriched from the REST API. Skipping crawler.")
        return
        
    log.info(f"Enriching {len(drug_codes)} products from REST API using {num_threads} threads...")
    
    session = build_session()
    db_lock = threading.Lock()
    
    success_count = 0
    not_found_count = 0
    failed_count = 0
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads, thread_name_prefix="APICrawler") as executor:
        futures = {executor.submit(fetch_single_api_record, session, dc, db_path, db_lock): dc for dc in drug_codes}
        
        if tqdm:
            with tqdm(total=len(futures), desc="API Enrichment") as pbar:
                for future in concurrent.futures.as_completed(futures):
                    status = future.result()
                    if status == "success":
                        success_count += 1
                    elif status == "not_found":
                        not_found_count += 1
                    else:
                        failed_count += 1
                    pbar.update(1)
        else:
            for i, future in enumerate(concurrent.futures.as_completed(futures)):
                status = future.result()
                if status == "success":
                    success_count += 1
                elif status == "not_found":
                    not_found_count += 1
                else:
                    failed_count += 1
                if (i + 1) % 100 == 0:
                    log.info(f"API Crawler: {i+1}/{len(futures)} processed ({success_count} success, {not_found_count} not found, {failed_count} failed).")
                    
    log.info(f"REST API enrichment complete. Success: {success_count}, Not Found: {not_found_count}, Failed: {failed_count}.")

# -- Database Search Interface -----------------------------------------------
def search_local_database(db_path: Path, search_query: str):
    """Searches local database by brand name, DIN, or active ingredient."""
    log.info(f"Searching database for query: '{search_query}'...")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    query = """
    SELECT DISTINCT
        dp.drug_code,
        dp.drug_identification_number as din,
        dp.brand_name,
        dp.product_categorization,
        dp.status_category,
        (SELECT group_concat(ingredient_name || ' (' || strength || ' ' || strength_unit || ')', '; ') 
         FROM active_ingredients WHERE drug_code = dp.drug_code) as active_ingredients
    FROM drug_product dp
    LEFT JOIN active_ingredients ai ON dp.drug_code = ai.drug_code
    WHERE 
        dp.brand_name LIKE ? OR 
        dp.drug_identification_number LIKE ? OR 
        ai.ingredient_name LIKE ?
    LIMIT 20
    """
    like_param = f"%{search_query}%"
    cursor.execute(query, (like_param, like_param, like_param))
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        log.info("No matching drug products found.")
        return
        
    log.info(f"Found {len(rows)} matching products (showing top 20 matches):")
    print("-" * 110)
    print(f"{'DIN':<10} | {'Brand Name':<25} | {'Category':<12} | {'Status':<10} | {'Ingredients'}")
    print("-" * 110)
    for row in rows:
        ingredients = row['active_ingredients'] or "N/A"
        if len(ingredients) > 45:
            ingredients = ingredients[:42] + "..."
        print(f"{row['din']:<10} | {row['brand_name']:<25} | {row['product_categorization']:<12} | {row['status_category']:<10} | {ingredients}")
    print("-" * 110)

# -- CLI / Main Entrypoint ---------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Resilient and multi-threaded data collector for the Health Canada Drug Product Database (DPD)."
    )
    subparsers = parser.add_subparsers(dest="command", required=True, help="Collector commands")
    
    # 1. download-bulk command
    parser_bulk = subparsers.add_parser("download-bulk", help="Download bulk ZIP extracts, compile SQLite database, and export master datasets.")
    parser_bulk.add_argument(
        "--output-dir", 
        type=str, 
        default=DEFAULT_OUTPUT_DIR, 
        help="Target directory for downloads and database compilation."
    )
    parser_bulk.add_argument(
        "--csv-only",
        action="store_true",
        help="Compile all DPD text files directly into unified CSV files by type, and delete all other raw/temporary files."
    )
    
    # 2. api-enrich command
    parser_api = subparsers.add_parser("api-enrich", help="Concurrently enrichment local drug records via DPD REST API.")
    parser_api.add_argument(
        "--output-dir", 
        type=str, 
        default=DEFAULT_OUTPUT_DIR, 
        help="Directory containing the compiled SQLite database."
    )
    parser_api.add_argument(
        "--threads", 
        type=int, 
        default=DEFAULT_THREADS, 
        help="Number of concurrent execution threads."
    )
    parser_api.add_argument(
        "--limit", 
        type=int, 
        default=None, 
        help="Maximum number of records to enrich in this execution."
    )
    
    # 3. search command
    parser_search = subparsers.add_parser("search", help="Query compiled local database.")
    parser_search.add_argument(
        "--output-dir", 
        type=str, 
        default=DEFAULT_OUTPUT_DIR, 
        help="Directory containing the compiled SQLite database."
    )
    parser_search.add_argument(
        "--query", 
        type=str, 
        required=True, 
        help="Search query (matches brand name, DIN, or ingredient)."
    )
    
    args = parser.parse_args()
    
    output_dir = Path(args.output_dir)
    db_path = output_dir / "canada_dpd.db"
    
    setup_logging(output_dir)
    
    if args.command == "download-bulk":
        log.info("=== Mode: download-bulk ===")
        raw_dir = output_dir / "raw"
        downloads_dir = output_dir / "downloads"
        
        downloads_dir.mkdir(parents=True, exist_ok=True)
        raw_dir.mkdir(parents=True, exist_ok=True)
        
        session = build_session()
        
        # Download ZIP extracts
        for status, filename in BULK_FILES.items():
            url = f"{DPD_BASE_URL}{filename}"
            zip_path = downloads_dir / filename
            log.info(f"Resolving download for {status} DPD extract...")
            try:
                download_file_with_resume(session, url, zip_path)
            except Exception as e:
                log.critical(f"Critical error downloading {filename}: {e}. Aborting bulk compilation.")
                sys.exit(1)
                
            # Extract ZIP into isolated directory to prevent overwriting identical filenames
            status_extract_dir = raw_dir / status
            try:
                extract_zip_to_directory(zip_path, status_extract_dir)
            except Exception as e:
                log.critical(f"Critical error extracting {filename}: {e}. Aborting bulk compilation.")
                sys.exit(1)
                
        if args.csv_only:
            compile_csv_only(output_dir, raw_dir)
            log.info("Bulk download and CSV compilation complete.")
        else:
            # Compile local SQLite database
            create_database_schema(db_path)
            process_extracted_files(db_path, raw_dir)
            
            # Export unified master datasets
            export_master_datasets(db_path, output_dir)
            log.info("Bulk download and compilation complete.")
        
    elif args.command == "api-enrich":
        log.info("=== Mode: api-enrich ===")
        if not db_path.exists():
            log.critical(f"Compiled database not found at {db_path}. Run 'download-bulk' first.")
            sys.exit(1)
            
        run_api_enrichment(db_path, args.threads, args.limit)
        
    elif args.command == "search":
        if not db_path.exists():
            log.critical(f"Compiled database not found at {db_path}. Run 'download-bulk' first.")
            sys.exit(1)
            
        search_local_database(db_path, args.query)

if __name__ == "__main__":
    main()
