#!/usr/bin/env python3
"""
ema_downloader.py
=================
A resilient, multi-threaded, and resumable data collector, PDF downloader, and 
clinical trial/prescribing text extractor for European Medicines Agency (EMA) 
centralized approvals (EPARs) and regulatory data.

Features:
  1. Dynamic Schema Mapping: Reads EMA medicines Excel database, cleans headers, 
     and dynamically creates/adapts the SQLite database schema.
  2. Resilient Checkpointing: Keeps track of page crawling, PDF processing, and 
     extraction using a local SQLite database, enabling immediate resumption.
  3. Multi-Threaded Concurrency: Concurrently crawls EPAR pages and extracts text 
     from documents using a thread pool executor.
  4. In-Memory PDF Text Extraction: Uses pypdf to extract plain text directly 
     from streamed PDF bytes without saving PDFs to disk (saving ~20+ GB storage).
  5. Structured LLM API Integration: Uses the MiniMax-M3 LLM API via OpenAI SDK to 
     extract structured clinical fields (composition, indications, posology, contraindications, 
     side effects, efficacy, NCT trials details).
  6. Comprehensive CSV Reports: Automatically exports:
     - Document-level extractions (ema_pdf_extractions.csv)
     - Trial-level clinical registry data (ema_clinical_trials.csv)
     - Consolidated drug-level regulatory summaries (ema_drug_summary.csv)

Requirements:
    pip install requests pandas openpyxl beautifulsoup4 pypdf openai
"""

import os
import sys
import hashlib
import time
import json
import csv
import re
import sqlite3
import logging
import argparse
import threading
import pathlib
import urllib.parse
from pathlib import Path
import concurrent.futures
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from bs4 import BeautifulSoup
import pandas as pd
import io

# PDF library
try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

# OpenAI SDK for MiniMax API
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

# S3 upload removed — the pipeline owns S3 (scrapers write local CSV only).
boto3 = None
BotoCoreError = Exception
ClientError = Exception

# Optional progress bar
try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

# -- Global Constants --------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent

BASE_URL = "https://www.ema.europa.eu"
MEDICINES_EXCEL_URL = "https://www.ema.europa.eu/en/documents/report/medicines-output-medicines-report_en.xlsx"
DOWNLOAD_PAGE_URL = "https://www.ema.europa.eu/en/medicines/download-medicine-data"

DEFAULT_MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY", "")
MINIMAX_BASE_URL = "https://api.minimax.io/v1"

ADDITIONAL_TABLES = {
    "post_authorisation": "https://www.ema.europa.eu/en/documents/report/medicines-output-post_authorisation-report_en.xlsx",
    "orphan_designations": "https://www.ema.europa.eu/en/documents/report/medicines-output-orphan_designations-report_en.xlsx",
    "referrals": "https://www.ema.europa.eu/en/documents/report/medicines-output-referrals-report_en.xlsx",
    "opinions_outside_eu": "https://www.ema.europa.eu/en/documents/report/medicines-output-opinions_outside_eu-report_en.xlsx",
    "paediatric_investigation_plans": "https://www.ema.europa.eu/en/documents/report/medicines-output-paediatric_investigation_plans-report_en.xlsx",
    "herbal_medicines": "https://www.ema.europa.eu/en/documents/report/medicines-output-herbal_medicines-report_en.xlsx",
    "periodic_safety_update_report_single_assessments": "https://www.ema.europa.eu/en/documents/report/medicines-output-periodic_safety_update_report_single_assessments-report_en.xlsx",
    "dhpc": "https://www.ema.europa.eu/en/documents/report/medicines-output-dhpc-report_en.xlsx",
    "shortages": "https://www.ema.europa.eu/en/documents/report/medicines-output-shortages-report_en.xlsx",
    "maximum_residue_limits": "https://www.ema.europa.eu/en/documents/report/medicines-output-maximum_residue_limits-report_en.xlsx"
}

# Logger setup
log = logging.getLogger("ema_downloader")

# -- Helper Functions --------------------------------------------------------
def setup_logging(output_dir: Path):
    """Configures file and console logging."""
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / "ema_downloader.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] (%(threadName)s) %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout)
        ]
    )

def clean_column_name(col_name: str) -> str:
    """Converts Excel column headers into clean, lower_snake_case SQL identifiers."""
    cleaned = re.sub(r'[^a-zA-Z0-9]', '_', str(col_name).lower())
    cleaned = re.sub(r'_+', '_', cleaned)
    return cleaned.strip('_')

def slugify(text: str) -> str:
    """Creates a Windows-safe filename or directory name by stripping illegal characters."""
    if not text:
        return "unnamed"
    cleaned = re.sub(r'[\\/*?:"<>|]', "", str(text))
    cleaned = re.sub(r'\s+', "_", cleaned).strip("_")
    return cleaned[:80]  # Truncate to prevent path length issues

def extract_language_and_type(url: str) -> tuple:
    """Extracts the language code and document type category from an EMA document URL."""
    lang_match = re.search(r'https?://[^/]+/([a-z]{2})/documents/', url)
    if lang_match:
        lang = lang_match.group(1)
    else:
        suffix_match = re.search(r'_([a-z]{2})\.pdf$', url.lower())
        lang = suffix_match.group(1) if suffix_match else 'unknown'
        
    type_match = re.search(r'/documents/([^/]+)/', url)
    doc_type = type_match.group(1) if type_match else 'other'
    
    return lang, doc_type

class SafeRetry(Retry):
    def parse_retry_after(self, retry_after):
        try:
            return super().parse_retry_after(retry_after)
        except Exception:
            try:
                return float(retry_after)
            except ValueError:
                return 1.0

