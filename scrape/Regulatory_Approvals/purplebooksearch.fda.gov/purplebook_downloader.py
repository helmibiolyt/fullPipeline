#!/usr/bin/env python3
"""
purplebook_downloader.py
========================
A resilient, multi-threaded, and resumable data collector for the FDA Purple Book.
Fetches, parses, enriches, and unifies biological product, patent, and glossary data.

Features:
  1. Dynamic URL Discovery:
     - Scrapes the official FDA downloads page to resolve the latest monthly CSV file.
     - Target the most recent month (last link in the current year container).
  2. Range-Based Resumable Downloader:
     - Downloads the monthly CSV using HTTP Range headers to resume from interruptions.
  3. Master Patent Scraper:
     - Scrapes the comprehensive patent list master table from the FDA Purple Book site.
  4. Glossary Extractor:
     - Downloads the JavaScript glossary file and extracts term definitions.
  5. Multi-Threaded Deep Crawler (Optional):
     - Crawls individual BLA detail pages concurrently using a thread pool.
     - Implements JSON-based checkpointing to resume exactly where it was interrupted.
  6. Resilient Column Normalization:
     - Handles varying column names across monthly CSV versions case-insensitively.
  7. Analytical Data Unification:
     - Combines products, monthly changes, patent lists, glossary definitions, and crawled data.
     - Resolves reference product BLA numbers while avoiding N/A brand name collisions.
     - Generates enriched JSON and CSV outputs.

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
import threading
import re
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
BASE_URL = "https://purplebooksearch.fda.gov/"
DOWNLOADS_URL = urljoin(BASE_URL, "index.cfm?event=downloads")
PATENT_LIST_URL = urljoin(BASE_URL, "index.cfm?event=patentlist")
GLOSSARY_JS_URL = urljoin(BASE_URL, "assets/js/glossary.js")

MAX_RETRIES = 5
CHUNK_SIZE = 8192
DEFAULT_THREADS = 5
INVALID_BRAND_NAMES = {'', 'n/a', 'na', 'n.a.', 'none', 'null'}

BASE_DIR = Path(__file__).resolve().parent

# -- Logging Setup -----------------------------------------------------------
log = logging.getLogger("purplebook_downloader")

def setup_logging(output_dir: Path):
    """Sets up file and console logging."""
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / "purplebook_downloader.log"

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
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5"
    })
    return session

# -- Helper for Resilient Key Lookup -----------------------------------------
def get_val(row: dict, keys: list[str], default: str = "") -> str:
    """Gets value from a dictionary using case-insensitive and space-insensitive key matching."""
    for k in keys:
        k_norm = k.lower().replace(' ', '').replace('_', '').replace('.', '')
        for rk, rv in row.items():
            rk_norm = rk.lower().replace(' ', '').replace('_', '').replace('.', '')
            if rk_norm == k_norm:
                return rv.strip() if rv is not None else default
    return default

# -- Core Downloader Class ----------------------------------------------------
class PurpleBookDownloader:
    def __init__(self, output_dir: Path, session: requests.Session, force_download: bool = False):
        self.output_dir = output_dir
        self.session = session
        self.force_download = force_download
        
        # Output paths
        self.csv_path = self.output_dir / "purplebook_raw_monthly.csv"
        self.progress_path = self.output_dir / "download_progress.json"
        self.patent_html_path = self.output_dir / "patent_list.html"
        self.glossary_js_path = self.output_dir / "glossary.js"

    def discover_csv_url(self) -> str:
        """Resolves the latest monthly CSV file download link from the FDA downloads page."""
        log.info(f"Discovering latest Purple Book CSV URL from {DOWNLOADS_URL}...")
        try:
            r = self.session.get(DOWNLOADS_URL, timeout=20)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, 'html.parser')
            
            current_container = soup.find(class_=re.compile(r'(current|current-year|current_year)', re.IGNORECASE))
            search_scope = current_container if current_container else soup
            
            csv_links = []
            for a in search_scope.find_all('a', href=True):
                href = a['href']
                text = a.get_text(strip=True).upper()
                if ".csv" in href.lower() and "CSV" in text:
                    resolved_url = urljoin(DOWNLOADS_URL, href)
                    csv_links.append(resolved_url)
            
            if csv_links:
                latest_url = csv_links[-1]
                log.info(f"Successfully discovered latest monthly CSV URL: {latest_url}")
                return latest_url
                
        except Exception as e:
            log.error(f"Failed to dynamically discover CSV URL: {e}")
            
        fallback_url = "https://www.accessdata.fda.gov/drugsatfda_docs/PurpleBook/2026/purplebook-search-May-data-download.csv"
        log.warning(f"Using fallback CSV URL: {fallback_url}")
        return fallback_url

    def download_csv(self, url: str):
        """Downloads the monthly CSV file with range-based resumption support."""
        if self.csv_path.exists() and not self.force_download:
            log.info(f"CSV file already exists at {self.csv_path}. Skipping download. Use --force to re-download.")
            return

        log.info(f"Initiating download of Purple Book CSV from: {url}")
        
        local_file_size = 0
        if self.csv_path.exists() and self.progress_path.exists():
            try:
                with open(self.progress_path, 'r', encoding='utf-8') as f:
                    progress_data = json.load(f)
                if progress_data.get("url") == url:
                    local_file_size = self.csv_path.stat().st_size
                    log.info(f"Found existing partial download. Size: {local_file_size} bytes. Resuming...")
            except Exception as e:
                log.warning(f"Could not read progress file: {e}. Starting fresh download.")
                local_file_size = 0

        headers = {}
        if local_file_size > 0:
            headers["Range"] = f"bytes={local_file_size}-"

        try:
            r = self.session.get(url, headers=headers, stream=True, timeout=30)
            
            if local_file_size > 0 and r.status_code == 206:
                log.info("Server accepted Range request. Appending to existing file...")
                write_mode = "ab"
                total_size = int(r.headers.get("content-length", 0)) + local_file_size
            else:
                if local_file_size > 0:
                    log.warning("Server rejected Range request. Starting download from scratch...")
                write_mode = "wb"
                local_file_size = 0
                total_size = int(r.headers.get("content-length", 0))

            pbar = None
            if tqdm:
                pbar = tqdm(total=total_size, initial=local_file_size, unit='B', unit_scale=True, desc="Downloading Monthly CSV")

            with open(self.csv_path, write_mode) as f:
                for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)
                        local_file_size += len(chunk)
                        if pbar:
                            pbar.update(len(chunk))
                        self._save_download_progress(url, local_file_size, total_size)

            if pbar:
                pbar.close()

            log.info("Monthly CSV download completed successfully.")
            if self.progress_path.exists():
                self.progress_path.unlink()

        except Exception as e:
            log.error(f"Monthly CSV download failed: {e}")
            raise

    def _save_download_progress(self, url: str, local_size: int, total_size: int):
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

    def fetch_master_patent_list(self) -> str:
        """Downloads the master patent list page HTML."""
        log.info(f"Fetching master patent list from {PATENT_LIST_URL}...")
        try:
            r = self.session.get(PATENT_LIST_URL, timeout=30)
            r.raise_for_status()
            with open(self.patent_html_path, 'w', encoding='utf-8') as f:
                f.write(r.text)
            log.info("Master patent list HTML saved.")
            return r.text
        except Exception as e:
            log.error(f"Failed to fetch master patent list: {e}")
            raise

    def fetch_glossary_js(self) -> str:
        """Downloads the glossary.js file containing definitions."""
        log.info(f"Fetching glossary JS from {GLOSSARY_JS_URL}...")
        try:
            r = self.session.get(GLOSSARY_JS_URL, timeout=30)
            r.raise_for_status()
            with open(self.glossary_js_path, 'w', encoding='utf-8') as f:
                f.write(r.text)
            log.info("Glossary JS saved.")
            return r.text
        except Exception as e:
            log.error(f"Failed to fetch glossary JS: {e}")
            raise

    def run(self) -> tuple[str, str, str]:
        """Runs the bulk collection engine to fetch CSV, patents, and glossary."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # 1. Discover and download monthly CSV (latest month)
        csv_url = self.discover_csv_url()
        self.download_csv(csv_url)
        
        # 2. Concurrently scrape patent list HTML and glossary JS
        with concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="BulkCollector") as executor:
            patent_future = executor.submit(self.fetch_master_patent_list)
            glossary_future = executor.submit(self.fetch_glossary_js)
            
            patent_html = patent_future.result()
            glossary_js = glossary_future.result()
            
        # Read monthly CSV content to return
        with open(self.csv_path, 'r', encoding='utf-8', errors='ignore') as f:
            csv_text = f.read()
            
        return csv_text, patent_html, glossary_js

