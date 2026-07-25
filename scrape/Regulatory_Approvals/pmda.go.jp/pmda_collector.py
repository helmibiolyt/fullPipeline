#!/usr/bin/env python3
"""
pmda_collector.py
=================
A resilient, multi-threaded, and resumable data collector and LLM information extractor
for Japan's PMDA (Pharmaceuticals and Medical Devices Agency) approvals and review reports.

Features:
  1. Dynamic Web Scraping: Scrapes approval tables across Drugs, Devices,
     Regenerative Products, and Quasi-Drugs from PMDA English portals.
  2. In-Memory PDF Text Extraction: Streams PDF review reports into memory and
     extracts clean text using PyPDF without forcing local file storage.
  3. Multi-LLM Concurrent Pipeline: Dispatches extraction tasks across 3 LLM providers
     (MiniMax, Gemini, Groq) with automatic failover, retry logic, and thread safety.
  4. Rich Structured CSV Output: Extracts key clinical, dosage, indication, efficacy,
     safety, and trial metadata into a unified CSV (`pmda_llm_extracted_data.csv`).
  5. Checkpoint & Resumption: Saves intermediate progress to support interruption recovery.

Requirements:
    pip install requests beautifulsoup4 pypdf
"""

import os
import re
import sys
import io
import time
import json
import csv
import logging
import argparse
import hashlib
import threading
from pathlib import Path
from urllib.parse import urljoin
import concurrent.futures

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from bs4 import BeautifulSoup

try:
    import pypdf
except ImportError:
    pypdf = None

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

# -- Environment & API Configuration -----------------------------------------
def load_dotenv():
    """Loads environment variables from local .env file if available."""
    env_paths = [
        Path(".env"),
        Path(__file__).parent / ".env",
        Path("c:/Users/LeMonde/Desktop/Biolyt_Inter/Biolyt_data_collection/.env")
    ]
    for env_path in env_paths:
        if env_path.exists():
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            os.environ.setdefault(k.strip(), v.strip())
                break
            except Exception:
                pass

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

BASE_URL = "https://www.pmda.go.jp"

PORTALS = {
    "drug": "https://www.pmda.go.jp/english/review-services/reviews/approved-information/drugs/0001.html",
    "device": "https://www.pmda.go.jp/english/review-services/reviews/approved-information/devices/0003.html",
    "regenerative": "https://www.pmda.go.jp/english/review-services/reviews/approved-information/0004.html",
    "quasi_drug": "https://www.pmda.go.jp/english/review-services/reviews/approved-information/0005.html"
}

MONTH_MAP = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "jun": "06",
    "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12"
}

# CSV output schema fields
CSV_FIELDS = [
    "id",
    "category",
    "brand_name",
    "generic_name",
    "approval_date",
    "approval_date_raw",
    "approval_type",
    "applicant_company",
    "indication_approved_usage",
    "dosage_and_administration",
    "key_efficacy_results",
    "key_safety_adverse_events",
    "clinical_trial_identifiers",
    "approval_history_notes",
    "source_pdf_url",
    "extraction_llm_provider"
]

checkpoint_lock = threading.Lock()
csv_lock = threading.Lock()
llm_rr_lock = threading.Lock()
llm_rr_counter = 0

log = logging.getLogger("pmda_collector")

def setup_logging(output_dir: Path):
    """Sets up file and console logging."""
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / "pmda_collector.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] (%(threadName)s) %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout)
        ]
    )

def build_session(max_retries: int) -> requests.Session:
    """Builds requests Session with retries."""
    session = requests.Session()
    retry_strategy = Retry(
        total=max_retries,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": "BiolytInternPMDACollector/2.0 (Python; requests; BeautifulSoup)"
    })
    return session

def parse_approval_date(date_str: str) -> str:
    """Parses date string like 'September 2019' into 'YYYY-MM'."""
    if not date_str:
        return "unknown"
    cleaned = date_str.strip().lower()
    year = None
    for w in cleaned.split():
        w_clean = "".join(c for c in w if c.isdigit())
        if len(w_clean) == 4:
            year = w_clean
            break
    if not year:
        return "unknown"
    month_num = "00"
    for m_name, m_num in MONTH_MAP.items():
        if m_name in cleaned:
            month_num = m_num
            break
    return f"{year}-{month_num}"