# -- Prompt Builders for LLM -------------------------------------------------
def get_prompt_for_doc_type(doc_type: str) -> str:
    """Builds document-type specific prompts for LLM structured JSON extraction."""
    clinical_trials_spec = """
You must also identify and extract structured information for each clinical trial or study mentioned in the text. Return this in a key "clinical_trials" which must be a list of objects, where each object contains these exact keys (use null for any missing details):
- "nct_id": The registry identifier (e.g. NCT03361748).
- "brief_title": Short title of the study.
- "official_title": Full official scientific title of the study.
- "overall_status": Status of the trial (e.g., active, completed, recruiting, terminated).
- "start_date": Trial start date.
- "sponsor": Lead sponsor / developer.
- "condition": Condition or disease being studied (e.g., Multiple Myeloma).
- "phase": The phase of the trial (e.g., Phase 3, Phase 1/2, Pivotal, Supportive).
- "intervention_name": Name of the drug or therapy (e.g. idecabtagene vicleucel).
- "intervention_type": Type of intervention (e.g. biological, gene therapy, small molecule, vaccine).
- "primary_outcome": Description of the primary efficacy outcomes/endpoints.
- "secondary_outcome": Description of secondary outcomes/endpoints.
- "country": Location or country where the trial was conducted (e.g., multinational, EU, US).
- "study_type": Study type (e.g., interventional, observational, randomized, open-label).
- "enrollment": Number of patients enrolled/randomized.

Also extract:
- "document_procedure_dates": Dates associated with the regulatory procedure, CHMP opinion date, publication date, or version date of this document. Return null if not found.

Make sure the values are precise, complete, and directly extracted or summarized from the text.
Do not include markdown code block formatting (like ```json) in your response. Just return the JSON object."""

    if doc_type == "overview":
        return """You are an expert clinical data extraction assistant.
Your task is to extract relevant clinical and regulatory information from the provided European Medicines Agency (EMA) EPAR Medicine Overview document text.
You must return a valid JSON object with the following keys:
1. "nct_numbers": A list of clinical trial registry IDs (like NCT00000000) found in the text. Return an empty list if none are found.
2. "composition": Qualitative and quantitative composition details of active substance.
3. "therapeutic_indications": What the medicine is used for / approved indications.
4. "posology": How the medicine is used/administered.
5. "contraindications": Contraindications (who should not take it).
6. "undesirable_effects": Side effects and risks.
7. "benefits_in_studies": Efficacy and benefits shown in studies.
8. "benefit_risk_balance": Summary of why the medicine was approved (benefit-risk balance).
""" + clinical_trials_spec

    elif doc_type == "product-information":
        return """You are an expert clinical data extraction assistant.
Your task is to extract relevant prescribing information from the provided Summary of Product Characteristics (SmPC).
You must return a valid JSON object with the following keys:
1. "nct_numbers": A list of clinical trial registry IDs (like NCT00000000) found in the text. Return an empty list if none are found.
2. "composition": Qualitative and quantitative composition details of active substance.
3. "therapeutic_indications": Approved therapeutic indications.
4. "posology": Posology and method of administration.
5. "contraindications": Documented contraindications.
6. "undesirable_effects": Undesirable effects / side effects.
""" + clinical_trials_spec

    elif doc_type == "assessment-report":
        return """You are an expert clinical data extraction assistant.
Your task is to extract relevant clinical efficacy and safety details from the EPAR Assessment Report.
You must return a valid JSON object with the following keys:
1. "nct_numbers": A list of clinical trial registry IDs (like NCT00000000) found in the text. Return an empty list if none are found.
2. "benefits_in_studies": Summary of the clinical trial efficacy results.
3. "benefit_risk_balance": Detailed conclusions on the benefit-risk balance.
""" + clinical_trials_spec

    else:
        return """You are an expert clinical data extraction assistant.
Your task is to extract a brief summary and any clinical trial numbers from this regulatory document.
You must return a valid JSON object with the following keys:
1. "nct_numbers": A list of clinical trial registry IDs (like NCT00000000) found in the text. Return an empty list if none are found.
2. "document_summary": A detailed summary of the content and purpose of this document.
""" + clinical_trials_spec

def clean_and_parse_json(raw_text: str) -> dict:
    """Strips LLM reasoning/thinking blocks and extracts a parseable JSON dictionary."""
    cleaned = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL).strip()
    first_brace = cleaned.find("{")
    last_brace = cleaned.rfind("}")
    if first_brace == -1 or last_brace == -1:
        raise ValueError("Could not find outer JSON braces in the LLM response.")
    json_str = cleaned[first_brace:last_brace+1]
    return json.loads(json_str)

# -- HTTP Session Builder ----------------------------------------------------
def build_session(max_retries: int) -> requests.Session:
    """Builds a requests.Session mimicking a browser with exponential backoff retries."""
    session = requests.Session()
    retry_strategy = SafeRetry(
        total=max_retries,
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
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive"
    })
    return session