# -- Data Parser Class --------------------------------------------------------
class PurpleBookParser:
    @staticmethod
    def parse_monthly_csv(csv_text: str) -> tuple[list[dict], list[dict]]:
        """Parses the Purple Book monthly CSV and splits it into changes and products."""
        lines = csv_text.splitlines()
        changes = []
        products = []
        
        # Identify the indices of header rows
        header_indices = []
        for idx, line in enumerate(lines):
            if "Applicant,BLA Number" in line or "BLA Number,Proprietary Name" in line:
                header_indices.append(idx)
                
        if not header_indices:
            log.error("Could not find any header rows containing 'Applicant,BLA Number' in the CSV.")
            raise ValueError("Invalid CSV format: header rows missing.")
            
        log.info(f"Identified header rows at line numbers: {[i+1 for i in header_indices]}")
        
        # Section 1: Changes (from first header to second header or end)
        changes_start = header_indices[0]
        changes_end = header_indices[1] if len(header_indices) > 1 else len(lines)
        
        changes_section = [lines[changes_start]]
        for idx in range(changes_start + 1, changes_end):
            line = lines[idx]
            if not line.strip() or line.replace(',', '').strip() == "":
                continue
            changes_section.append(line)
            
        if len(changes_section) > 1:
            reader = csv.DictReader(changes_section)
            changes = list(reader)
            log.info(f"Successfully parsed {len(changes)} monthly change records.")
            
        # Section 2: Full Database (from second header to the end)
        if len(header_indices) > 1:
            products_start = header_indices[1]
            products_section = [lines[products_start]]
            for idx in range(products_start + 1, len(lines)):
                line = lines[idx]
                if not line.strip() or line.replace(',', '').strip() == "":
                    continue
                products_section.append(line)
                
            if len(products_section) > 1:
                reader = csv.DictReader(products_section)
                products = list(reader)
                log.info(f"Successfully parsed {len(products)} full database product records.")
                
        return changes, products

    @staticmethod
    def parse_patent_list(html_text: str) -> list[dict]:
        """Parses the patent list master table from HTML."""
        log.info("Parsing master patent list table...")
        soup = BeautifulSoup(html_text, 'html.parser')
        table = soup.find('table')
        patents = []
        if not table:
            log.warning("No table element found on the patent list page.")
            return patents
            
        rows = table.find_all('tr')
        if not rows:
            log.warning("No rows found in the patent list table.")
            return patents
            
        headers = [th.get_text(strip=True) for th in rows[0].find_all(['th', 'td'])]
        log.info(f"Patent table headers: {headers}")
        
        for idx, r in enumerate(rows[1:]):
            cells = [td.get_text(strip=True) for td in r.find_all('td')]
            if len(cells) == len(headers):
                patents.append(dict(zip(headers, cells)))
            else:
                log.debug(f"Row {idx+1} cell count mismatch. Expected {len(headers)}, got {len(cells)}.")
                
        log.info(f"Successfully parsed {len(patents)} patent records.")
        return patents

    @staticmethod
    def parse_glossary_js(js_text: str) -> dict[str, str]:
        """Parses the glossary.js file to extract terms and definitions."""
        log.info("Extracting glossary terms and definitions...")
        glossary = {}
        
        # Extract the body of the glossaryData object
        match = re.search(r'const\s+glossaryData\s*=\s*\{([\s\S]*?)\};', js_text)
        if not match:
            match = re.search(r'glossaryData\s*=\s*\{([\s\S]*?)\};', js_text)
            
        if not match:
            log.warning("Could not locate the glossaryData variable in glossary.js.")
            return glossary
            
        body = match.group(1)
        
        # Regex to parse Javascript object key-value pairs (handles both double quotes and backticks)
        pattern = re.compile(r'"([^"]+)"\s*:\s*(?:"((?:[^"\\]|\\.)*)"|`([^`]*)`)', re.DOTALL)
        matches = pattern.findall(body)
        
        for k, dq_val, bt_val in matches:
            val = dq_val if dq_val else bt_val
            if dq_val:
                val = dq_val.replace('\\"', '"').replace('\\n', '\n')
            if bt_val:
                # Format multiline backtick text nicely
                lines = [line.strip() for line in bt_val.split('\n')]
                val = '\n'.join([l for l in lines if l])
            glossary[k] = val.strip()
            
        log.info(f"Successfully extracted {len(glossary)} glossary definitions.")
        return glossary