# -- Scraping Logic ----------------------------------------------------------
def scrape_portal(category: str, url: str, session: requests.Session) -> list:
    """Scrapes PMDA portal for product approval records and English PDF review report URLs."""
    log.info(f"Scraping {category.upper()} portal: {url}")
    records = []
    try:
        r = session.get(url, timeout=25)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')
        tables = soup.find_all('table', class_='normal-table')
        if not tables:
            log.warning(f"No normal-table elements found on {category} portal.")
            return records

        for table in tables:
            rows = table.find_all('tr')
            if not rows:
                continue

            first_row_links = table.find_all('a')
            if first_row_links and all(a.get('href', '').startswith('#') for a in first_row_links):
                continue

            header_row = rows[0]
            headers = [th.get_text(strip=True).lower() for th in header_row.find_all(['th', 'td'])]

            brand_idx = -1
            generic_idx = -1
            date_idx = -1
            en_idx = -1

            for idx, h in enumerate(headers):
                if "brand" in h:
                    brand_idx = idx
                elif "non-proprietary" in h or "term name" in h or "generic" in h:
                    generic_idx = idx
                elif "approved in" in h or "approved date" in h:
                    date_idx = idx
                elif "english" in h or "report (en)" in h:
                    en_idx = idx

            if brand_idx == -1 and len(headers) >= 4:
                brand_idx, generic_idx, date_idx, en_idx = 0, 1, 2, 3

            if brand_idx == -1:
                continue

            for row in rows[1:]:
                cols = row.find_all(['td', 'th'])
                if len(cols) <= max(brand_idx, generic_idx, date_idx):
                    continue

                brand_cell = cols[brand_idx]
                superscript = brand_cell.find('sup')
                approval_type = "Initial Approval"
                if superscript:
                    sup_text = superscript.get_text(strip=True)
                    if "change" in sup_text.lower() or "partial" in sup_text.lower():
                        approval_type = "Partial Change Approval"
                    superscript.extract()

                brand_name = brand_cell.get_text(" ", strip=True)
                if not brand_name or brand_name.lower() == "brand name":
                    continue

                generic_name = "-"
                if generic_idx != -1 and generic_idx < len(cols):
                    generic_name = cols[generic_idx].get_text(strip=True)

                approval_date_raw = "unknown"
                if date_idx != -1 and date_idx < len(cols):
                    approval_date_raw = cols[date_idx].get_text(strip=True)
                approval_date = parse_approval_date(approval_date_raw)

                en_urls = []
                if en_idx != -1 and en_idx < len(cols):
                    en_links = cols[en_idx].find_all('a')
                    for a in en_links:
                        href = a.get('href')
                        if href and href.lower().endswith('.pdf'):
                            en_urls.append(urljoin(BASE_URL, href))

                records.append({
                    "category": category,
                    "brand_name": brand_name,
                    "approval_type": approval_type,
                    "generic_name": generic_name,
                    "approval_date_raw": approval_date_raw,
                    "approval_date": approval_date,
                    "en_urls": en_urls
                })

        log.info(f"Scraped {len(records)} product records from {category} portal.")
    except Exception as e:
        log.error(f"Error scraping {category} portal: {e}")
    return records

# -- PDF Text Extraction -----------------------------------------------------
def extract_pdf_text_from_url(url: str, session: requests.Session, timeout: int = 25, max_pages: int = 15) -> str:
    """Streams a PDF from URL into memory and extracts text using PyPDF."""
    if not pypdf:
        log.error("pypdf library is not installed.")
        return ""
    try:
        r = session.get(url, stream=True, timeout=timeout)
        r.raise_for_status()
        pdf_bytes = io.BytesIO(r.content)

        reader = pypdf.PdfReader(pdf_bytes)
        num_pages = min(len(reader.pages), max_pages)

        text_pages = []
        for i in range(num_pages):
            page_text = reader.pages[i].extract_text()
            if page_text:
                text_pages.append(page_text)

        full_text = "\n".join(text_pages).strip()
        # Truncate to ~12,000 characters to fit LLM context efficiently
        if len(full_text) > 12000:
            full_text = full_text[:12000] + "\n... [TRUNCATED] ..."
        return full_text
    except Exception as e:
        log.warning(f"Could not extract PDF text from {url}: {e}")
        return ""

# -- LLM Multi-Provider Execution Engine -------------------------------------
def call_minimax(prompt: str) -> tuple[str, str]:
    """Calls MiniMax API."""
    api_key = os.getenv("MINIMAX_API_KEY")
    base_url = os.getenv("MINIMAX_BASE_URL", "https://api.minimax.io/v1")
    if not api_key:
        raise ValueError("Missing MINIMAX_API_KEY")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    url = f"{base_url}/chat/completions"
    payload = {
        "model": "MiniMax-Text-01",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1
    }
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    if r.status_code == 200:
        content = r.json()['choices'][0]['message']['content']
        return content, "minimax"
    else:
        raise RuntimeError(f"MiniMax HTTP {r.status_code}: {r.text[:200]}")

