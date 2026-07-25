#!/usr/bin/env python3
"""
dailymed_downloader.py
======================
Authoritative data collector for DailyMed Structured Product Labeling (SPL).
Provides features to download, compile, and query DailyMed drug and substance data.

Modes of Operation:
  1. download-mappings: Downloads NLM bulk mapping ZIP files, extracts them,
                        compiles a unified SQLite database with indexing,
                        and exports a joined master CSV of drug-to-substance mappings.
  2. search: Query the local compiled SQLite database or fall back to the DailyMed
             REST API to search for drugs by name, generic ingredient, NDC, or Set ID.
  3. api-fetch-spls: Harvests the entire active SPL catalog paginated from the
                     DailyMed REST API, with resumability and progress tracking.
  4. api-fetch-details: Concurrently enriches a list of SPL Set IDs with NDCs,
                        package descriptions, and media URLs using a thread pool.

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
import io
from pathlib import Path
import concurrent.futures
from urllib.parse import urljoin
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

# Optional progress indicator
try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

# -- Configuration & Constants -----------------------------------------------
API_BASE_URL = "https://dailymed.nlm.nih.gov/dailymed/services/v2"
MAPPING_BASE_URL = "https://dailymed-data.nlm.nih.gov/public-release-files/"

# Resolve output paths relative to this script, never the current working dir.
BASE_DIR = Path(__file__).resolve().parent

DEFAULT_DELAY = 0.15           # Polite delay between API calls (seconds)
REQUEST_TIMEOUT = 30           # Request timeout (seconds)
MAX_RETRIES = 5
DEFAULT_THREADS = 4

MAPPING_FILES = {
    "metadata": "dm_spl_zip_files_meta_data.zip",
    "rxnorm": "rxnorm_mappings.zip",
    "pharma": "pharmacologic_class_mappings.zip"
}

CSV_MASTER_FIELDS = [
    "setid",
    "title",
    "spl_version",
    "rxcui",
    "rxstring",
    "rxtty"
]

# -- Logging Setup -----------------------------------------------------------
log = logging.getLogger("dailymed_downloader")

def setup_logging(output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / "dailymed_downloader.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout)
        ]
    )

# -- HTTP Session Configuration ----------------------------------------------
def build_session() -> requests.Session:
    """Builds a requests.Session with retries, back-off, and standard headers."""
    session = requests.Session()
    retry_strategy = Retry(
        total=MAX_RETRIES,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "Accept": "application/json",
        "User-Agent": "BiolytInternDailyMedDownloader/1.0 (Python; requests)"
    })
    return session

# -- Progress Indicator Helper ------------------------------------------------
class SimpleProgressBar:
    """Custom progress bar fallback if tqdm is not installed."""
    def __init__(self, total: int, desc: str = ""):
        self.total = total
        self.desc = desc
        self.current = 0
        self.start_time = time.time()
        
    def update(self, amount: int = 1):
        self.current += amount
        percent = (self.current / self.total) * 100 if self.total > 0 else 0
        elapsed = time.time() - self.start_time
        speed = self.current / elapsed if elapsed > 0 else 0
        eta = (self.total - self.current) / speed if speed > 0 else 0
        
        sys.stdout.write(
            f"\r{self.desc}: {self.current}/{self.total} ({percent:.1f}%) | "
            f"{speed:.1f} items/s | Elapsed: {elapsed:.1f}s | ETA: {eta:.1f}s"
        )
        sys.stdout.flush()
        if self.current >= self.total:
            sys.stdout.write("\n")
            sys.stdout.flush()

# -- Subcommand 1: Download & Compile Mappings ------------------------------
def handle_download_mappings(args, session: requests.Session, output_dir: Path):
    """Downloads bulk mapping ZIP files and compiles them into a unified SQLite database."""
    log.info("Starting DailyMed bulk mappings downloader & compiler...")
    
    downloads_dir = output_dir / "downloads"
    downloads_dir.mkdir(exist_ok=True)
    db_file = output_dir / "dailymed.db"
    csv_file = output_dir / "dailymed_master_mapping.csv"
    
    # 1. Download ZIP files
    for key, filename in MAPPING_FILES.items():
        url = urljoin(MAPPING_BASE_URL, filename)
        dest_path = downloads_dir / filename
        
        # Check if file exists and is a valid ZIP archive
        if dest_path.exists() and zipfile.is_zipfile(dest_path):
            log.info(f"File {filename} already exists and is valid. Skipping download.")
            continue
            
        log.info(f"Downloading {filename} from {url}...")
        try:
            # Stream the download to file
            with session.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                total_size = int(r.headers.get("content-length", 0))
                
                # Setup progress bar
                if tqdm:
                    pbar = tqdm(total=total_size, unit='B', unit_scale=True, desc=filename)
                else:
                    pbar = None
                    log.info(f"Downloading {filename} ({total_size / (1024*1024):.2f} MB)...")
                
                with open(dest_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            if pbar:
                                pbar.update(len(chunk))
                if pbar:
                    pbar.close()
            log.info(f"  -> Saved to {dest_path}")
        except Exception as e:
            log.error(f"Failed to download {filename}: {e}")
            sys.exit(1)

    # 2. Extract and compile into SQLite
    log.info(f"Compiling data into SQLite database: {db_file}...")
    try:
        conn = sqlite3.connect(db_file)
        cur = conn.cursor()
        
        # Initialize Tables
        log.info("Initializing SQLite tables...")
        cur.execute("DROP TABLE IF EXISTS spl_metadata;")
        cur.execute("""
            CREATE TABLE spl_metadata (
                setid TEXT PRIMARY KEY,
                zip_file_name TEXT,
                upload_date TEXT,
                spl_version INTEGER,
                title TEXT
            );
        """)
        
        cur.execute("DROP TABLE IF EXISTS rxnorm_mappings;")
        cur.execute("""
            CREATE TABLE rxnorm_mappings (
                setid TEXT,
                spl_version INTEGER,
                rxcui TEXT,
                rxstring TEXT,
                rxtty TEXT
            );
        """)
        
        cur.execute("DROP TABLE IF EXISTS pharma_mappings;")
        cur.execute("""
            CREATE TABLE pharma_mappings (
                spl_setid TEXT,
                spl_version INTEGER,
                pharma_setid TEXT,
                pharma_version INTEGER
            );
        """)
        conn.commit()
        
        # Helper to extract and insert pipe-delimited data
        def insert_from_zip(zip_path: Path, table_name: str, delimiter: str = "|"):
            log.info(f"Extracting and loading {zip_path.name} into '{table_name}'...")
            with zipfile.ZipFile(zip_path) as z:
                # Find the main text file
                txt_files = [x for x in z.namelist() if x.endswith('.txt')]
                if not txt_files:
                    log.warning(f"No text file found in {zip_path.name}")
                    return
                
                txt_file = txt_files[0]
                with z.open(txt_file) as f:
                    content = f.read().decode('utf-8', errors='ignore')
                    lines = content.splitlines()
                    
                    if not lines:
                        return
                    
                    header = [h.strip().upper() for h in lines[0].split(delimiter)]
                    placeholders = ", ".join(["?"] * len(header))
                    insert_sql = f"INSERT INTO {table_name} ({', '.join(header)}) VALUES ({placeholders});"
                    
                    # Prepare batch insert
                    batch = []
                    count = 0
                    
                    for line in lines[1:]:
                        if not line.strip():
                            continue
                        parts = [p.strip() for p in line.split(delimiter)]
                        # Handle potential length mismatches
                        if len(parts) == len(header):
                            batch.append(parts)
                        else:
                            # Pad or slice to match headers
                            parts = (parts + [""] * len(header))[:len(header)]
                            batch.append(parts)
                            
                        if len(batch) >= 10000:
                            cur.executemany(insert_sql, batch)
                            count += len(batch)
                            batch = []
                            
                    if batch:
                        cur.executemany(insert_sql, batch)
                        count += len(batch)
                        
                    log.info(f"  -> Successfully loaded {count:,} records into '{table_name}'.")
                    
        insert_from_zip(downloads_dir / MAPPING_FILES["metadata"], "spl_metadata")
        insert_from_zip(downloads_dir / MAPPING_FILES["rxnorm"], "rxnorm_mappings")
        insert_from_zip(downloads_dir / MAPPING_FILES["pharma"], "pharma_mappings")
        
        # 3. Create Indexes for performance
        log.info("Creating indexes for relational performance...")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_rxnorm_setid ON rxnorm_mappings(setid);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_rxnorm_rxcui ON rxnorm_mappings(rxcui);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_rxnorm_rxstring ON rxnorm_mappings(rxstring);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pharma_setid ON pharma_mappings(spl_setid);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_metadata_title ON spl_metadata(title);")
        conn.commit()
        
        # 4. Export Joined Master CSV (SPL + RxNorm mapping)
        log.info(f"Exporting master joined dataset to CSV: {csv_file}...")
        cur.execute("""
            SELECT m.setid, m.title, m.spl_version, r.rxcui, r.rxstring, r.rxtty
            FROM spl_metadata m
            LEFT JOIN rxnorm_mappings r ON m.setid = r.setid
            ORDER BY m.title ASC;
        """)
        
        # Write CSV in chunks
        with open(csv_file, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(CSV_MASTER_FIELDS)
            
            row_count = 0
            while True:
                rows = cur.fetchmany(10000)
                if not rows:
                    break
                writer.writerows(rows)
                row_count += len(rows)
                
        log.info(f"  -> Exported {row_count:,} mapping rows to {csv_file}")
        
        # 5. Export Joined Pharma CSV (SPL + Pharmacologic Class mapping)
        pharma_csv_file = output_dir / "dailymed_pharma_mapping.csv"
        log.info(f"Exporting pharmacological class mappings to CSV: {pharma_csv_file}...")
        cur.execute("""
            SELECT p.spl_setid, m.title, p.spl_version, p.pharma_setid, p.pharma_version
            FROM pharma_mappings p
            LEFT JOIN spl_metadata m ON p.spl_setid = m.setid
            ORDER BY m.title ASC;
        """)
        
        PHARMA_CSV_FIELDS = ["spl_setid", "title", "spl_version", "pharma_setid", "pharma_version"]
        
        with open(pharma_csv_file, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(PHARMA_CSV_FIELDS)
            
            pharma_row_count = 0
            while True:
                rows = cur.fetchmany(10000)
                if not rows:
                    break
                writer.writerows(rows)
                pharma_row_count += len(rows)
                
        log.info(f"  -> Exported {pharma_row_count:,} pharmacological class mapping rows to {pharma_csv_file}")
        
        # Database Stats
        cur.execute("SELECT COUNT(*) FROM spl_metadata;")
        total_spls = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT rxcui) FROM rxnorm_mappings;")
        total_rxcuis = cur.fetchone()[0]
        log.info("Compilation complete!")
        log.info(f"  Total Active Drug Labels (SPLs): {total_spls:,}")
        log.info(f"  Total Unique Substance Concepts (RxCUI): {total_rxcuis:,}")
        
        conn.close()
        
        # 6. Delete database and downloads folder if requested
        if getattr(args, "delete_db", False):
            log.info(f"Removing SQLite database as requested by --delete-db: {db_file}")
            try:
                if db_file.exists():
                    db_file.unlink()
                    log.info("Database deleted successfully.")
            except Exception as e:
                log.error(f"Failed to delete database: {e}")
                
            downloads_dir = output_dir / "downloads"
            log.info(f"Removing downloads directory as requested by --delete-db: {downloads_dir}")
            try:
                if downloads_dir.exists():
                    import shutil
                    shutil.rmtree(downloads_dir)
                    log.info("Downloads directory deleted successfully.")
            except Exception as e:
                log.error(f"Failed to delete downloads directory: {e}")
                
    except Exception as e:
        log.error(f"SQLite compilation error: {e}")
        sys.exit(1)

# -- Subcommand 2: Search (Local or API) -------------------------------------
def handle_search(args, output_dir: Path):
    """Searches the drug index, using local SQLite if available, otherwise falling back to API."""
    query = args.query.strip()
    db_file = output_dir / "dailymed.db"
    
    if db_file.exists():
        # Perform Local SQLite Search
        log.info(f"Performing local database search for: '{query}'...")
        try:
            conn = sqlite3.connect(db_file)
            cur = conn.cursor()
            
            # Search by name, ingredient, or RxCUI
            search_pattern = f"%{query}%"
            cur.execute("""
                SELECT DISTINCT m.setid, m.title, m.spl_version, r.rxcui, r.rxstring, r.rxtty
                FROM spl_metadata m
                LEFT JOIN rxnorm_mappings r ON m.setid = r.setid
                WHERE m.title LIKE ? OR r.rxstring LIKE ? OR r.rxcui = ?
                LIMIT 50;
            """, (search_pattern, search_pattern, query))
            
            results = cur.fetchall()
            conn.close()
            
            if results:
                log.info(f"Found {len(results)} matches (showing top 50):")
                print("\n" + "="*80)
                print(f"{'SETID':<38} | {'DRUG LABEL (TITLE) / RXNORM CONCEPT':<40}")
                print("="*80)
                for r in results:
                    setid, title, version, rxcui, rxstring, rxtty = r
                    # Print label info
                    print(f"{setid:<38} | {title[:80]}")
                    if rxcui:
                        print(f"{'':<38} |   -> RxNorm: {rxstring} (CUI: {rxcui}, Type: {rxtty})")
                print("="*80 + "\n")
            else:
                log.info("No matching drug labels found in the local database.")
        except Exception as e:
            log.error(f"Local search failed: {e}")
    else:
        # Fallback to Online REST API Search
        log.info(f"Local database not found at {db_file}. Falling back to online REST API...")
        url = f"{API_BASE_URL}/spls.json"
        params = {"drug_name": query, "page": 1, "pagesize": 20}
        
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                spls = data.get("data", [])
                meta = data.get("metadata", {})
                total = meta.get("total_elements", 0)
                
                log.info(f"Online search returned {len(spls)} items of {total:,} total elements:")
                print("\n" + "="*80)
                print(f"{'SETID':<38} | {'DRUG LABEL TITLE':<40}")
                print("="*80)
                for item in spls:
                    print(f"{item.get('setid'):<38} | {item.get('title')[:80]}")
                print("="*80 + "\n")
            else:
                log.error(f"API search failed (HTTP {resp.status_code}): {resp.text}")
        except Exception as e:
            log.error(f"Online search request failed: {e}")

# -- Subcommand 3: API SPL Catalog Harvesting (Paginated) --------------------
def handle_api_fetch_spls(args, session: requests.Session, output_dir: Path):
    """Harvests active SPL catalog from the DailyMed REST API with resumability."""
    log.info("Initializing DailyMed online SPL catalog harvester...")
    
    catalog_file = output_dir / "dailymed_catalog.jsonl"
    progress_file = output_dir / "dailymed_progress.json"
    
    # Load progress state if exists
    start_page = 1
    total_elements = 0
    pagesize = min(args.pagesize, 100) # Max page size is 100
    
    if progress_file.exists():
        try:
            with open(progress_file, "r") as f:
                state = json.load(f)
                start_page = state.get("next_page", 1)
                total_elements = state.get("total_elements", 0)
                log.info(f"Resuming from previous run: page={start_page}, total_elements={total_elements:,}")
        except Exception as e:
            log.warning(f"Could not load progress file: {e}. Starting from page 1.")
            
    url = f"{API_BASE_URL}/spls.json"
    current_page = start_page
    
    # 1. Fetch first page to find total count if starting fresh
    if current_page == 1 or total_elements == 0:
        log.info("Querying initial page for catalog metadata...")
        try:
            resp = session.get(url, params={"page": 1, "pagesize": pagesize}, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            total_elements = data.get("metadata", {}).get("total_elements", 0)
            log.info(f"Total active drug labels (SPLs) in DailyMed: {total_elements:,}")
        except Exception as e:
            log.error(f"Failed to query initial page: {e}")
            sys.exit(1)
            
    total_pages = (total_elements + pagesize - 1) // pagesize
    log.info(f"Harvesting {total_elements:,} labels over {total_pages:,} pages (pagesize={pagesize})...")
    
    # Determine page range
    end_page = total_pages
    if args.max_pages:
        end_page = min(current_page + args.max_pages - 1, total_pages)
        log.info(f"Limiting run to {args.max_pages} pages (target end page: {end_page}).")
        
    # Setup Progress Bar
    if tqdm:
        pbar = tqdm(total=end_page - current_page + 1, desc="Catalog Pages")
    else:
        pbar = SimpleProgressBar(total=end_page - current_page + 1, desc="Catalog Pages")
        
    mode = "a" if catalog_file.exists() else "w"
    
    try:
        with open(catalog_file, mode, encoding="utf-8") as fh:
            while current_page <= end_page:
                log.debug(f"Fetching catalog page {current_page}...")
                params = {"page": current_page, "pagesize": pagesize}
                
                resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
                if resp.status_code != 200:
                    log.warning(f"Error fetching page {current_page}: HTTP {resp.status_code}. Retrying after delay...")
                    time.sleep(2)
                    continue
                    
                data = resp.json()
                spls = data.get("data", [])
                
                if not spls:
                    log.info("No more records returned. Harvesting complete.")
                    break
                    
                # Write to JSONL
                for spl in spls:
                    fh.write(json.dumps(spl) + "\n")
                    
                # Save progress
                current_page += 1
                with open(progress_file, "w") as pf:
                    json.dump({
                        "next_page": current_page,
                        "total_elements": total_elements,
                        "timestamp": time.time()
                    }, pf)
                    
                pbar.update(1)
                time.sleep(DEFAULT_DELAY) # Polite throttling
                
    except KeyboardInterrupt:
        log.warning("\nHarvesting interrupted by user. Progress saved.")
    finally:
        if isinstance(pbar, tqdm):
            pbar.close()
            
    log.info(f"Catalog harvesting page run completed. Outputs saved to {catalog_file}")

# -- Subcommand 4: Parallel API Detail Enrichment ---------------------------
def fetch_spl_details(session: requests.Session, setid: str) -> dict:
    """Helper to fetch NDC and packaging details for a single SETID from the API."""
    ndcs_url = f"{API_BASE_URL}/spls/{setid}/ndcs.json"
    pkg_url = f"{API_BASE_URL}/spls/{setid}/packaging.json"
    media_url = f"{API_BASE_URL}/spls/{setid}/media.json"
    
    result = {
        "setid": setid,
        "ndcs": [],
        "packages": [],
        "media_urls": [],
        "error": None
    }
    
    # 1. Fetch NDCs
    try:
        time.sleep(DEFAULT_DELAY)
        resp = session.get(ndcs_url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            # Extract NDCs
            ndcs_list = data.get("data", {}).get("ndcs", [])
            result["ndcs"] = [item.get("ndc") for item in ndcs_list if item.get("ndc")]
        elif resp.status_code != 404:
            result["error"] = f"NDCs HTTP {resp.status_code}"
    except Exception as e:
        result["error"] = f"NDCs exception: {e}"
        
    # 2. Fetch Packaging
    try:
        time.sleep(DEFAULT_DELAY)
        resp = session.get(pkg_url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            products = data.get("data", {}).get("products", [])
            pkgs = []
            for prod in products:
                for pkg in prod.get("packaging", []):
                    desc = ", ".join(pkg.get("package_descriptions", []))
                    ndc = pkg.get("ndc", "")
                    pkgs.append(f"{ndc}:[{desc}]" if ndc else f"[{desc}]")
            result["packages"] = pkgs
        elif resp.status_code != 404:
            result["error"] = (result["error"] + " | " if result["error"] else "") + f"Pkg HTTP {resp.status_code}"
    except Exception as e:
        result["error"] = (result["error"] + " | " if result["error"] else "") + f"Pkg exception: {e}"

    # 3. Fetch Media
    try:
        time.sleep(DEFAULT_DELAY)
        resp = session.get(media_url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            media = data.get("data", {}).get("media", [])
            result["media_urls"] = [m.get("url") for m in media if m.get("url")]
    except Exception:
        # Suppress media errors to avoid noise
        pass
        
    return result

def handle_api_fetch_details(args, session: requests.Session, output_dir: Path):
    """Downloads NDC, packaging, and media details for a list of Set IDs in parallel."""
    log.info("Starting concurrent DailyMed details enricher...")
    
    details_csv = output_dir / "dailymed_details.csv"
    details_jsonl = output_dir / "dailymed_details.jsonl"
    
    # 1. Gather SETIDs to process
    setids = []
    
    # Option A: Read from list file if provided
    if args.setid_file:
        setid_path = Path(args.setid_file)
        if setid_path.exists():
            with open(setid_path, "r") as fh:
                setids = [line.strip() for line in fh if line.strip() and not line.startswith("#")]
            log.info(f"Loaded {len(setids):,} Set IDs from file {setid_path}")
        else:
            log.error(f"Set ID file not found: {setid_path}")
            sys.exit(1)
            
    # Option B: Query from compiled SQLite database
    elif (output_dir / "dailymed.db").exists():
        log.info("Reading Set IDs from local compiled SQLite database...")
        try:
            conn = sqlite3.connect(output_dir / "dailymed.db")
            cur = conn.cursor()
            limit_sql = f" LIMIT {args.limit}" if args.limit else ""
            cur.execute(f"SELECT setid FROM spl_metadata ORDER BY title ASC{limit_sql};")
            setids = [r[0] for r in cur.fetchall()]
            conn.close()
            log.info(f"Loaded {len(setids):,} Set IDs from SQLite database.")
        except Exception as e:
            log.error(f"Failed to load Set IDs from SQLite: {e}")
            sys.exit(1)
            
    # Option C: Read from catalog JSONL
    elif (output_dir / "dailymed_catalog.jsonl").exists():
        log.info("Reading Set IDs from local catalog JSONL...")
        try:
            with open(output_dir / "dailymed_catalog.jsonl", "r") as fh:
                for line in fh:
                    if line.strip():
                        item = json.loads(line)
                        if item.get("setid"):
                            setids.append(item.get("setid"))
                            if args.limit and len(setids) >= args.limit:
                                break
            log.info(f"Loaded {len(setids):,} Set IDs from catalog JSONL.")
        except Exception as e:
            log.error(f"Failed to load Set IDs from catalog JSONL: {e}")
            sys.exit(1)
    else:
        log.error("No source of Set IDs found. Please run 'download-mappings' first or provide a '--setid-file'.")
        sys.exit(1)
        
    # Apply limit if specified
    if args.limit and len(setids) > args.limit:
        setids = setids[:args.limit]
        log.info(f"Applied limit: processing first {args.limit:,} Set IDs.")
        
    if not setids:
        log.warning("No Set IDs found to enrich. Exiting.")
        return
        
    # 2. Check for completed Set IDs (for resumability)
    completed_setids = set()
    if details_jsonl.exists():
        try:
            with open(details_jsonl, "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        item = json.loads(line)
                        if item.get("setid"):
                            completed_setids.add(item.get("setid"))
            log.info(f"Found {len(completed_setids):,} already enriched Set IDs. Skipping duplicates.")
        except Exception as e:
            log.warning(f"Could not parse details JSONL for progress: {e}")
            
    setids_to_process = [s for s in setids if s not in completed_setids]
    log.info(f"Queue size for enrichment: {len(setids_to_process):,} Set IDs.")
    
    if not setids_to_process:
        log.info("All Set IDs are already processed.")
        return
        
    # 3. Setup output files (append mode)
    csv_exists = details_csv.exists()
    
    csv_fh = open(details_csv, "a", encoding="utf-8", newline="")
    csv_writer = csv.writer(csv_fh)
    if not csv_exists:
        csv_writer.writerow(["setid", "ndc_list", "packaging_descriptions", "media_urls", "error"])
        
    jsonl_fh = open(details_jsonl, "a", encoding="utf-8")
    
    # 4. Run Concurrently using ThreadPoolExecutor
    threads = args.threads or DEFAULT_THREADS
    log.info(f"Running with {threads} threads...")
    
    # Setup Progress Bar
    if tqdm:
        pbar = tqdm(total=len(setids_to_process), desc="Enriching SPLs")
    else:
        pbar = SimpleProgressBar(total=len(setids_to_process), desc="Enriching SPLs")
        
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
            # Submit all tasks
            future_to_setid = {executor.submit(fetch_spl_details, session, sid): sid for sid in setids_to_process}
            
            for future in concurrent.futures.as_completed(future_to_setid):
                sid = future_to_setid[future]
                try:
                    res = future.result()
                    
                    # 1. Write to JSONL
                    jsonl_fh.write(json.dumps(res) + "\n")
                    jsonl_fh.flush()
                    
                    # 2. Write to CSV (flatten lists to pipe-separated strings)
                    csv_writer.writerow([
                        res["setid"],
                        "|".join(res["ndcs"]),
                        "|".join(res["packages"]),
                        "|".join(res["media_urls"]),
                        res["error"] or ""
                    ])
                    csv_fh.flush()
                except Exception as e:
                    log.error(f"Thread execution error for Set ID {sid}: {e}")
                    
                pbar.update(1)
                
    except KeyboardInterrupt:
        log.warning("\nEnrichment interrupted by user. Progress saved.")
    finally:
        csv_fh.close()
        jsonl_fh.close()
        if isinstance(pbar, tqdm):
            pbar.close()
            
    log.info(f"Enrichment complete. Detailed records saved to:\n  - CSV: {details_csv}\n  - JSONL: {details_jsonl}")

# -- Main Entry Point --------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Authoritative data collector and compiler for DailyMed Structured Product Labeling (SPL)."
    )
    
    # Dynamically resolve default output directory to be relative to the script's location
    default_dir = str(BASE_DIR / "dailymed_data")
    
    parser.add_argument(
        "--output-dir",
        default=default_dir,
        help=f"Directory to save log files, database, and CSV outputs (default: {default_dir})"
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    
    # 1. download-mappings parser
    download_p = subparsers.add_parser(
        "download-mappings",
        help="Download NLM bulk mapping ZIP files and compile them into a unified, indexed SQLite database."
    )
    download_p.add_argument(
        "--delete-db",
        action="store_true",
        help="Automatically delete the SQLite database file (dailymed.db) and downloads directory after successfully exporting all CSV mappings."
    )
    
    # 2. search parser
    search_p = subparsers.add_parser(
        "search",
        help="Search for drug labels by title, generic ingredient, NDC, or Set ID."
    )
    search_p.add_argument(
        "--query",
        required=True,
        help="Search term (e.g. drug name, ingredient name, NDC, or Set ID)"
    )
    
    # 3. api-fetch-spls parser
    api_p = subparsers.add_parser(
        "api-fetch-spls",
        help="Harvest active SPL catalog paginated from the DailyMed REST API."
    )
    api_p.add_argument(
        "--pagesize",
        type=int,
        default=100,
        help="Number of records per page (max 100, default: 100)"
    )
    api_p.add_argument(
        "--max-pages",
        type=int,
        help="Limit the run to a specific number of pages"
    )
    
    # 4. api-fetch-details parser
    details_p = subparsers.add_parser(
        "api-fetch-details",
        help="Concurrently enrich a list of Set IDs with NDC, packaging, and media details from the API."
    )
    details_p.add_argument(
        "--setid-file",
        help="Path to a text file containing Set IDs to process (one per line)"
    )
    details_p.add_argument(
        "--limit",
        type=int,
        help="Limit the number of Set IDs to process"
    )
    details_p.add_argument(
        "--threads",
        type=int,
        default=DEFAULT_THREADS,
        help=f"Number of parallel worker threads (default: {DEFAULT_THREADS})"
    )
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(0)
        
    output_dir = Path(args.output_dir)
    setup_logging(output_dir)
    
    # Initialize HTTP Session
    session = build_session()
    
    # Run Subcommands
    try:
        if args.command == "download-mappings":
            handle_download_mappings(args, session, output_dir)
        elif args.command == "search":
            handle_search(args, output_dir)
        elif args.command == "api-fetch-spls":
            handle_api_fetch_spls(args, session, output_dir)
        elif args.command == "api-fetch-details":
            handle_api_fetch_details(args, session, output_dir)
    finally:
        session.close()

if __name__ == "__main__":
    main()