# -- Deep Crawler Class (Resumable & Multi-threaded) --------------------------
class PurpleBookDeepCrawler:
    def __init__(self, output_dir: Path, session: requests.Session, threads: int = DEFAULT_THREADS, max_products: int = None):
        self.output_dir = output_dir
        self.session = session
        self.threads = threads
        self.max_products = max_products
        
        self.checkpoint_path = self.output_dir / "deep_crawl_progress.json"
        
        # Thread-safe collections
        self.completed_blas = set()
        self.results = {}
        self.progress_lock = threading.Lock()
        
        self._load_checkpoint()

    def _load_checkpoint(self):
        """Loads crawling progress from a JSON checkpoint file if it exists."""
        if self.checkpoint_path.exists():
            try:
                with open(self.checkpoint_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.completed_blas = set(data.get("completed_blas", []))
                self.results = data.get("results", {})
                log.info(f"Loaded deep crawl checkpoint. Resuming from {len(self.completed_blas)} completed BLAs.")
            except Exception as e:
                log.warning(f"Could not load deep crawl checkpoint: {e}. Starting fresh crawl.")

    def _save_checkpoint(self):
        """Thread-safely saves crawling progress to a JSON checkpoint file."""
        with self.progress_lock:
            try:
                temp_path = self.checkpoint_path.with_suffix(".tmp")
                with open(temp_path, 'w', encoding='utf-8') as f:
                    json.dump({
                        "completed_blas": list(self.completed_blas),
                        "results": self.results
                    }, f, indent=2)
                temp_path.replace(self.checkpoint_path)
            except Exception as e:
                log.error(f"Failed to save deep crawl checkpoint: {e}")

    def crawl_product(self, bla_no: str):
        """Crawls and parses an individual product details page."""
        url = urljoin(BASE_URL, f"index.cfm?event=productdetails&blaNo={bla_no}")
        log.debug(f"Crawling detail page for BLA: {bla_no}")
        
        try:
            r = self.session.get(url, timeout=20)
            r.raise_for_status()
            
            soup = BeautifulSoup(r.text, 'html.parser')
            product_data = {"bla_number": bla_no, "products": [], "metadata": {}}
            
            # 1. Parse BLA metadata (applicant, approval dates, etc.)
            details_div = soup.find('div', class_='product-details')
            if details_div:
                cols = details_div.find_all('div', class_=re.compile(r'col-md-\d+'))
                for col in cols:
                    strong = col.find('strong')
                    if strong:
                        label = strong.get_text(strip=True)
                        text = col.get_text(strip=True)
                        val = text.replace(label, '').strip()
                        key = label.lower().replace(' ', '_').replace('.', '').replace('(', '').replace(')', '')
                        product_data["metadata"][key] = val
            
            # 2. Parse product presentations grid (strength, dosage form, route, etc.)
            grid_div = soup.find('div', class_='product-grid')
            if grid_div:
                header_row = grid_div.find('div', class_='grid-header')
                if header_row:
                    headers = [d.get_text(strip=True) for d in header_row.find_all('div')]
                    rows = grid_div.find_all('div', class_='grid-row')
                    for row in rows:
                        if 'grid-header' in row.get('class', []):
                            continue
                        cells = [d.get_text(strip=True) for d in row.find_all('div')]
                        if len(cells) == len(headers):
                            product_data["products"].append(dict(zip(headers, cells)))
            
            with self.progress_lock:
                self.results[bla_no] = product_data
                self.completed_blas.add(bla_no)
                
            self._save_checkpoint()
            log.info(f"Successfully crawled BLA: {bla_no}")
            
        except Exception as e:
            log.error(f"Error crawling BLA {bla_no}: {e}")

    def run(self, bla_numbers: list[str]) -> dict:
        """Coordinates the multi-threaded crawl over the list of BLA numbers."""
        to_crawl = [bla for bla in bla_numbers if bla not in self.completed_blas]
        
        if self.max_products:
            to_crawl = to_crawl[:self.max_products]
            
        if not to_crawl:
            log.info("No new BLA detail pages to crawl.")
            return self.results
            
        log.info(f"Starting deep crawl of {len(to_crawl)} BLA detail pages using {self.threads} threads...")
        
        pbar = None
        if tqdm:
            pbar = tqdm(total=len(to_crawl), desc="Crawling Detail Pages")
            
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.threads, thread_name_prefix="DeepCrawler") as executor:
            futures = {executor.submit(self.crawl_product, bla): bla for bla in to_crawl}
            
            for future in concurrent.futures.as_completed(futures):
                bla = futures[future]
                if pbar:
                    pbar.update(1)
                    
        if pbar:
            pbar.close()
            
        log.info(f"Deep crawl finished. Total crawled records: {len(self.results)}")
        
        all_requested_done = all(bla in self.completed_blas for bla in to_crawl)
        if all_requested_done and not self.max_products and self.checkpoint_path.exists():
            try:
                self.checkpoint_path.unlink()
                log.info("Crawl completed fully. Removed progress checkpoint.")
            except Exception:
                pass
                
        return self.results

