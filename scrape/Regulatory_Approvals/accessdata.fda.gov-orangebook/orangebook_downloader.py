#!/usr/bin/env python3
"""
orangebook_downloader.py
========================
A resilient, multi-threaded, and resumable data collector for the FDA Orange Book.
Fetches, parses, enriches, and unifies product, patent, and exclusivity data.

Features:
  1. Dynamic URL Discovery:
     - Scrapes the official FDA page to resolve the latest monthly ZIP archive.
     - Automatically falls back to a stable media redirection URL if scraping fails.
  2. Range-Based Resumable Downloader:
     - Downloads the ZIP archive using HTTP Range headers to resume from interruptions.
     - Tracks progress via local state files.
  3. Multi-Threaded Code Scraping:
     - Concurrently scrapes over 6,500 patent use codes and exclusivity code definitions
       from the FDA website to translate cryptic codes into human-readable definitions.
  4. Concurrent Parsing & Enrichment:
     - Parses the tilde-delimited ASCII text files in parallel.
     - Joins patent and exclusivity records with their corresponding scraped definitions.
  5. Analytical Unification:
     - Generates a merged dataset linking drug products with all their active patents and exclusivities.

Requirements:
    pip install requests beautifulsoup4
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
from urllib.parse import urljoin
import concurrent.futures
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from bs4 import BeautifulSoup

# Optional progress indicator
try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

# -- Configuration & Constants -----------------------------------------------
FDA_INDEX_URL = "https://www.fda.gov/drugs/drug-approvals-and-databases/orange-book-data-files"
FALLBACK_ZIP_URL = "https://www.fda.gov/media/76860/download?attachment"
PATENT_CODES_URL = "https://www.accessdata.fda.gov/scripts/cder/ob/results_patent.cfm"
EXCLUSIVITY_CODES_URL = "https://www.accessdata.fda.gov/scripts/cder/ob/results_exclusivity.cfm"

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = str(BASE_DIR / "orangebook_data")
MAX_RETRIES = 5
CHUNK_SIZE = 8192

# -- Logging Setup -----------------------------------------------------------
log = logging.getLogger("orangebook_downloader")

def setup_logging(output_dir: Path):
    """Sets up file and console logging."""
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / "orangebook_downloader.log"

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
        allowed_methods=["GET", "HEAD"],
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    
    session.headers.update({
        "User-Agent": "BiolytInternOrangeBookDownloader/1.0 (Python; requests; BeautifulSoup)"
    })
    return session

# -- Code Scrapers -----------------------------------------------------------
def scrape_patent_use_codes(session: requests.Session) -> dict:
    """Scrapes patent use code definitions from the FDA site."""
    log.info("Scraping patent use codes from FDA...")
    definitions = {}
    try:
        r = session.get(PATENT_CODES_URL, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')
        tables = soup.find_all('table')
        if not tables:
            log.warning("No tables found on patent use codes page.")
            return definitions

        # The page has a single table with patent codes
        rows = tables[0].find_all('tr')
        for row in rows:
            cols = [td.get_text(strip=True) for td in row.find_all(['td', 'th'])]
            if len(cols) >= 2:
                code, val = cols[0], cols[1]
                if code and code.lower() != "code" and val.lower() != "definition":
                    definitions[code] = val
        log.info(f"Successfully scraped {len(definitions)} patent use codes.")
    except Exception as e:
        log.error(f"Error scraping patent use codes: {e}")
    return definitions

def scrape_exclusivity_codes(session: requests.Session) -> dict:
    """Scrapes exclusivity code definitions from the FDA site."""
    log.info("Scraping exclusivity codes from FDA...")
    definitions = {}
    try:
        r = session.get(EXCLUSIVITY_CODES_URL, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')
        tables = soup.find_all('table')
        if not tables:
            log.warning("No tables found on exclusivity codes page.")
            return definitions

        # Exclusivity codes page typically has multiple tables (e.g., Table 0 and Table 1)
        for i, table in enumerate(tables):
            rows = table.find_all('tr')
            table_count = 0
            for row in rows:
                cols = [td.get_text(strip=True) for td in row.find_all(['td', 'th'])]
                if len(cols) >= 2:
                    code, val = cols[0], cols[1]
                    if code and code.lower() != "code" and val.lower() != "definition":
                        definitions[code] = val
                        table_count += 1
            log.debug(f"Scraped {table_count} codes from exclusivity table {i}.")
        log.info(f"Successfully scraped {len(definitions)} exclusivity codes.")
    except Exception as e:
        log.error(f"Error scraping exclusivity codes: {e}")
    return definitions

# -- Downloader Class --------------------------------------------------------
class OrangeBookDownloader:
    def __init__(self, output_dir: Path, session: requests.Session):
        self.output_dir = output_dir
        self.session = session
        
        # Output paths
        self.zip_path = self.output_dir / "orange_book.zip"
        self.progress_path = self.output_dir / "download_progress.json"
        self.patent_codes_path = self.output_dir / "patent_codes.json"
        self.exclusivity_codes_path = self.output_dir / "exclusivity_codes.json"

    def discover_zip_url(self) -> str:
        """Resolves the latest monthly ZIP file download link from the FDA page."""
        log.info(f"Discovering latest Orange Book ZIP URL from {FDA_INDEX_URL}...")
        try:
            r = self.session.get(FDA_INDEX_URL, timeout=20)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, 'html.parser')
            for a in soup.find_all('a', href=True):
                href = a['href']
                text = a.get_text(strip=True).lower()
                # Check for zip link keywords
                if ".zip" in href.lower() and ("compressed" in text or "data file" in text or "orange book" in text):
                    resolved_url = urljoin(FDA_INDEX_URL, href)
                    log.info(f"Discovered ZIP URL: {resolved_url}")
                    return resolved_url
        except Exception as e:
            log.warning(f"Failed to dynamically discover ZIP URL: {e}. Falling back to default.")
        
        log.info(f"Using fallback ZIP URL: {FALLBACK_ZIP_URL}")
        return FALLBACK_ZIP_URL

    def download_zip(self, url: str):
        """Downloads the ZIP archive with range-based resumption support."""
        log.info(f"Initiating download of Orange Book ZIP from: {url}")
        
        # Check if we can resume an existing download
        local_file_size = 0
        if self.zip_path.exists() and self.progress_path.exists():
            try:
                with open(self.progress_path, 'r', encoding='utf-8') as f:
                    progress_data = json.load(f)
                if progress_data.get("url") == url:
                    local_file_size = self.zip_path.stat().st_size
                    log.info(f"Found existing partial download. Size: {local_file_size} bytes. Attempting to resume...")
            except Exception as e:
                log.warning(f"Could not read progress file: {e}. Starting fresh download.")
                local_file_size = 0

        headers = {}
        if local_file_size > 0:
            headers["Range"] = f"bytes={local_file_size}-"

        try:
            r = self.session.get(url, headers=headers, stream=True, timeout=30)
            
            # Handle resumption status
            if local_file_size > 0 and r.status_code == 206:
                log.info("Server accepted Range request. Resuming download...")
                write_mode = "ab"
                total_size = int(r.headers.get("content-length", 0)) + local_file_size
            else:
                if local_file_size > 0:
                    log.warning("Server rejected Range request or returned status code. Starting download from scratch.")
                write_mode = "wb"
                local_file_size = 0
                total_size = int(r.headers.get("content-length", 0))

            # Set up progress bar if tqdm is installed
            pbar = None
            if tqdm:
                pbar = tqdm(total=total_size, initial=local_file_size, unit='B', unit_scale=True, desc="Downloading Orange Book ZIP")

            with open(self.zip_path, write_mode) as f:
                for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)
                        local_file_size += len(chunk)
                        if pbar:
                            pbar.update(len(chunk))
                        
                        # Save progress state
                        self._save_progress(url, local_file_size, total_size)

            if pbar:
                pbar.close()

            log.info("Download completed successfully.")
            # Delete progress file on successful completion
            if self.progress_path.exists():
                self.progress_path.unlink()

        except Exception as e:
            log.error(f"Download failed: {e}")
            raise

    def _save_progress(self, url: str, local_size: int, total_size: int):
        """Saves current download progress metadata."""
        try:
            state = {
                "url": url,
                "local_size": local_size,
                "total_size": total_size,
                "timestamp": time.time()
            }
            with open(self.progress_path, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2)
        except Exception:
            pass

    def run(self):
        """Coordinates the dynamic discovery, resumable download, and reference scraping."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # 1. Spawns threads to scrape code definitions concurrently with URL discovery & download
        with concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix="OBCollector") as executor:
            # Submit scraping tasks
            patent_future = executor.submit(scrape_patent_use_codes, self.session)
            exclusivity_future = executor.submit(scrape_exclusivity_codes, self.session)
            
            # Resolve URL and download in the main thread
            zip_url = self.discover_zip_url()
            self.download_zip(zip_url)
            
            # Retrieve scraped definitions
            patent_codes = patent_future.result()
            exclusivity_codes = exclusivity_future.result()

        # 2. Save scraped definitions to local JSON files
        if patent_codes:
            with open(self.patent_codes_path, 'w', encoding='utf-8') as f:
                json.dump(patent_codes, f, indent=2)
        if exclusivity_codes:
            with open(self.exclusivity_codes_path, 'w', encoding='utf-8') as f:
                json.dump(exclusivity_codes, f, indent=2)