# -- Database Manager --------------------------------------------------------
class DatabaseManager:
    """Thread-safe SQLite database manager for checkpointing and progress tracking."""
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.lock = threading.Lock()
        
    def execute(self, query: str, params: tuple = None):
        """Executes a single write query inside a thread lock."""
        with self.lock:
            with sqlite3.connect(self.db_path, timeout=30.0) as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                cursor = conn.cursor()
                if params:
                    cursor.execute(query, params)
                else:
                    cursor.execute(query)
                conn.commit()
                return cursor.fetchall()

    def query(self, query: str, params: tuple = None) -> list:
        """Executes a read query inside a thread lock."""
        with self.lock:
            with sqlite3.connect(self.db_path, timeout=30.0) as conn:
                cursor = conn.cursor()
                if params:
                    cursor.execute(query, params)
                else:
                    cursor.execute(query)
                return cursor.fetchall()

    def initialize_fixed_tables(self):
        """Creates system tables including documents, downloads, and pdf_extractions."""
        self.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                document_url TEXT PRIMARY KEY,
                ema_product_number TEXT,
                medicine_name TEXT,
                document_title TEXT,
                language TEXT,
                document_type TEXT,
                scraped_at TEXT
            );
        """)
        
        self.execute("""
            CREATE TABLE IF NOT EXISTS downloads (
                document_url TEXT PRIMARY KEY,
                local_path TEXT,
                status TEXT DEFAULT 'pending',
                error_message TEXT,
                retries INTEGER DEFAULT 0,
                download_timestamp TEXT,
                file_size INTEGER
            );
        """)

        # PDF extractions table schema
        self.execute("""
            CREATE TABLE IF NOT EXISTS pdf_extractions (
                document_url TEXT PRIMARY KEY,
                ema_product_number TEXT,
                medicine_name TEXT,
                document_type TEXT,
                extraction_status TEXT DEFAULT 'pending',
                extraction_error TEXT,
                extracted_at TEXT,
                nct_numbers TEXT,
                composition TEXT,
                therapeutic_indications TEXT,
                posology TEXT,
                contraindications TEXT,
                undesirable_effects TEXT,
                benefits_in_studies TEXT,
                benefit_risk_balance TEXT,
                document_summary TEXT,
                clinical_trials_json TEXT,
                document_procedure_dates TEXT
            );
        """)
        log.info("Initialized system schema tables.")

    def create_dynamic_medicines_table(self, clean_columns: list):
        """Dynamically creates the medicines table based on Excel columns."""
        fields = []
        for col in clean_columns:
            if col == "ema_product_number":
                fields.append(f"{col} TEXT PRIMARY KEY")
            else:
                fields.append(f"{col} TEXT")
                
        fields.append("scrape_status TEXT DEFAULT 'pending'")
        fields.append("scrape_error TEXT")
        fields.append("scraped_at TEXT")
        
        query = f"CREATE TABLE IF NOT EXISTS medicines ({', '.join(fields)});"
        self.execute(query)
        log.info(f"Dynamically mapped medicines table with {len(clean_columns)} fields.")

    def insert_medicines(self, df, clean_columns: list):
        """Inserts medicines into the database using INSERT OR IGNORE to preserve crawl state."""
        df_clean = df.where(pd.notnull(df), None)
        prod_col = "ema_product_number"
        if prod_col in df_clean.columns:
            df_clean = df_clean[df_clean[prod_col].notna() & (df_clean[prod_col] != "")]
            
        columns_str = ", ".join(clean_columns)
        placeholders = ", ".join(["?"] * len(clean_columns))
        query = f"INSERT OR IGNORE INTO medicines ({columns_str}) VALUES ({placeholders});"
        
        rows = [tuple(r) for r in df_clean.values]
        
        with self.lock:
            with sqlite3.connect(self.db_path, timeout=30.0) as conn:
                cursor = conn.cursor()
                cursor.executemany(query, rows)
                conn.commit()
                
        log.info(f"Successfully inserted/updated {len(rows)} medicine records.")

    def claim_next_medicine(self) -> tuple:
        """Claims a pending medicine record for EPAR page crawling in a thread-safe transaction."""
        with self.lock:
            with sqlite3.connect(self.db_path, timeout=30.0) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT ema_product_number, name_of_medicine, medicine_url 
                    FROM medicines 
                    WHERE scrape_status = 'pending' 
                    LIMIT 1;
                """)
                row = cursor.fetchone()
                if row:
                    prod_num, name, url = row
                    cursor.execute("""
                        UPDATE medicines 
                        SET scrape_status = 'crawling' 
                        WHERE ema_product_number = ?;
                    """, (prod_num,))
                    conn.commit()
                    return prod_num, name, url
                return None

    def update_medicine_status(self, prod_num: str, status: str, error: str = None):
        """Updates the crawling status of a medicine."""
        self.execute("""
            UPDATE medicines 
            SET scrape_status = ?, scrape_error = ?, scraped_at = datetime('now') 
            WHERE ema_product_number = ?;
        """, (status, error, prod_num))

    def insert_documents_and_downloads(self, docs: list, downloads: list):
        """Inserts documents and download queue tasks inside a single transaction."""
        with self.lock:
            with sqlite3.connect(self.db_path, timeout=30.0) as conn:
                cursor = conn.cursor()
                if docs:
                    cursor.executemany("""
                        INSERT OR IGNORE INTO documents 
                        (document_url, ema_product_number, medicine_name, document_title, language, document_type, scraped_at) 
                        VALUES (?, ?, ?, ?, ?, ?, datetime('now'));
                    """, docs)
                    
                if downloads:
                    cursor.executemany("""
                        INSERT OR IGNORE INTO downloads 
                        (document_url, local_path, status) 
                        VALUES (?, ?, 'pending');
                    """, downloads)
                conn.commit()

    def claim_next_download(self) -> tuple:
        """Claims a pending PDF document for processing in a thread-safe transaction."""
        with self.lock:
            with sqlite3.connect(self.db_path, timeout=30.0) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT dl.document_url, dl.local_path, m.medicine_url, d.ema_product_number, d.medicine_name, d.document_type
                    FROM downloads dl
                    JOIN documents d ON dl.document_url = d.document_url
                    JOIN medicines m ON d.ema_product_number = m.ema_product_number
                    WHERE dl.status = 'pending' 
                    LIMIT 1;
                """)
                row = cursor.fetchone()
                if row:
                    url, local_path, referer, prod_num, med_name, doc_type = row
                    cursor.execute("""
                        UPDATE downloads 
                        SET status = 'processing' 
                        WHERE document_url = ?;
                    """, (url,))
                    conn.commit()
                    return url, local_path, referer, prod_num, med_name, doc_type
                return None

    def update_download_status(self, url: str, status: str, size: int = 0, error: str = None):
        """Updates status of a document download/processing task."""
        with self.lock:
            with sqlite3.connect(self.db_path, timeout=30.0) as conn:
                cursor = conn.cursor()
                if status == 'completed':
                    cursor.execute("""
                        UPDATE downloads 
                        SET status = 'completed', file_size = ?, error_message = NULL, download_timestamp = datetime('now') 
                        WHERE document_url = ?;
                    """, (size, url))
                else:
                    cursor.execute("""
                        UPDATE downloads 
                        SET status = 'failed', error_message = ?, retries = retries + 1 
                        WHERE document_url = ?;
                    """, (error, url))
                conn.commit()

    def update_pdf_extraction(self, doc_url: str, prod_num: str, med_name: str, doc_type: str, 
                              status: str, error: str = None, data: dict = None):
        """Thread-safe update of the checkpoint database with LLM extraction results."""
        nct_str = ",".join(data.get("nct_numbers", [])) if data and "nct_numbers" in data else None
        trials_json = json.dumps(data["clinical_trials"]) if data and "clinical_trials" in data else None
            
        def safe_str(val):
            if val is None:
                return None
            if isinstance(val, (dict, list)):
                return json.dumps(val)
            return str(val)

        query = """
        INSERT INTO pdf_extractions (
            document_url, ema_product_number, medicine_name, document_type,
            extraction_status, extraction_error, extracted_at, nct_numbers,
            composition, therapeutic_indications, posology, contraindications,
            undesirable_effects, benefits_in_studies, benefit_risk_balance,
            document_summary, clinical_trials_json, document_procedure_dates
        ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(document_url) DO UPDATE SET
            extraction_status = excluded.extraction_status,
            extraction_error = excluded.extraction_error,
            extracted_at = excluded.extracted_at,
            nct_numbers = excluded.nct_numbers,
            composition = excluded.composition,
            therapeutic_indications = excluded.therapeutic_indications,
            posology = excluded.posology,
            contraindications = excluded.contraindications,
            undesirable_effects = excluded.undesirable_effects,
            benefits_in_studies = excluded.benefits_in_studies,
            benefit_risk_balance = excluded.benefit_risk_balance,
            document_summary = excluded.document_summary,
            clinical_trials_json = excluded.clinical_trials_json,
            document_procedure_dates = excluded.document_procedure_dates;
        """
        
        params = (
            doc_url, prod_num, med_name, doc_type, status, safe_str(error), nct_str,
            safe_str(data.get("composition")) if data else None,
            safe_str(data.get("therapeutic_indications")) if data else None,
            safe_str(data.get("posology")) if data else None,
            safe_str(data.get("contraindications")) if data else None,
            safe_str(data.get("undesirable_effects")) if data else None,
            safe_str(data.get("benefits_in_studies")) if data else None,
            safe_str(data.get("benefit_risk_balance")) if data else None,
            safe_str(data.get("document_summary")) if data else None,
            trials_json,
            safe_str(data.get("document_procedure_dates")) if data else None
        )
        
        self.execute(query, params)

# -- EMA Data Collector ------------------------------------------------------
class EMADataCollector:
    """Handles downloading the master Excel sheet and crawling individual medicine EPAR pages."""
    def __init__(self, db: DatabaseManager, session: requests.Session, output_dir: Path, 
                 languages: list, s3_client=None, s3_bucket=None, s3_prefix=None, s3_direct=False):
        self.db = db
        self.session = session
        self.output_dir = output_dir
        self.languages = languages
        self.s3_client = s3_client
        self.s3_bucket = s3_bucket
        self.s3_prefix = s3_prefix
        self.s3_direct = s3_direct
        self.db.initialize_fixed_tables()

    def download_and_parse_master(self) -> bool:
        """Downloads the master EMA medicines output report Excel and populates SQLite."""
        excel_path = self.output_dir / "medicines_report.xlsx"
        log.info(f"Checking for master Excel database at: {excel_path}")
        
        try:
            log.info(f"Downloading EMA master Excel database from {MEDICINES_EXCEL_URL}...")
            r = self.session.get(MEDICINES_EXCEL_URL, stream=True, timeout=60)
            if r.status_code != 200:
                log.error(f"Failed to download medicines Excel file. HTTP Status: {r.status_code}")
                return False
                
            with open(excel_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            log.info("Downloaded master medicines Excel successfully.")
            
        except Exception as e:
            log.error(f"Error downloading master Excel: {e}")
            return False

        try:
            log.info("Parsing medicines Excel spreadsheet...")
            df = pd.read_excel(excel_path, header=8)
            
            clean_cols = [clean_column_name(c) for c in df.columns]
            df.columns = clean_cols
            
            if 'ema_product_number' not in clean_cols:
                for col in clean_cols:
                    if 'product_number' in col or 'number' in col:
                        df.rename(columns={col: 'ema_product_number'}, inplace=True)
                        break
                        
            if 'name_of_medicine' not in clean_cols:
                for col in clean_cols:
                    if 'name' in col or 'medicine' in col:
                        df.rename(columns={col: 'name_of_medicine'}, inplace=True)
                        break

            clean_cols = list(df.columns)
            self.db.create_dynamic_medicines_table(clean_cols)
            self.db.insert_medicines(df, clean_cols)
            
            csv_path = self.output_dir / "ema_medicines.csv"
            df.to_csv(csv_path, index=False, encoding='utf-8')
            log.info(f"Exported clean medicines metadata to CSV: {csv_path}")
            
            if self.s3_direct and self.s3_client:
                s3_key = f"{self.s3_prefix}/ema_medicines.csv".replace('\\', '/').lstrip('/')
                log.info(f"Uploading ema_medicines.csv to S3: s3://{self.s3_bucket}/{s3_key}")
                self.s3_client.upload_file(str(csv_path), self.s3_bucket, s3_key)
                log.info("Uploaded ema_medicines.csv to S3 successfully.")

            if excel_path.exists():
                excel_path.unlink()
                log.info("Removed temporary local copy of medicines_report.xlsx.")
                
            return True
            
        except Exception as e:
            log.error(f"Error parsing master Excel spreadsheet: {e}")
            return False

    def crawl_medicine_page(self, prod_num: str, name: str, url: str) -> int:
        """Crawls an individual medicine's EPAR page and extracts eligible PDF links."""
        if not url or not isinstance(url, str) or not url.startswith("http"):
            log.warning(f"Invalid or missing EPAR URL for medicine '{name}' (Prod #: {prod_num})")
            self.db.update_medicine_status(prod_num, 'failed', 'Invalid URL')
            return 0
            
        log.info(f"Crawling EPAR page for '{name}': {url}")
        
        try:
            r = self.session.get(url, timeout=30)
            if r.status_code != 200:
                log.warning(f"Failed to fetch EPAR page for {name} (Status: {r.status_code})")
                self.db.update_medicine_status(prod_num, 'failed', f"HTTP Status {r.status_code}")
                return 0
                
            soup = BeautifulSoup(r.text, 'html.parser')
            pdf_links = set()
            
            for a_tag in soup.find_all('a', href=True):
                href = a_tag['href']
                if '.pdf' in href.lower():
                    pdf_links.add((href, a_tag.get_text(strip=True)))
                    
            docs_to_insert = []
            downloads_to_insert = []
            safe_medicine_name = slugify(name)
            
            for href, title in pdf_links:
                absolute_url = urllib.parse.urljoin(url, href)
                lang, doc_type = extract_language_and_type(absolute_url)
                
                if 'all' in self.languages or lang in self.languages:
                    safe_doc_title = slugify(title if title else Path(absolute_url).stem)
                    local_filename = f"{safe_doc_title}_{lang}.pdf"
                    if safe_doc_title.lower() in ['view', 'download', 'pdf', 'link']:
                        url_hash = hashlib.md5(absolute_url.encode('utf-8')).hexdigest()[:8]
                        local_filename = f"document_{url_hash}_{lang}.pdf"
                        
                    local_path = str(Path("pdfs") / safe_medicine_name / local_filename)
                    
                    docs_to_insert.append((absolute_url, prod_num, name, title, lang, doc_type))
                    downloads_to_insert.append((absolute_url, local_path))
                    
            if docs_to_insert:
                self.db.insert_documents_and_downloads(docs_to_insert, downloads_to_insert)
                
            self.db.update_medicine_status(prod_num, 'completed')
            log.info(f"Successfully scraped EPAR page for '{name}'. Found {len(docs_to_insert)} eligible PDF links.")
            return len(docs_to_insert)
            
        except Exception as e:
            log.error(f"Error crawling EPAR page for '{name}': {e}")
            self.db.update_medicine_status(prod_num, 'failed', str(e))
            return 0

    def crawl_all_medicines(self, max_workers: int, limit: int = None):
        """Crawls EPAR pages in parallel using ThreadPoolExecutor."""
        log.info("Starting multi-threaded EPAR page crawling...")
        pending_count = self.db.query("SELECT COUNT(*) FROM medicines WHERE scrape_status = 'pending';")[0][0]
        log.info(f"Found {pending_count} pending medicines to crawl.")
        
        if pending_count == 0:
            log.info("No pending medicines to crawl.")
            return
            
        if limit:
            log.info(f"Limiting crawling to {limit} medicines.")
            
        processed_count = 0
        
        def worker():
            nonlocal processed_count
            while True:
                if limit and processed_count >= limit:
                    break
                item = self.db.claim_next_medicine()
                if not item:
                    break
                prod_num, name, url = item
                self.crawl_medicine_page(prod_num, name, url)
                processed_count += 1
                time.sleep(0.5)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="Crawler") as executor:
            futures = [executor.submit(worker) for _ in range(max_workers)]
            concurrent.futures.wait(futures)
            
        log.info("EPAR page crawling finished.")