# -- Data Unifier & Analytical Aggregator -------------------------------------
class PurpleBookDataUnifier:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        # Path to the monthly raw CSV (referenced in the final unify log/return).
        self.csv_path = self.output_dir / "purplebook_raw_monthly.csv"

    def unify_data(self, products: list[dict], changes: list[dict], patents: list[dict], glossary: dict, crawled_data: dict = None) -> Path:
        """Unifies the products database with patents, glossary definitions, and constructed links."""
        log.info("Initiating analytical data unification...")
        
        # Group patents by Reference Product BLA Number
        patents_by_ref_bla = {}
        for p in patents:
            ref_bla = get_val(p, ['Reference Product BLA Number', 'BLA Number'])
            if ref_bla:
                ref_bla_clean = ref_bla.strip().lstrip('0')
                patents_by_ref_bla.setdefault(ref_bla_clean, []).append({
                    "patent_number": get_val(p, ['Patent Number']),
                    "expiration_date": get_val(p, ['Patent Expiration Date'])
                })
        
        # Resolve reference BLA numbers and link patents to biological products
        # We build a lookup of reference products: proprietary_name -> BLA number for 351(a) products
        originators = {}
        for prod in products:
            license_type = get_val(prod, ['License Type', 'BLA Type'])
            bla = get_val(prod, ['BLA Number']).strip().lstrip('0')
            prop_name = get_val(prod, ['Proprietary Name']).strip().lower()
            # ONLY index genuine brand names. Avoid 'N/A' or empty strings!
            if '351(a)' in license_type and prop_name and prop_name not in INVALID_BRAND_NAMES and bla:
                originators[prop_name] = bla
                
        log.info(f"Indexed {len(originators)} reference/originator products for BLA resolution.")

        enriched_products = []
        for prod in products:
            bla = get_val(prod, ['BLA Number']).strip().lstrip('0')
            prop_name = get_val(prod, ['Proprietary Name'])
            proper_name = get_val(prod, ['Proper Name'])
            license_type = get_val(prod, ['License Type', 'BLA Type'])
            
            # Construct base enriched product record using our resilient get_val helper
            item = {
                "bla_number": get_val(prod, ['BLA Number']),
                "proprietary_name": prop_name if prop_name else "N/A",
                "proper_name": proper_name,
                "license_type": license_type,
                "applicant": get_val(prod, ['Applicant']),
                "licensure": get_val(prod, ['Licensure'], 'Licensed'),
                "approval_date": get_val(prod, ['Approval Date', 'Original Approval Date']),
                "marketing_status": get_val(prod, ['Marketing Status'], 'Rx'),
                "dosage_form": get_val(prod, ['Dosage Form']),
                "route_of_administration": get_val(prod, ['Route of Administration']),
                "strength": get_val(prod, ['Strength']),
                "product_presentation": get_val(prod, ['Product Presentation']),
                "product_number": get_val(prod, ['Product Number']),
                "center": get_val(prod, ['Center']),
                "date_of_first_licensure": get_val(prod, ['Date of First Licensure']),
                "exclusivity_expiration_date": get_val(prod, ['Exclusivity Expiration Date']),
                "first_interchangeable_exclusivity_exp_date": get_val(prod, ['First Interchangeable Exclusivity Exp. Date', 'First Interchangeable Exclusivity Expiration Date']),
                "ref_product_exclusivity_exp_date": get_val(prod, ['Ref. Product Exclusivity Exp. Date', 'Reference Product Exclusivity Expiration Date']),
                "orphan_exclusivity_exp_date": get_val(prod, ['Orphan Exclusivity Exp. Date', 'Orphan Exclusivity Expiration Date']),
                "patent_list_provided": get_val(prod, ['Patent List Provided'], 'No')
            }
            
            # Construct product label URL pointing to Drugs@FDA
            item["product_label_url"] = f"https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo={get_val(prod, ['BLA Number'])}"
            
            # Determine reference product BLA number
            ref_prop_name = get_val(prod, ['Ref. Product Proprietary Name', 'Reference Product Proprietary Name']).strip().lower()
            ref_bla = ""
            if ref_prop_name and ref_prop_name not in INVALID_BRAND_NAMES:
                ref_bla = originators.get(ref_prop_name, "")
            
            # If BLA itself is originator (351(a)), it is its own reference BLA for patent lookup
            lookup_bla = bla if '351(a)' in license_type else ref_bla
            
            # Link patents
            item["patents"] = patents_by_ref_bla.get(lookup_bla, [])
            item["resolved_reference_bla"] = ref_bla if ref_bla else "N/A"
            item["reference_proprietary_name"] = get_val(prod, ['Ref. Product Proprietary Name', 'Reference Product Proprietary Name'], 'N/A')
            item["reference_proper_name"] = get_val(prod, ['Ref. Product Proper Name', 'Reference Product Proper Name'], 'N/A')
            
            # If crawled details are provided, merge them
            if crawled_data and get_val(prod, ['BLA Number']) in crawled_data:
                crawl_item = crawled_data[get_val(prod, ['BLA Number'])]
                item["crawled_details"] = crawl_item.get("metadata", {})
            
            enriched_products.append(item)

        # Write unified CSV (flattening patents)
        csv_output_path = self.output_dir / "purplebook_enriched_products.csv"
        
        fieldnames = [
            "bla_number", "proprietary_name", "proper_name", "license_type", "applicant",
            "licensure", "approval_date", "marketing_status", "dosage_form", "route_of_administration",
            "strength", "product_presentation", "product_number", "center", "date_of_first_licensure",
            "exclusivity_expiration_date", "first_interchangeable_exclusivity_exp_date", 
            "ref_product_exclusivity_exp_date", "orphan_exclusivity_exp_date", "patent_list_provided",
            "product_label_url", "resolved_reference_bla", "reference_proprietary_name", "reference_proper_name",
            "associated_patents_count", "patent_numbers", "patent_expirations"
        ]
        
        # 1. Write patent_list.csv from parsed patents data
        patent_csv_path = self.output_dir / "patent_list.csv"
        if patents:
            with open(patent_csv_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=list(patents[0].keys()))
                writer.writeheader()
                writer.writerows(patents)
                
        # 2. Write glossary.csv from parsed glossary definitions
        glossary_csv_path = self.output_dir / "glossary.csv"
        with open(glossary_csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(["Term", "Definition"])
            for k, v in glossary.items():
                writer.writerow([k, v])

        # 3. Clean up temporary html and js files
        for filename in ["patent_list.html", "glossary.js"]:
            temp_file = self.output_dir / filename
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except Exception:
                    pass

        log.info(f"Unification complete. Only CSV files kept in output directory:")
        log.info(f"  Monthly raw CSV: {self.csv_path}")
        log.info(f"  Patents CSV: {patent_csv_path}")
        log.info(f"  Glossary CSV: {glossary_csv_path}")
        return self.csv_path

# -- Main Entry Point & Orchestration -----------------------------------------
def main():
    parser = argparse.ArgumentParser(description="FDA Purple Book Resumable & Multi-Threaded Data Collector")
    parser.add_argument(
        "-o", "--output-dir",
        type=str,
        default=str(BASE_DIR / "purplebook_data"),
        help="Directory to save collected datasets (default: BASE_DIR/purplebook_data)"
    )
    parser.add_argument(
        "-t", "--threads",
        type=int,
        default=DEFAULT_THREADS,
        help=f"Number of threads for deep crawler (default: {DEFAULT_THREADS})"
    )
    parser.add_argument(
        "-d", "--deep-crawl",
        action="store_true",
        help="Perform deep crawling of individual BLA details pages"
    )
    parser.add_argument(
        "-m", "--max-products",
        type=int,
        default=None,
        help="Limit the number of product pages crawled in deep crawl mode (for testing)"
    )
    parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="Force re-download of bulk files even if they already exist"
    )
    
    args = parser.parse_args()
    
    output_dir = Path(args.output_dir)
    setup_logging(output_dir)
    
    log.info("================================================================")
    log.info("FDA Purple Book Data Collector Initiated")
    log.info(f"Output Directory: {output_dir.resolve()}")
    log.info("================================================================")
    
    start_time = time.time()
    
    try:
        session = build_session()
        
        # 1. Bulk download and crawl phase
        downloader = PurpleBookDownloader(output_dir, session, force_download=args.force)
        csv_text, patent_html, glossary_js = downloader.run()
        
        # 2. Parse phase
        changes, products = PurpleBookParser.parse_monthly_csv(csv_text)
        patents = PurpleBookParser.parse_patent_list(patent_html)
        glossary = PurpleBookParser.parse_glossary_js(glossary_js)
        
        # 3. Optional deep crawl phase
        crawled_data = None
        if args.deep_crawl:
            bla_numbers = sorted(list(set(get_val(p, ['BLA Number']) for p in products if get_val(p, ['BLA Number']))))
            log.info(f"Extracted {len(bla_numbers)} unique BLA numbers for deep crawl.")
            
            crawler = PurpleBookDeepCrawler(
                output_dir=output_dir,
                session=session,
                threads=args.threads,
                max_products=args.max_products
            )
            crawled_data = crawler.run(bla_numbers)
            
        # 4. Data Unification and Enrichment phase
        unifier = PurpleBookDataUnifier(output_dir)
        enriched_path = unifier.unify_data(
            products=products,
            changes=changes,
            patents=patents,
            glossary=glossary,
            crawled_data=crawled_data
        )
        
        elapsed = time.time() - start_time
        log.info("================================================================")
        log.info(f"Data Collection Completed in {elapsed:.2f} seconds.")
        log.info(f"Enriched dataset successfully created: {enriched_path.name}")
        log.info("================================================================")
        
    except Exception as e:
        log.exception(f"Fatal error occurred during execution: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