def call_gemini(prompt: str) -> tuple[str, str]:
    """Calls Google Gemini API."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("Missing GEMINI_API_KEY")
    target_model = "gemini-2.0-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{target_model}:generateContent?key={api_key}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1}
    }
    r = requests.post(url, json=payload, timeout=30)
    if r.status_code == 200:
        content = r.json()['candidates'][0]['content']['parts'][0]['text']
        return content, "gemini"
    else:
        raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:200]}")

def call_groq(prompt: str) -> tuple[str, str]:
    """Calls Groq API."""
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise ValueError("Missing GROQ_API_KEY")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    url = "https://api.groq.com/openai/v1/chat/completions"
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1
    }
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    if r.status_code == 200:
        content = r.json()['choices'][0]['message']['content']
        return content, "groq"
    else:
        raise RuntimeError(f"Groq HTTP {r.status_code}: {r.text[:200]}")

def execute_llM_extraction(pdf_text: str, record_meta: dict) -> tuple[dict, str]:
    """Dispatches extraction request across LLMs (MiniMax, Gemini, Groq) with automatic failover."""
    global llm_rr_counter
    
    prompt = f"""
You are an expert regulatory affairs & clinical pharmacology AI assistant analyzing Japan PMDA (Pharmaceuticals and Medical Devices Agency) approval review reports.

Source Metadata:
- Product Brand Name: {record_meta.get('brand_name')}
- Generic Name: {record_meta.get('generic_name')}
- Category: {record_meta.get('category')}
- Approval Date (Raw): {record_meta.get('approval_date_raw')}

Extracted PDF Document Text:
\"\"\"
{pdf_text if pdf_text else '[No PDF text available. Extract based on provided metadata.]'}
\"\"\"

Please extract and normalize the following structured fields in valid JSON format.
Ensure dates are strictly in YYYY-MM-DD or YYYY-MM format if available.
Do not invent information. If a field is not mentioned, set its value to "N/A".