# -- In-Memory PDF Processing & LLM Text Extractor ----------------------------
class EMAPDFProcessor:
    """Streams PDFs in-memory, extracts text, and invokes MiniMax-M3 LLM for structured clinical data."""
    def __init__(self, db: DatabaseManager, session: requests.Session, output_dir: Path,
                 api_key: str = None, s3_client=None, s3_bucket=None, s3_prefix=None, s3_direct=False,
                 save_pdfs=False, skip_llm=False):
        self.db = db
        self.session = session
        self.output_dir = output_dir
        self.api_key = api_key or DEFAULT_MINIMAX_API_KEY
        self.s3_client = s3_client
        self.s3_bucket = s3_bucket
        self.s3_prefix = s3_prefix
        self.s3_direct = s3_direct
        self.save_pdfs = save_pdfs
        self.skip_llm = skip_llm

        # No LLM client is created (and no API key is needed) when the raw docs are
        # kept for downstream extraction by the vector store.
        if OpenAI and not skip_llm:
            self.openai_client = OpenAI(api_key=self.api_key, base_url=MINIMAX_BASE_URL)
        else:
            self.openai_client = None

    def process_pdf(self, url: str, rel_path: str, referer: str, prod_num: str, med_name: str, doc_type: str) -> bool:
        """Streams PDF bytes in memory, extracts text with pypdf, calls MiniMax LLM, and updates database."""
        headers = {}
        if referer:
            headers["Referer"] = referer
            
        try:
            r = self.session.get(url, stream=True, timeout=30, headers=headers)
            if r.status_code != 200:
                log.warning(f"Failed to stream PDF {url} (Status: {r.status_code})")
                self.db.update_download_status(url, 'failed', error=f"HTTP Status {r.status_code}")
                return False
                
            pdf_bytes = r.content
            size = len(pdf_bytes)
            
            # Resiliency magic header check
            if not pdf_bytes.startswith(b"%PDF-"):
                error_msg = f"Corrupted file: Magic bytes {pdf_bytes[:5]} do not start with %PDF-"
                log.warning(f"[{med_name}] Validation check failed for {url}: {error_msg}")
                self.db.update_download_status(url, 'failed', error=error_msg)
                return False
                
            # Option to save PDF to disk/S3 if explicitly requested via --save-pdfs
            if self.save_pdfs:
                if self.s3_direct and self.s3_client:
                    s3_key = f"{self.s3_prefix}/{rel_path}".replace('\\', '/').lstrip('/')
                    extra_args = {"ContentType": r.headers.get("Content-Type", "application/pdf")}
                    self.s3_client.upload_fileobj(io.BytesIO(pdf_bytes), self.s3_bucket, s3_key, ExtraArgs=extra_args)
                    log.info(f"Saved raw PDF to S3: s3://{self.s3_bucket}/{s3_key}")
                else:
                    local_path = self.output_dir / rel_path
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(local_path, "wb") as f:
                        f.write(pdf_bytes)
                    log.info(f"Saved raw PDF locally: {rel_path}")

            # Raw-only mode: keep the PDF as-is (the vector store extracts it later),
            # skip all LLM text-extraction. No MiniMax/OpenAI key required.
            if self.skip_llm:
                self.db.update_download_status(url, 'completed', size)
                self.db.update_pdf_extraction(url, prod_num, med_name, doc_type,
                                              'raw_saved', error=None)
                return True

            # 1. Text Extraction using pypdf
            if PdfReader is None:
                log.error("'pypdf' package is not installed. Install with: pip install pypdf")
                self.db.update_download_status(url, 'failed', error="pypdf not installed")
                return False
                
            try:
                reader = PdfReader(io.BytesIO(pdf_bytes))
                total_pages = len(reader.pages)
                
                max_pages = total_pages
                if doc_type == "product-information":
                    max_pages = min(total_pages, 15)
                elif doc_type == "assessment-report":
                    max_pages = min(total_pages, 30)
                elif doc_type not in ["overview"]:
                    max_pages = min(total_pages, 10)
                    
                pdf_text = ""
                for i in range(max_pages):
                    page_text = reader.pages[i].extract_text()
                    if page_text:
                        pdf_text += page_text + "\n"
                        
                pdf_text = pdf_text.strip()
                if not pdf_text:
                    log.warning(f"[{med_name}] Empty PDF text extracted from {url}")
                    self.db.update_download_status(url, 'completed', size)
                    self.db.update_pdf_extraction(url, prod_num, med_name, doc_type, 'empty_text', error="Empty text")
                    return True
                    
            except Exception as e:
                log.warning(f"[{med_name}] PDF text parsing failed for {url}: {e}")
                self.db.update_download_status(url, 'failed', error=f"PDF text parse error: {e}")
                return False

            # 2. Invoke MiniMax-M3 LLM API for structured JSON extraction
            if self.openai_client is None:
                log.error("'openai' SDK not installed. Install with: pip install openai")
                self.db.update_download_status(url, 'completed', size)
                self.db.update_pdf_extraction(url, prod_num, med_name, doc_type, 'failed', error="openai SDK missing")
                return True

            system_prompt = get_prompt_for_doc_type(doc_type)
            user_prompt = f"Document Text:\n{pdf_text}"
            
            api_retries = 3
            api_backoff = 2.0
            extracted_data = None
            
            for attempt in range(api_retries):
                try:
                    response = self.openai_client.chat.completions.create(
                        model="MiniMax-M3",
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt}
                        ],
                        timeout=90.0
                    )
                    raw_reply = response.choices[0].message.content
                    extracted_data = clean_and_parse_json(raw_reply)
                    break
                except Exception as e:
                    log.warning(f"[{med_name}] MiniMax API call attempt {attempt+1} failed: {e}")
                    if attempt < api_retries - 1:
                        time.sleep(api_backoff * (attempt + 1))

            if extracted_data:
                self.db.update_pdf_extraction(url, prod_num, med_name, doc_type, 'completed', data=extracted_data)
                self.db.update_download_status(url, 'completed', size)
                log.info(f"Successfully extracted structured text from [{med_name}] -> {url}")
                return True
            else:
                self.db.update_pdf_extraction(url, prod_num, med_name, doc_type, 'failed', error="LLM API retries exceeded")
                self.db.update_download_status(url, 'completed', size)
                return True

        except Exception as e:
            log.error(f"Error processing PDF {url}: {e}")
            self.db.update_download_status(url, 'failed', error=str(e))
            return False

    def process_all_queued(self, max_workers: int, limit: int = None):
        """Processes queued PDFs in parallel using ThreadPoolExecutor."""
        log.info("Starting multi-threaded PDF text & LLM extractions...")
        pending_count = self.db.query("SELECT COUNT(*) FROM downloads WHERE status = 'pending';")[0][0]
        log.info(f"Found {pending_count} pending documents in queue.")
        
        if pending_count == 0:
            log.info("No pending documents to process.")
            return
            
        if limit:
            log.info(f"Limiting PDF processing to {limit} files.")
            
        processed_count = 0
        pbar = None
        if tqdm:
            total_items = min(pending_count, limit) if limit else pending_count
            pbar = tqdm(total=total_items, desc="Extracting PDFs", unit="doc")
            
        def worker():
            nonlocal processed_count
            while True:
                if limit and processed_count >= limit:
                    break
                item = self.db.claim_next_download()
                if not item:
                    break
                url, local_path, referer, prod_num, med_name, doc_type = item
                success = self.process_pdf(url, local_path, referer, prod_num, med_name, doc_type)
                if success:
                    processed_count += 1
                if pbar:
                    pbar.update(1)
                time.sleep(0.3)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="Extractor") as executor:
            futures = [executor.submit(worker) for _ in range(max_workers)]
            concurrent.futures.wait(futures)
            
        if pbar:
            pbar.close()
        log.info("PDF processing and extractions finished.")