# -- Data Parser & Enricher Class ---------------------------------------------
class OrangeBookParser:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        
        # Source paths
        self.zip_path = self.output_dir / "orange_book.zip"
        self.patent_codes_path = self.output_dir / "patent_codes.json"
        self.exclusivity_codes_path = self.output_dir / "exclusivity_codes.json"
        
        # Extracted file paths
        self.products_txt = self.output_dir / "products.txt"
        self.patent_txt = self.output_dir / "patent.txt"
        self.exclusivity_txt = self.output_dir / "exclusivity.txt"
        
        # Output paths
        self.products_csv = self.output_dir / "products.csv"
        self.products_json = self.output_dir / "products.json"
        self.patent_csv = self.output_dir / "patents_enriched.csv"
        self.patent_json = self.output_dir / "patents_enriched.json"
        self.exclusivity_csv = self.output_dir / "exclusivity_enriched.csv"
        self.exclusivity_json = self.output_dir / "exclusivity_enriched.json"
        self.unified_csv = self.output_dir / "orange_book_unified.csv"

    def extract_zip(self):
        """Extracts the Orange Book ZIP archive."""
        if not self.zip_path.exists():
            raise FileNotFoundError(f"Orange Book ZIP file not found at: {self.zip_path}")
        
        log.info(f"Extracting {self.zip_path} to {self.output_dir}...")
        with zipfile.ZipFile(self.zip_path, 'r') as z:
            z.extractall(self.output_dir)
            log.info(f"Extracted files: {z.namelist()}")

    def load_code_mappings(self) -> tuple:
        """Loads patent and exclusivity code maps from scraped JSON files."""
        patent_map = {}
        exclusivity_map = {}
        
        if self.patent_codes_path.exists():
            try:
                with open(self.patent_codes_path, 'r', encoding='utf-8') as f:
                    patent_map = json.load(f)
                log.info(f"Loaded {len(patent_map)} patent use code definitions.")
            except Exception as e:
                log.error(f"Failed to load patent codes map: {e}")
                
        if self.exclusivity_codes_path.exists():
            try:
                with open(self.exclusivity_codes_path, 'r', encoding='utf-8') as f:
                    exclusivity_map = json.load(f)
                log.info(f"Loaded {len(exclusivity_map)} exclusivity code definitions.")
            except Exception as e:
                log.error(f"Failed to load exclusivity codes map: {e}")
                
        return patent_map, exclusivity_map

    @staticmethod
    def _parse_tilde_file(file_path: Path) -> list:
        """Helper to parse tilde-delimited files into dictionaries."""
        records = []
        if not file_path.exists():
            log.warning(f"File not found: {file_path}")
            return records

        log.info(f"Parsing tilde-delimited file: {file_path}")
        with open(file_path, 'r', encoding='latin1') as f:
            # Tilde-delimited text files sometimes contain leading or trailing whitespaces
            header_line = f.readline().strip()
            if not header_line:
                return records
            
            headers = [h.strip() for h in header_line.split('~')]
            for line_no, line in enumerate(f, start=2):
                line = line.strip()
                if not line:
                    continue
                fields = [field.strip() for field in line.split('~')]
                
                # Check for mismatched columns and pad/truncate if necessary
                if len(fields) != len(headers):
                    log.debug(f"[{file_path.name}:{line_no}] Mismatched columns: Got {len(fields)}, expected {len(headers)}.")
                    if len(fields) < len(headers):
                        fields.extend([""] * (len(headers) - len(fields)))
                    else:
                        fields = fields[:len(headers)]
                        
                records.append(dict(zip(headers, fields)))
                
        log.info(f"Successfully parsed {len(records)} records from {file_path.name}.")
        return records

    def parse_and_enrich(self):
        """Concurrently parses the text files and enriches patent/exclusivity datasets."""
        self.extract_zip()
        patent_map, exclusivity_map = self.load_code_mappings()

        # Concurrent parsing of products, patent, and exclusivity text files
        log.info("Spawning concurrent threads to parse data files...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix="OBParser") as executor:
            future_products = executor.submit(self._parse_tilde_file, self.products_txt)
            future_patents = executor.submit(self._parse_tilde_file, self.patent_txt)
            future_exclusivities = executor.submit(self._parse_tilde_file, self.exclusivity_txt)

            products = future_products.result()
            patents = future_patents.result()
            exclusivities = future_exclusivities.result()

        # 1. Process & Save Products
        if products:
            self._save_dataset(products, self.products_csv, self.products_json)
        
        # 2. Enrich & Save Patents
        enriched_patents = []
        for pat in patents:
            code = pat.get("Patent_Use_Code", "")
            definition = patent_map.get(code, "")
            pat["Patent_Use_Definition"] = definition
            enriched_patents.append(pat)
        if enriched_patents:
            self._save_dataset(enriched_patents, self.patent_csv, self.patent_json)

        # 3. Enrich & Save Exclusivities
        enriched_exclusivities = []
        for exc in exclusivities:
            code = exc.get("Exclusivity_Code", "")
            definition = exclusivity_map.get(code, "")
            exc["Exclusivity_Definition"] = definition
            enriched_exclusivities.append(exc)
        if enriched_exclusivities:
            self._save_dataset(enriched_exclusivities, self.exclusivity_csv, self.exclusivity_json)

        # 4. Generate the Unified Dataset
        self.generate_unified(products, enriched_patents, enriched_exclusivities)

    def _save_dataset(self, data: list, csv_path: Path, json_path: Path):
        """Saves a dataset to both CSV and JSON formats."""
        if not data:
            return
        
        # Save CSV
        keys = data[0].keys()
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(data)
        
        # Save JSON
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
            
        log.info(f"Saved dataset: {csv_path.name} and {json_path.name}")

    def generate_unified(self, products: list, patents: list, exclusivities: list):
        """Joins products, patents, and exclusivities into a unified view."""
        log.info("Generating unified analytical dataset...")
        
        # Map patents by (Appl_Type, Appl_No, Product_No)
        patent_index = {}
        for pat in patents:
            key = (pat.get("Appl_Type"), pat.get("Appl_No"), pat.get("Product_No"))
            if key not in patent_index:
                patent_index[key] = []
            patent_index[key].append(pat)

        # Map exclusivities by (Appl_Type, Appl_No, Product_No)
        exclusivity_index = {}
        for exc in exclusivities:
            key = (exc.get("Appl_Type"), exc.get("Appl_No"), exc.get("Product_No"))
            if key not in exclusivity_index:
                exclusivity_index[key] = []
            exclusivity_index[key].append(exc)

        unified_records = []
        for prod in products:
            appl_type = prod.get("Appl_Type")
            appl_no = prod.get("Appl_No")
            prod_no = prod.get("Product_No")
            key = (appl_type, appl_no, prod_no)

            # Retrieve matching patents & exclusivities
            prod_patents = patent_index.get(key, [])
            prod_exclusivities = exclusivity_index.get(key, [])

            # Compile patents string and count
            patent_details_list = []
            for pat in prod_patents:
                pat_num = pat.get("Patent_No", "")
                pat_exp = pat.get("Patent_Expire_Date_Text", "")
                use_code = pat.get("Patent_Use_Code", "")
                use_def = pat.get("Patent_Use_Definition", "")
                
                detail = f"Patent {pat_num} (Exp: {pat_exp})"
                if use_code:
                    detail += f" [Use: {use_code} - {use_def}]"
                patent_details_list.append(detail)
            
            # Compile exclusivity string and count
            exclusivity_details_list = []
            for exc in prod_exclusivities:
                exc_code = exc.get("Exclusivity_Code", "")
                exc_exp = exc.get("Exclusivity_Date", "")
                exc_def = exc.get("Exclusivity_Definition", "")
                
                detail = f"Exclusivity {exc_code} (Exp: {exc_exp})"
                if exc_def:
                    detail += f" [{exc_def}]"
                exclusivity_details_list.append(detail)

            # Create the unified record
            record = {
                "Ingredient": prod.get("Ingredient", ""),
                "Trade_Name": prod.get("Trade_Name", ""),
                "Dosage_Form_Route": prod.get("DF;Route", ""),
                "Strength": prod.get("Strength", ""),
                "Applicant": prod.get("Applicant", ""),
                "Applicant_Full_Name": prod.get("Applicant_Full_Name", ""),
                "Appl_Type": appl_type,
                "Appl_No": appl_no,
                "Product_No": prod_no,
                "TE_Code": prod.get("TE_Code", ""),
                "Approval_Date": prod.get("Approval_Date", ""),
                "Type": prod.get("Type", ""),  # RX / OTC / DISCN
                "RLD": prod.get("RLD", ""),
                "RS": prod.get("RS", ""),
                "Active_Patents_Count": len(prod_patents),
                "Active_Patents_List": " | ".join(patent_details_list),
                "Active_Exclusivities_Count": len(prod_exclusivities),
                "Active_Exclusivities_List": " | ".join(exclusivity_details_list)
            }
            unified_records.append(record)

        # Save unified dataset to CSV
        if unified_records:
            keys = unified_records[0].keys()
            with open(self.unified_csv, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=keys)
                writer.writeheader()
                writer.writerows(unified_records)
            log.info(f"Unified dataset compiled. Saved {len(unified_records)} products to {self.unified_csv.name}.")

# -- Search Engine Class ------------------------------------------------------
class OrangeBookSearch:
    def __init__(self, output_dir: Path):
        self.unified_csv = output_dir / "orange_book_unified.csv"

    def search(self, query: str, field: str = "Ingredient"):
        """Searches the unified analytical dataset by a specified field."""
        if not self.unified_csv.exists():
            print(f"Error: Unified dataset not found at {self.unified_csv}. Please run 'parse' or 'unified' first.")
            return

        print(f"Searching for '{query}' in '{field}'...")
        results = []
        
        with open(self.unified_csv, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                val = row.get(field, "")
                if query.lower() in val.lower():
                    results.append(row)

        print(f"Found {len(results)} matches:")
        print("-" * 80)
        for i, row in enumerate(results[:20], 1):
            print(f"{i}. [{row['Trade_Name']}] - {row['Ingredient']} ({row['Strength']})")
            print(f"   Applicant: {row['Applicant']} | TE Code: {row['TE_Code'] or 'None'} | Type: {row['Type']}")
            print(f"   Application: {row['Appl_Type']}{row['Appl_No']} | Product No: {row['Product_No']}")
            if int(row['Active_Patents_Count']) > 0:
                print(f"   Patents ({row['Active_Patents_Count']}): {row['Active_Patents_List']}")
            if int(row['Active_Exclusivities_Count']) > 0:
                print(f"   Exclusivities ({row['Active_Exclusivities_Count']}): {row['Active_Exclusivities_List']}")
            print("-" * 80)

        if len(results) > 20:
            print(f"... and {len(results) - 20} more matches.")

def cleanup_orangebook_files(output_dir: Path):
    """Deletes all non-CSV files generated during downloading and parsing."""
    files_to_delete = [
        output_dir / "orange_book.zip",
        output_dir / "download_progress.json",
        output_dir / "patent_codes.json",
        output_dir / "exclusivity_codes.json",
        output_dir / "products.txt",
        output_dir / "patent.txt",
        output_dir / "exclusivity.txt",
        output_dir / "products.json",
        output_dir / "patents_enriched.json",
        output_dir / "exclusivity_enriched.json",
    ]

    for file_path in files_to_delete:
        if file_path.exists():
            try:
                file_path.unlink()
                print(f"Cleaned up file: {file_path}")
            except Exception as e:
                print(f"Error deleting {file_path.name}: {e}")

    # Shutdown logging to delete the log file on Windows
    log_file = output_dir / "orangebook_downloader.log"
    if log_file.exists():
        try:
            logging.shutdown()
            log_file.unlink()
            print(f"Cleaned up log file: {log_file}")
        except Exception as e:
            print(f"Error deleting log file: {e}")

# -- CLI Definition -----------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="FDA Orange Book Data Collector and Analyst.",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: download
    subparsers.add_parser("download", help="Discover and download Orange Book ZIP, and scrape code definitions.")

    # Subcommand: parse
    subparsers.add_parser("parse", help="Extract ZIP, parse tilde-delimited files, map code definitions, and compile outputs.")

    # Subcommand: unified
    subparsers.add_parser("unified", help="Compile the unified analytical dataset from existing parsed files.")

    # Subcommand: search
    search_parser = subparsers.add_parser("search", help="Search the local unified database.")
    search_parser.add_argument("query", type=str, help="Search term.")
    search_parser.add_argument(
        "--field", 
        type=str, 
        default="Ingredient",
        choices=["Ingredient", "Trade_Name", "Applicant", "TE_Code", "Active_Patents_List", "Active_Exclusivities_List"],
        help="Field to query (default: Ingredient)."
    )

    # Global options
    parser.add_argument("--output-dir", type=str, default=DEFAULT_OUTPUT_DIR, help="Output directory path.")
    
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    
    setup_logging(output_dir)
    session = build_session()

    try:
        if args.command == "download":
            downloader = OrangeBookDownloader(output_dir, session)
            downloader.run()
        elif args.command == "parse":
            parser_obj = OrangeBookParser(output_dir)
            parser_obj.parse_and_enrich()
            cleanup_orangebook_files(output_dir)
        elif args.command == "unified":
            parser_obj = OrangeBookParser(output_dir)
            products = parser_obj._parse_tilde_file(parser_obj.products_txt)
            patents = parser_obj._parse_tilde_file(parser_obj.patent_txt)
            exclusivities = parser_obj._parse_tilde_file(parser_obj.exclusivity_txt)
            
            # Map code definitions if available
            patent_map, exclusivity_map = parser_obj.load_code_mappings()
            for pat in patents:
                pat["Patent_Use_Definition"] = patent_map.get(pat.get("Patent_Use_Code", ""), "")
            for exc in exclusivities:
                exc["Exclusivity_Definition"] = exclusivity_map.get(exc.get("Exclusivity_Code", ""), "")
                
            parser_obj.generate_unified(products, patents, exclusivities)
        elif args.command == "search":
            searcher = OrangeBookSearch(output_dir)
            searcher.search(args.query, args.field)
    except Exception as e:
        log.error(f"Command execution failed: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