Required JSON Output Format:
{{
    "brand_name": "{record_meta.get('brand_name')}",
    "generic_name": "{record_meta.get('generic_name')}",
    "category": "{record_meta.get('category')}",
    "approval_date": "YYYY-MM or YYYY-MM-DD",
    "approval_type": "{record_meta.get('approval_type')}",
    "applicant_company": "Name of applicant / marketing authorization holder company",
    "indication_approved_usage": "Target medical condition or indication approved",
    "dosage_and_administration": "Approved dosage regime or clinical administration route",
    "key_efficacy_results": "Summary of primary endpoints or key trial efficacy findings",
    "key_safety_adverse_events": "Summary of key safety profile or notable adverse events",
    "clinical_trial_identifiers": "Trial registry IDs (e.g. NCTxxxx, jRCTxxxx) if mentioned",
    "approval_history_notes": "Brief summary of PMDA evaluation notes or prior approval history"
}}
Return ONLY the raw JSON string without markdown code block formatting.
"""

    providers = [call_minimax, call_gemini, call_groq]

    with llm_rr_lock:
        start_idx = llm_rr_counter % len(providers)
        llm_rr_counter += 1

    # Try providers in round-robin sequence with failover
    last_error = None
    for attempt in range(len(providers)):
        provider_fn = providers[(start_idx + attempt) % len(providers)]
        try:
            raw_response, provider_name = provider_fn(prompt)
            # Parse JSON
            cleaned = raw_response.strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            if cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()

            match = re.search(r'\{.*\}', cleaned, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                return data, provider_name
        except Exception as e:
            last_error = e
            log.warning(f"Provider {provider_fn.__name__} attempt failed: {e}. Trying next provider...")

    log.error(f"All LLM providers failed for {record_meta.get('brand_name')}: {last_error}")
    # Return fallback basic structure on error
    fallback = {
        "brand_name": record_meta.get("brand_name", "N/A"),
        "generic_name": record_meta.get("generic_name", "N/A"),
        "category": record_meta.get("category", "N/A"),
        "approval_date": record_meta.get("approval_date", "N/A"),
        "approval_type": record_meta.get("approval_type", "N/A"),
        "applicant_company": "N/A",
        "indication_approved_usage": "N/A",
        "dosage_and_administration": "N/A",
        "key_efficacy_results": "N/A",
        "key_safety_adverse_events": "N/A",
        "clinical_trial_identifiers": "N/A",
        "approval_history_notes": "N/A"
    }
    return fallback, "fallback"

# -- Metadata & CSV Exporters -------------------------------------------------
def init_csv_file(csv_path: Path):
    """Initializes CSV header if file does not exist."""
    if not csv_path.exists():
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            writer.writeheader()

def append_csv_record(csv_path: Path, record: dict):
    """Thread-safe append of a record to CSV."""
    with csv_lock:
        with open(csv_path, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            row = {field: record.get(field, "N/A") for field in CSV_FIELDS}
            writer.writerow(row)

# -- Main Execution Pipeline -------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Resumable and multi-threaded PMDA Japan approvals and review reports collector with LLM PDF data extraction."
    )
    parser.add_argument(
        "--output-dir", default=str(BASE_DIR / "pmda_data"), help="Directory to save output files and metadata."
    )
    parser.add_argument(
        "--threads", type=int, default=5, help="Number of concurrent processing threads."
    )
    parser.add_argument(
        "--max-retries", type=int, default=3, help="Maximum HTTP retries."
    )
    parser.add_argument(
        "--timeout", type=int, default=25, help="Timeout in seconds for HTTP requests."
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Limit number of product approvals to process."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Scrape portal records without invoking LLMs or downloading PDFs."
    )
    parser.add_argument(
        "--skip-llm", action="store_true",
        help="Download and keep raw PDFs + a metadata CSV; skip LLM extraction (no API key needed). "
             "The vector store handles document extraction downstream."
    )

    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    setup_logging(output_dir)

    log.info("Starting PMDA Collector & LLM Information Extraction Pipeline...")
    session = build_session(args.max_retries)

    # Phase 1: Scrape Portals
    all_records = []
    for category, portal_url in PORTALS.items():
        records = scrape_portal(category, portal_url, session)
        all_records.extend(records)

    log.info(f"Total PMDA product records scraped across categories: {len(all_records)}")

    # Add unique IDs
    for r in all_records:
        hash_input = f"{r['category']}_{r['brand_name']}_{r['approval_date']}"
        r["id"] = hashlib.md5(hash_input.encode('utf-8')).hexdigest()[:8]

    if args.limit:
        log.info(f"Limiting execution to first {args.limit} records.")
        all_records = all_records[:args.limit]

    if args.dry_run:
        log.info("Dry-run active. Exporting raw scraped metadata...")
        dry_csv = output_dir / "metadata_raw.csv"
        with open(dry_csv, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=list(all_records[0].keys()))
            writer.writeheader()
            writer.writerows(all_records)
        log.info(f"Dry-run completed. Raw metadata saved to {dry_csv}")
        return

    # Phase 2 (raw mode): download PDFs to disk + write metadata CSV, no LLM/API key.
    # The vector store extracts the raw PDFs downstream.
    if args.skip_llm:
        pdfs_dir = output_dir / "pdfs"
        pdfs_dir.mkdir(parents=True, exist_ok=True)
        meta_csv_path = output_dir / "pmda_metadata.csv"
        meta_rows = []

        raw_pbar = tqdm(total=len(all_records), desc="Downloading PMDA PDFs", unit="pdf") if tqdm else None

        def raw_worker(rec):
            pdf_url = rec["en_urls"][0] if rec.get("en_urls") else ""
            local_pdf = ""
            if pdf_url:
                try:
                    r = session.get(pdf_url, stream=True, timeout=args.timeout)
                    r.raise_for_status()
                    pdf_bytes = r.content
                    if pdf_bytes.startswith(b"%PDF-"):
                        fname = f"{rec['id']}.pdf"
                        with open(pdfs_dir / fname, "wb") as f:
                            f.write(pdf_bytes)
                        local_pdf = f"pdfs/{fname}"
                    else:
                        log.warning(f"[{rec.get('brand_name','?')}] not a PDF: {pdf_url}")
                except Exception as e:
                    log.warning(f"Failed to download PDF {pdf_url}: {e}")

            row = {
                "id": rec["id"],
                "category": rec.get("category", ""),
                "brand_name": rec.get("brand_name", ""),
                "generic_name": rec.get("generic_name", ""),
                "approval_type": rec.get("approval_type", ""),
                "approval_date": rec.get("approval_date", ""),
                "approval_date_raw": rec.get("approval_date_raw", ""),
                "source_pdf_url": pdf_url,
                "local_pdf_path": local_pdf,
            }
            with csv_lock:
                meta_rows.append(row)
            if raw_pbar:
                raw_pbar.update(1)

        log.info(f"Launching raw-download pool with {args.threads} threads...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.threads, thread_name_prefix="RawWorker") as executor:
            futures = [executor.submit(raw_worker, rec) for rec in all_records]
            concurrent.futures.wait(futures)
        if raw_pbar:
            raw_pbar.close()

        if meta_rows:
            with open(meta_csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=list(meta_rows[0].keys()))
                writer.writeheader()
                writer.writerows(meta_rows)
        saved = sum(1 for row in meta_rows if row["local_pdf_path"])
        log.info(f"Raw mode complete: {saved} PDFs saved to {pdfs_dir}, metadata -> {meta_csv_path}")
        return

    # Phase 2: Process Records with LLM Information Extraction
    csv_output_path = output_dir / "pmda_llm_extracted_data.csv"
    json_output_path = output_dir / "pmda_llm_extracted_data.json"
    init_csv_file(csv_output_path)

    extracted_results = []
    success_count = 0
    fail_count = 0

    pbar = None
    if tqdm:
        pbar = tqdm(total=len(all_records), desc="Processing & Extracting with LLMs", unit="product")

    def worker(rec):
        nonlocal success_count, fail_count
        pdf_url = rec["en_urls"][0] if rec.get("en_urls") else ""
        pdf_text = ""
        if pdf_url:
            pdf_text = extract_pdf_text_from_url(pdf_url, session, timeout=args.timeout)

        llm_data, provider_used = execute_llM_extraction(pdf_text, rec)

        merged_record = {
            "id": rec["id"],
            "category": rec["category"],
            "brand_name": llm_data.get("brand_name", rec["brand_name"]),
            "generic_name": llm_data.get("generic_name", rec["generic_name"]),
            "approval_date": llm_data.get("approval_date", rec["approval_date"]),
            "approval_date_raw": rec.get("approval_date_raw", "N/A"),
            "approval_type": llm_data.get("approval_type", rec["approval_type"]),
            "applicant_company": llm_data.get("applicant_company", "N/A"),
            "indication_approved_usage": llm_data.get("indication_approved_usage", "N/A"),
            "dosage_and_administration": llm_data.get("dosage_and_administration", "N/A"),
            "key_efficacy_results": llm_data.get("key_efficacy_results", "N/A"),
            "key_safety_adverse_events": llm_data.get("key_safety_adverse_events", "N/A"),
            "clinical_trial_identifiers": llm_data.get("clinical_trial_identifiers", "N/A"),
            "approval_history_notes": llm_data.get("approval_history_notes", "N/A"),
            "source_pdf_url": pdf_url if pdf_url else "N/A",
            "extraction_llm_provider": provider_used
        }

        append_csv_record(csv_output_path, merged_record)
        with csv_lock:
            extracted_results.append(merged_record)
            if provider_used != "fallback":
                success_count += 1
            else:
                fail_count += 1

        if pbar:
            pbar.update(1)

    log.info(f"Launching LLM processing pool with {args.threads} threads...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.threads, thread_name_prefix="LLMWorker") as executor:
        futures = [executor.submit(worker, rec) for rec in all_records]
        concurrent.futures.wait(futures)

    if pbar:
        pbar.close()

    # Clean up intermediate JSON files
    try:
        if json_output_path.exists():
            json_output_path.unlink()
            log.info(f"Cleaned up intermediate JSON file: {json_output_path.name}")
        meta_json = output_dir / "metadata.json"
        if meta_json.exists():
            meta_json.unlink()
            log.info(f"Cleaned up metadata JSON file: {meta_json.name}")
        checkpoint_json = output_dir / "checkpoint.json"
        if checkpoint_json.exists():
            checkpoint_json.unlink()
            log.info(f"Cleaned up checkpoint JSON file: {checkpoint_json.name}")
    except Exception as e:
        log.warning(f"Could not delete intermediate JSON files: {e}")

    log.info("==================================================")
    log.info("         PMDA LLM Extraction Pipeline Summary")
    log.info("==================================================")
    log.info(f"Total product records processed: {len(all_records)}")
    log.info(f"Successful LLM Extractions: {success_count}")
    log.info(f"Fallbacks / Errors: {fail_count}")
    log.info(f"Output CSV saved to: {csv_output_path.resolve()}")
    log.info("Pipeline completed successfully.")

if __name__ == "__main__":
    main()