# -- Additional Tables Exporter ----------------------------------------------
def download_all_additional_tables(session: requests.Session, output_dir: Path,
                                   s3_client=None, s3_bucket=None, s3_prefix=None, s3_direct=False):
    """Downloads all 10 additional EMA Excel tables, exports them as CSVs, and optionally uploads to S3."""
    log.info("Starting download of additional regulatory tables...")
    add_dir = output_dir / "additional_tables"
    add_dir.mkdir(parents=True, exist_ok=True)
    
    for name, url in ADDITIONAL_TABLES.items():
        log.info(f"Processing additional table '{name}'...")
        excel_path = add_dir / f"{name}.xlsx"
        csv_path = add_dir / f"{name}.csv"
        
        if s3_direct and s3_client:
            csv_s3_key = f"{s3_prefix}/additional_tables/{name}.csv".replace('\\', '/').lstrip('/')
            try:
                s3_client.head_object(Bucket=s3_bucket, Key=csv_s3_key)
                log.info(f"S3 Object already exists: s3://{s3_bucket}/{csv_s3_key}. Skipping table '{name}'.")
                continue
            except ClientError as e:
                if e.response.get('Error', {}).get('Code') != '404':
                    log.warning(f"Error checking S3 object existence for {csv_s3_key}: {e}")
            except Exception as e:
                log.warning(f"Error checking S3 object existence for {csv_s3_key}: {e}")

        try:
            r = session.get(url, stream=True, timeout=30)
            if r.status_code != 200:
                log.error(f"Failed to download '{name}' table (Status: {r.status_code})")
                continue
                
            with open(excel_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            log.info(f"Downloaded '{name}' Excel table.")
            
            parsed = False
            try:
                df = pd.read_excel(excel_path, header=8)
                df.columns = [clean_column_name(col) for col in df.columns]
                df.to_csv(csv_path, index=False, encoding='utf-8')
                log.info(f"Exported '{name}' to CSV: {csv_path}")
                parsed = True
            except Exception:
                try:
                    df = pd.read_excel(excel_path, header=0)
                    df.columns = [clean_column_name(col) for col in df.columns]
                    df.to_csv(csv_path, index=False, encoding='utf-8')
                    log.info(f"Exported '{name}' to CSV (fallback header=0): {csv_path}")
                    parsed = True
                except Exception as ex_fallback:
                    log.error(f"Could not parse '{name}' Excel table: {ex_fallback}")
            
            if parsed and s3_direct and s3_client:
                csv_s3_key = f"{s3_prefix}/additional_tables/{name}.csv".replace('\\', '/').lstrip('/')
                try:
                    log.info(f"Uploading {name}.csv directly to S3: s3://{s3_bucket}/{csv_s3_key}")
                    s3_client.upload_file(str(csv_path), s3_bucket, csv_s3_key)
                    log.info(f"Successfully uploaded {name}.csv to S3.")
                except Exception as e:
                    log.error(f"Failed to upload {name}.csv to S3: {e}")
                
                if excel_path.exists():
                    excel_path.unlink()
                if csv_path.exists():
                    csv_path.unlink()
                    
        except Exception as e:
            log.error(f"Error downloading/processing '{name}': {e}")
            
    try:
        if s3_direct and add_dir.exists() and not any(add_dir.iterdir()):
            add_dir.rmdir()
            log.info("Removed empty local 'additional_tables' directory.")
    except Exception as e:
        log.warning(f"Could not remove local 'additional_tables' directory: {e}")
            
    log.info("Additional regulatory tables processing complete.")

# -- CSV Extractions & Clinical Reports Exporter ------------------------------
def export_extractions_to_csv(db: DatabaseManager, output_dir: Path):
    """Exports structured document extractions, clinical trials, and drug summary CSVs."""
    log.info("Exporting structured extractions to CSV reports...")
    doc_csv = output_dir / "ema_pdf_extractions.csv"
    trials_csv = output_dir / "ema_clinical_trials.csv"
    summary_csv = output_dir / "ema_drug_summary.csv"

    # 1. Document-level CSV
    try:
        rows = db.query("""
            SELECT document_url, ema_product_number, medicine_name, document_type,
                   extraction_status, extraction_error, extracted_at, nct_numbers,
                   composition, therapeutic_indications, posology, contraindications,
                   undesirable_effects, benefits_in_studies, benefit_risk_balance,
                   document_summary, document_procedure_dates
            FROM pdf_extractions;
        """)
        headers = [
            "document_url", "ema_product_number", "medicine_name", "document_type",
            "extraction_status", "extraction_error", "extracted_at", "nct_numbers",
            "composition", "therapeutic_indications", "posology", "contraindications",
            "undesirable_effects", "benefits_in_studies", "benefit_risk_balance",
            "document_summary", "document_procedure_dates"
        ]
        with open(doc_csv, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            writer.writerows(rows)
        log.info(f"Exported document extractions report ({len(rows)} rows): {doc_csv}")
    except Exception as e:
        log.error(f"Failed to export document extractions CSV: {e}")

    # 2. Trial-level CSV (Exploding clinical_trials JSON array)
    try:
        trial_rows = db.query("""
            SELECT document_url, ema_product_number, medicine_name, clinical_trials_json
            FROM pdf_extractions
            WHERE clinical_trials_json IS NOT NULL AND clinical_trials_json != '';
        """)
        extracted_trials = []
        trial_headers = [
            "document_url", "ema_product_number", "medicine_name", "nct_id", "brief_title",
            "official_title", "overall_status", "start_date", "sponsor", "condition", "phase",
            "intervention_name", "intervention_type", "primary_outcome", "secondary_outcome",
            "country", "study_type", "enrollment"
        ]
        for url, prod_num, med_name, json_str in trial_rows:
            try:
                trials_list = json.loads(json_str)
                if isinstance(trials_list, list):
                    for trial in trials_list:
                        if isinstance(trial, dict):
                            extracted_trials.append([
                                url, prod_num, med_name,
                                trial.get("nct_id"), trial.get("brief_title"), trial.get("official_title"),
                                trial.get("overall_status"), trial.get("start_date"), trial.get("sponsor"),
                                trial.get("condition"), trial.get("phase"), trial.get("intervention_name"),
                                trial.get("intervention_type"), trial.get("primary_outcome"), trial.get("secondary_outcome"),
                                trial.get("country"), trial.get("study_type"), trial.get("enrollment")
                            ])
            except Exception:
                continue

        with open(trials_csv, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(trial_headers)
            writer.writerows(extracted_trials)
        log.info(f"Exported clinical trials report ({len(extracted_trials)} rows): {trials_csv}")
    except Exception as e:
        log.error(f"Failed to export clinical trials CSV: {e}")

    # 3. Drug-level Summary CSV
    try:
        summary_rows = db.query("""
            SELECT ema_product_number, medicine_name,
                   GROUP_CONCAT(DISTINCT nct_numbers) AS nct_numbers,
                   MAX(composition) AS composition,
                   MAX(therapeutic_indications) AS therapeutic_indications,
                   MAX(posology) AS posology,
                   MAX(contraindications) AS contraindications,
                   MAX(undesirable_effects) AS undesirable_effects,
                   MAX(benefits_in_studies) AS benefits_in_studies,
                   MAX(benefit_risk_balance) AS benefit_risk_balance
            FROM pdf_extractions
            WHERE extraction_status = 'completed'
            GROUP BY ema_product_number, medicine_name;
        """)
        summary_headers = [
            "ema_product_number", "medicine_name", "nct_numbers", "composition",
            "therapeutic_indications", "posology", "contraindications",
            "undesirable_effects", "benefits_in_studies", "benefit_risk_balance"
        ]
        with open(summary_csv, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(summary_headers)
            writer.writerows(summary_rows)
        log.info(f"Exported drug-level summary report ({len(summary_rows)} rows): {summary_csv}")
    except Exception as e:
        log.error(f"Failed to export drug summary CSV: {e}")

def export_documents_metadata(db: DatabaseManager, output_dir: Path):
    """Exports scraped documents metadata from SQLite to a clean CSV file."""
    log.info("Exporting documents metadata to CSV...")
    csv_path = output_dir / "ema_documents.csv"
    try:
        rows = db.query("""
            SELECT d.ema_product_number, d.medicine_name, d.document_title, 
                   d.language, d.document_type, d.document_url, dl.status, dl.local_path, dl.file_size
            FROM documents d
            LEFT JOIN downloads dl ON d.document_url = dl.document_url;
        """)
        headers = [
            "ema_product_number", "medicine_name", "document_title", 
            "language", "document_type", "document_url", "download_status", "local_path", "file_size_bytes"
        ]
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            writer.writerows(rows)
        log.info(f"Exported {len(rows)} documents metadata rows to CSV: {csv_path}")
    except Exception as e:
        log.error(f"Failed to export documents metadata: {e}")

# -- Command Line Interface & Main -------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Resilient, multi-threaded collector and in-memory LLM text extractor for EMA approvals (EPARs)."
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(BASE_DIR / "ema_data"),
        help="Directory to save metadata CSVs, SQLite database, and logs (default: BASE_DIR/ema_data)."
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=5,
        help="Maximum concurrent threads for crawling and processing (default: 5)."
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=5,
        help="Maximum HTTP request retries with exponential backoff (default: 5)."
    )
    parser.add_argument(
        "--languages",
        type=str,
        default="en",
        help="Comma-separated list of document language codes to process, or 'all' (default: 'en')."
    )
    parser.add_argument(
        "--skip-pdfs",
        action="store_true",
        help="Only scrape metadata and populate SQLite/CSV; do not process/extract PDF content."
    )
    parser.add_argument(
        "--save-pdfs",
        action="store_true",
        help="Optionally save raw PDF files to local disk/S3 in addition to text extraction."
    )
    parser.add_argument(
        "--skip-llm",
        action="store_true",
        help="Download and keep raw PDFs only; skip MiniMax LLM extraction (no API key needed). "
             "The vector store handles document extraction downstream."
    )
    parser.add_argument(
        "--minimax-api-key",
        type=str,
        default=DEFAULT_MINIMAX_API_KEY,
        help="API Key for MiniMax LLM API."
    )
    parser.add_argument(
        "--limit-medicines",
        type=int,
        default=None,
        help="Limit the number of medicine EPAR pages to crawl (useful for testing)."
    )
    parser.add_argument(
        "--limit-downloads",
        type=int,
        default=None,
        help="Limit the number of PDFs to process/extract (useful for testing)."
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Reset the SQLite database and start fresh (warning: clears progress checkpoints)."
    )
    parser.add_argument(
        "--download-all-tables",
        action="store_true",
        help="Download and export all 10 additional EMA Excel tables (Shortages, Orphans, Referrals, etc.) to CSV."
    )
    args = parser.parse_args()
    
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    setup_logging(output_dir)
    log.info("Starting EMA EPAR Downloader & Text Extractor...")
    
    langs = [l.strip().lower() for l in args.languages.split(",") if l.strip()]
    log.info(f"Target languages configured: {langs}")
    
    db_path = output_dir / "ema_database.db"
    if args.reset and db_path.exists():
        log.warning(f"Reset flag set. Deleting database at {db_path}...")
        try:
            db_path.unlink()
        except Exception as e:
            log.error(f"Could not delete database: {e}")
            
    db = DatabaseManager(db_path)
    session = build_session(args.max_retries)

    # Step 1: Download & Parse Master Excel (writes local CSV; pipeline handles S3)
    collector = EMADataCollector(db, session, output_dir, langs)
    success = collector.download_and_parse_master()
    if not success:
        log.error("Failed to download or parse master medicines Excel. Exiting.")
        sys.exit(1)

    # Optional: Download all additional tables
    if args.download_all_tables:
        download_all_additional_tables(session, output_dir)
        
    # Step 2: Crawl individual EPAR pages for PDF links
    collector.crawl_all_medicines(max_workers=args.threads, limit=args.limit_medicines)
    
    # Step 3: Stream PDFs in memory, extract text, and call MiniMax LLM
    if not args.skip_pdfs:
        processor = EMAPDFProcessor(
            db, session, output_dir,
            api_key=args.minimax_api_key,
            # skip-llm implies keeping the raw PDF, else the run yields no artifacts.
            save_pdfs=args.save_pdfs or args.skip_llm,
            skip_llm=args.skip_llm
        )
        processor.process_all_queued(max_workers=args.threads, limit=args.limit_downloads)
    else:
        log.info("Skipping PDF processing as --skip-pdfs was specified.")
        
    # Step 4: Export metadata & extractions to CSV reports (local; pipeline uploads)
    export_documents_metadata(db, output_dir)
    export_extractions_to_csv(db, output_dir)

    log.info("EMA Downloader & Extractor process completed successfully.")

if __name__ == "__main__":
    main()
