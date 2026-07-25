#!/usr/bin/env python3
"""
PubMed MEDLINE Downloader & Crawler
Author: Antigravity AI Coding Assistant
Description: Connects to the official NCBI E-utilities API to search and retrieve 
             biomedical literature metadata. Uses NCBI History Server (WebEnv/query_key) 
             for robust batch pagination, polite rate-limiting, and resuming checkpoints.
"""

import os
import sys
import re
import csv
import json
import time
import argparse
import logging
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Load credentials from .env file
load_dotenv()

# Resolve all paths relative to this script
BASE_DIR = Path(__file__).resolve().parent

# Standard headers to play nice with NCBI servers
HEADERS = {
    "User-Agent": "BiolytInternPubMedDownloader/1.0 (Python; requests; Antigravity)",
}

# Logger setup
logger = logging.getLogger("pubmed_downloader")

# ---------------------------------------------------------------------------
# Global Rate Limiter (shared by ALL worker threads)
# ---------------------------------------------------------------------------
# NCBI E-utilities policy: max 3 requests/second without an API key,
# 10 requests/second with one. This gate enforces a GLOBAL minimum interval
# between outbound requests across every thread, so no matter how many workers
# are running the combined request rate never exceeds the cap. This is the real
# safety mechanism (the thread count is secondary).

class RateLimiter:
    """Thread-safe min-interval gate that caps the COMBINED request rate.

    A single lock serializes the reservation of the next allowed send-time, so
    N threads still emit at most `rate_per_sec` requests/second in aggregate.
    """

    def __init__(self, rate_per_sec: float, jitter_frac: float = 0.15):
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be > 0")
        self.rate_per_sec = rate_per_sec
        self.min_interval = 1.0 / rate_per_sec
        # Jitter only ever ADDS to the interval (never shortens it below the safe
        # minimum), so the true rate stays under the cap while decorrelating our
        # request pattern from NCBI's fixed 1-second counting windows -- this
        # avoids the boundary bursts that otherwise trip an occasional HTTP 429.
        self.jitter_frac = max(0.0, jitter_frac)
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self):
        """Block until this thread is permitted to make one request."""
        interval = self.min_interval
        if self.jitter_frac:
            interval *= (1.0 + random.uniform(0.0, self.jitter_frac))
        with self._lock:
            now = time.monotonic()
            # If the next slot is in the future, we must wait until it.
            wait_for = self._next_allowed - now
            # Reserve the slot for this caller and advance the pointer.
            start = self._next_allowed if wait_for > 0 else now
            self._next_allowed = start + interval
        if wait_for > 0:
            time.sleep(wait_for)


# Module-level limiter shared by every request function / thread. Initialized
# in main() once the API-key presence is known. A conservative default (3 req/s)
# is used until then so accidental early calls stay within the no-key cap.
RATE_LIMITER = RateLimiter(3.0)

def setup_logging(verbose: bool):
    """Configures the logging output level."""
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(str(BASE_DIR / "pubmed_downloader.log"), encoding="utf-8")
        ]
    )

# Note: per-request pacing is now handled globally by the module-level
# RATE_LIMITER (see below), which is shared across all worker threads. The old
# single-threaded polite_sleep() helper has been removed in favor of it.

# ---------------------------------------------------------------------------
# XML Parsing Helpers for EFetch Response
# ---------------------------------------------------------------------------

def parse_pubmed_xml(xml_content: str) -> list:
    """
    Parses NCBI EFetch XML and extracts structured metadata records.
    Returns a list of parsed dictionaries.
    """
    soup = BeautifulSoup(xml_content, "xml")
    articles = soup.find_all("PubmedArticle")
    
    parsed_records = []
    
    for article in articles:
        # 1. PMID (Required ID)
        pmid_el = article.find("PMID")
        if not pmid_el:
            continue
        pmid = pmid_el.get_text().strip()
        
        # 2. DOI & PMCID
        doi = ""
        pmcid = ""
        article_ids = article.find_all("ArticleId")
        for aid in article_ids:
            id_type = aid.get("IdType")
            if id_type == "doi":
                doi = aid.get_text().strip()
            elif id_type == "pmc":
                pmcid = aid.get_text().strip()
                # Ensure pmcid prefix is correct (e.g. PMC12345)
                if pmcid and not pmcid.lower().startswith("pmc"):
                    pmcid = f"PMC{pmcid}"
                    
        # Check ELocationID as fallback for DOI
        if not doi:
            eloc_doi = article.find("ELocationID", EIdType="doi")
            if eloc_doi:
                doi = eloc_doi.get_text().strip()
                
        # 3. Title
        title_el = article.find("ArticleTitle")
        title = " ".join(title_el.get_text().split()) if title_el else ""
        
        # 4. Abstract
        abstract_parts = []
        abstract_el = article.find("Abstract")
        if abstract_el:
            abstract_texts = abstract_el.find_all("AbstractText")
            for text_el in abstract_texts:
                label = text_el.get("Label")
                text = text_el.get_text().strip()
                if text:
                    if label:
                        abstract_parts.append(f"{label}: {text}")
                    else:
                        abstract_parts.append(text)
        abstract = "\n".join(abstract_parts) if abstract_parts else ""
        
        # 5. Authors, Affiliations, ORCIDs
        authors_list = []
        affiliations = set()
        orcids = []
        author_list_el = article.find("AuthorList")
        if author_list_el:
            for author_el in author_list_el.find_all("Author"):
                last_name = author_el.find("LastName")
                fore_name = author_el.find("ForeName")
                initials = author_el.find("Initials")
                collective = author_el.find("CollectiveName")
                
                # Full Name formatting
                name_str = ""
                if last_name:
                    last_val = last_name.get_text().strip()
                    first_val = fore_name.get_text().strip() if fore_name else (initials.get_text().strip() if initials else "")
                    name_str = f"{last_val} {first_val}".strip()
                elif collective:
                    name_str = collective.get_text().strip()
                    
                if name_str:
                    authors_list.append(name_str)
                    
                # Affiliation Details
                for aff_el in author_el.find_all("Affiliation"):
                    aff_text = aff_el.get_text().strip()
                    if aff_text:
                        affiliations.add(aff_text)
                        
                # ORCID Identifiers
                auth_id = author_el.find("Identifier", Source="ORCID")
                if auth_id:
                    orcid_url = auth_id.get_text().strip()
                    # Clean up URL to extract ORCID ID if needed
                    orcid_match = re.search(r"(\d{4}-\d{4}-\d{4}-\d{3}[0-9X])", orcid_url)
                    if orcid_match:
                        orcids.append(orcid_match.group(1))
                        
        authors = "; ".join(authors_list)
        author_affiliations = "; ".join(sorted(list(affiliations)))
        author_orcids = "; ".join(orcids)
        
        # 6. Journal Info
        journal_title = ""
        journal_issn = ""
        journal_volume = ""
        journal_issue = ""
        
        journal_el = article.find("Journal")
        if journal_el:
            jt_el = journal_el.find("Title")
            if jt_el:
                journal_title = jt_el.get_text().strip()
            else:
                j_iso = journal_el.find("ISOAbbreviation")
                if j_iso:
                    journal_title = j_iso.get_text().strip()
                    
            issn_el = journal_el.find("ISSN")
            if issn_el:
                journal_issn = issn_el.get_text().strip()
                
            ji_el = journal_el.find("JournalIssue")
            if ji_el:
                vol_el = ji_el.find("Volume")
                iss_el = ji_el.find("Issue")
                if vol_el: journal_volume = vol_el.get_text().strip()
                if iss_el: journal_issue = iss_el.get_text().strip()
                
        # 7. Publication Date and Year
        pub_year = ""
        pub_date_str = ""
        if journal_el:
            pub_date_el = journal_el.find("PubDate")
            if pub_date_el:
                y = pub_date_el.find("Year")
                m = pub_date_el.find("Month")
                d = pub_date_el.find("Day")
                medline_dt = pub_date_el.find("MedlineDate")
                
                y_val = y.get_text().strip() if y else ""
                m_val = m.get_text().strip() if m else ""
                d_val = d.get_text().strip() if d else ""
                medline_val = medline_dt.get_text().strip() if medline_dt else ""
                
                if y_val:
                    pub_year = y_val
                elif medline_val:
                    # Regex search for 4-digit year in MedlineDate string
                    year_match = re.search(r"\b(19|20)\d{2}\b", medline_val)
                    if year_match:
                        pub_year = year_match.group(0)
                        
                pub_date_str = medline_val if medline_val else f"{y_val}-{m_val}-{d_val}".strip("-")
                
        # 8. Publication Types
        pub_types = []
        pub_types_el = article.find("PublicationTypeList")
        if pub_types_el:
            for pt in pub_types_el.find_all("PublicationType"):
                pt_text = pt.get_text().strip()
                if pt_text:
                    pub_types.append(pt_text)
        pub_type = "; ".join(pub_types)
        
        # 9. Date of Creation / Date Completed
        date_of_creation = ""
        dc_el = article.find("DateCreated") or article.find("DateCompleted")
        if dc_el:
            y = dc_el.find("Year")
            m = dc_el.find("Month")
            d = dc_el.find("Day")
            if y and m and d:
                date_of_creation = f"{y.get_text().strip()}-{m.get_text().strip().zfill(2)}-{d.get_text().strip().zfill(2)}"
                
        # 10. Keywords
        keywords_list = []
        kw_list_el = article.find("KeywordList")
        if kw_list_el:
            for kw in kw_list_el.find_all("Keyword"):
                kw_text = kw.get_text().strip()
                if kw_text:
                    keywords_list.append(kw_text)
        keywords = "; ".join(keywords_list)
        
        # 11. Open Access & Full Text Link
        is_open_access = "Y" if pmcid else "N"
        
        urls = []
        if pmid:
            urls.append(f"[PubMed] https://pubmed.ncbi.nlm.nih.gov/{pmid}/")
        if doi:
            urls.append(f"[DOI] https://doi.org/{doi}")
        if pmcid:
            urls.append(f"[PMC] https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/")
        full_text_urls = "; ".join(urls)
        
        meta_row = {
            "id": pmid,
            "source": "PubMed",
            "pmid": pmid,
            "pmcid": pmcid,
            "doi": doi,
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "author_affiliations": author_affiliations,
            "author_orcids": author_orcids,
            "journal_title": journal_title,
            "journal_issn": journal_issn,
            "journal_volume": journal_volume,
            "journal_issue": journal_issue,
            "pub_year": pub_year,
            "pub_type": pub_type,
            "is_open_access": is_open_access,
            "has_pdf": "N",  # PubMed index doesn't guarantee direct PDF hosting
            "cited_by_count": "",  # EFetch doesn't supply cited-by numbers directly
            "date_of_creation": date_of_creation,
            "first_publication_date": pub_date_str,
            "full_text_urls": full_text_urls,
            "keywords": keywords
        }
        
        parsed_records.append(meta_row)
        
    return parsed_records

# ---------------------------------------------------------------------------
# API Entrez Utilities Requests
# ---------------------------------------------------------------------------

def run_esearch(query: str, api_key: str, email: str) -> dict:
    """
    Submits a search term to Entrez ESearch with history server enabled.
    Returns dictionary with WebEnv, QueryKey, and Count.
    """
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    params = {
        "db": "pubmed",
        "term": query,
        "usehistory": "y",
        "retmode": "json",
        "tool": "BiolytInternPubMedDownloader",
    }
    if api_key:
        params["api_key"] = api_key
    if email:
        params["email"] = email
        
    for attempt in range(5):
        try:
            RATE_LIMITER.acquire()  # global rate gate (shared across threads)
            r = requests.get(url, params=params, headers=HEADERS, timeout=40)
            if r.status_code == 429:
                logger.warning("NCBI API rate limit hit (429). Sleeping 10s...")
                time.sleep(10)
                continue
            r.raise_for_status()
            data = r.json()
            
            esearch_result = data.get("esearchresult", {})
            webenv = esearch_result.get("webenv")
            querykey = esearch_result.get("querykey")
            count = int(esearch_result.get("count", 0))
            
            if not webenv or not querykey:
                raise RuntimeError("ESearch failed to return WebEnv or QueryKey.")
                
            return {
                "webenv": webenv,
                "querykey": querykey,
                "total_count": count
            }
        except Exception as e:
            logger.error(f"ESearch request attempt {attempt+1} failed: {e}")
            if attempt == 4:
                raise e
            time.sleep(2 * (attempt + 1))
            
    raise RuntimeError("Failed to complete ESearch request.")

def run_efetch(webenv: str, querykey: str, start: int, size: int, api_key: str, email: str) -> str:
    """
    Downloads detailed XML records from Entrez EFetch using History server identifiers.
    """
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
    params = {
        "db": "pubmed",
        "WebEnv": webenv,
        "query_key": querykey,
        "retstart": start,
        "retmax": size,
        "retmode": "xml",
        "tool": "BiolytInternPubMedDownloader",
    }
    if api_key:
        params["api_key"] = api_key
    if email:
        params["email"] = email
        
    for attempt in range(5):
        try:
            RATE_LIMITER.acquire()  # global rate gate (shared across threads)
            r = requests.post(url, data=params, headers=HEADERS, timeout=60)
            if r.status_code == 429:
                logger.warning("NCBI API rate limit hit (429). Sleeping 10s...")
                time.sleep(10)
                continue
            r.raise_for_status()
            return r.text
        except Exception as e:
            logger.error(f"EFetch request attempt {attempt+1} failed at retstart={start}: {e}")
            if attempt == 4:
                raise e
            time.sleep(3 * (attempt + 1))
            
    raise RuntimeError(f"Failed to fetch XML at retstart={start}.")

def fetch_and_parse_batch(webenv: str, querykey: str, start: int, size: int,
                          api_key: str, email: str) -> tuple:
    """Worker unit executed inside the thread pool.

    Fetches one EFetch batch (the HTTP call is gated by the global RATE_LIMITER)
    and parses it. Returns (start_offset, [records]) so the caller can reassemble
    batches in their original order. Fetch + XML parse both run in the worker
    thread so CPU parsing overlaps other threads' network waits.
    """
    xml_data = run_efetch(webenv, querykey, start, size, api_key, email)
    records = parse_pubmed_xml(xml_data)
    return start, records

# ---------------------------------------------------------------------------
# Main Crawler Script
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Search PubMed and download detailed article metadata to CSV using official NCBI E-utilities API."
    )
    parser.add_argument(
        "--query",
        type=str,
        default='cancer OR diabetes OR influenza OR malaria OR cardiovascular OR pulmonary OR hypertension OR asthma OR arthritis OR vaccine OR "clinical trial"',
        help="Biomedical/clinical search query for PubMed search."
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(BASE_DIR / "pubmed"),
        help="Output directory to save collected CSVs, logs, and progress checkpoints."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="Maximum total records to download. Set to 0 or negative for unlimited."
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=200,
        help="Batch download page size (1-500). Default is 200."
    )
    parser.add_argument(
        "--delay",
        type=float,
        help="Polite wait delay in seconds between request loops (default: 0.35s without key, 0.1s with key)."
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=None,
        help="Number of concurrent worker threads for batch downloads. "
             "Default: 3 (no API key) or 8 (with API key). Use --threads 1 for "
             "single-threaded behavior. The global rate limiter keeps the "
             "combined request rate within NCBI's cap regardless of this value."
    )
    parser.add_argument(
        "--api-key",
        type=str,
        help="Optional NCBI API key to increase request rate limits up to 10 requests/sec."
    )
    parser.add_argument(
        "--email",
        type=str,
        help="Optional email contact details to attach to Entrez requests."
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore any previous checkpoints and trigger a clean crawl."
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose debug logging."
    )
    
    args = parser.parse_args()
    setup_logging(args.verbose)
    
    # 1. Resolve API Credentials
    api_key = args.api_key or os.getenv("NCBI_API_KEY") or os.environ.get("NCBI_API_KEY")
    email = args.email or os.getenv("NCBI_EMAIL")

    # NCBI cap: 3 req/s without a key, 10 req/s with one. We deliberately target a
    # rate slightly BELOW the nominal cap. Reason: with N worker threads and a
    # min-interval of exactly 1/cap, up to cap+1 requests can fall inside a single
    # rolling one-second window at the boundary, which trips NCBI's strict
    # per-second counter (observed HTTP 429s at exactly 3/s). A safety factor
    # keeps the true windowed rate under the cap. If the user supplies an explicit
    # --delay we honor it verbatim (rate = 1/delay).
    NCBI_SAFETY_FACTOR = 0.75
    if args.delay is not None and args.delay > 0:
        requests_per_sec = 1.0 / args.delay
    else:
        nominal_cap = 10.0 if api_key else 3.0
        requests_per_sec = nominal_cap * NCBI_SAFETY_FACTOR

    # Install the GLOBAL rate limiter shared by every worker thread. This is the
    # real safety mechanism: even with many threads the combined request rate is
    # held at/under the cap.
    global RATE_LIMITER
    RATE_LIMITER = RateLimiter(requests_per_sec)

    # Resolve worker count. Default: 3 without a key, 8 with one. The limiter --
    # not the thread count -- guarantees we stay under NCBI's cap.
    if args.threads is not None:
        num_threads = max(1, args.threads)
    else:
        num_threads = 8 if api_key else 3

    delay = 1.0 / requests_per_sec  # retained for logging / checkpoint parity
    logger.info(
        f"Rate limit: {requests_per_sec:.1f} req/s "
        f"({'API key present' if api_key else 'no API key'}), "
        f"min interval {delay:.3f}s, worker threads: {num_threads}"
    )
    
    # 2. Resolve Paths
    output_path = Path(args.output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    metadata_csv_file = output_path / "pubmed_metadata.csv"
    progress_file = output_path / "pubmed_progress.json"
    
    # 3. Load Progress
    webenv = None
    querykey = None
    retstart = 0
    total_count = None
    
    if not args.fresh and progress_file.exists():
        try:
            with open(progress_file, "r", encoding="utf-8") as pf:
                progress = json.load(pf)
                
            if progress.get("query") == args.query:
                webenv = progress.get("webenv")
                querykey = progress.get("querykey")
                retstart = progress.get("retstart", 0)
                total_count = progress.get("total_count", 0)
                logger.info(f"Resuming progress: retstart={retstart}, total={total_count}, WebEnv={webenv[:20]}...")
            else:
                logger.warning("Search query changed. Starting fresh crawl. Use --fresh to suppress this warning.")
        except Exception as e:
            logger.warning(f"Could not load progress checkpoint: {e}. Starting fresh.")
            
    # 4. Load Duplication Cache from CSV
    completed_ids = set()
    if not args.fresh and metadata_csv_file.exists():
        try:
            with open(metadata_csv_file, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if header:
                    for row in reader:
                        if row:
                            completed_ids.add(row[0])  # PMID is first column
            logger.info(f"Loaded {len(completed_ids):,} existing records for de-duplication.")
        except Exception as e:
            logger.warning(f"Could not load existing CSV for de-duplication: {e}")
            
    # 5. Open/Append CSV File
    metadata_fields = [
        "id", "source", "pmid", "pmcid", "doi", "title", "abstract", "authors", 
        "author_affiliations", "author_orcids", "journal_title", "journal_issn", 
        "journal_volume", "journal_issue", "pub_year", "pub_type", "is_open_access", 
        "has_pdf", "cited_by_count", "date_of_creation", "first_publication_date", 
        "full_text_urls", "keywords"
    ]
    
    csv_exists = metadata_csv_file.exists() and not args.fresh
    csv_mode = "a" if csv_exists else "w"
    
    f_meta = open(metadata_csv_file, csv_mode, newline="", encoding="utf-8")
    meta_writer = csv.DictWriter(f_meta, fieldnames=metadata_fields)
    if not csv_exists:
        meta_writer.writeheader()
        f_meta.flush()
        
    # 6. ESearch Trigger
    if not webenv or not querykey:
        logger.info(f"Submitting query to ESearch: {args.query}")
        search_data = run_esearch(args.query, api_key, email)
        webenv = search_data["webenv"]
        querykey = search_data["querykey"]
        total_count = search_data["total_count"]
        retstart = 0
        logger.info(f"ESearch query succeeded. Total matches in PubMed: {total_count:,}")
        
    # Respect limit
    max_records = total_count
    if args.limit > 0:
        max_records = min(total_count, args.limit)
        logger.info(f"Retrieve limit set to: {max_records:,}")
        
    logger.info("=" * 60)
    logger.info("          STARTING PUBMED METADATA CRAWL")
    logger.info("=" * 60)
    
    # 7. EFetch Pagination (parallel, globally rate-limited)
    #
    # Each EFetch batch at a distinct retstart offset is independent given the
    # shared WebEnv/query_key, so batches are fetched concurrently by the thread
    # pool. The GLOBAL RATE_LIMITER caps combined req/s under NCBI's policy no
    # matter how many workers run. All CSV writing / de-duplication / checkpoint
    # updates happen ONLY in this main thread (workers just fetch + parse and
    # return data), so no CSV/counter lock is needed. Completed batches are
    # persisted strictly in ascending-offset order, which both preserves the
    # original record ordering and keeps the checkpoint's `retstart` equal to the
    # highest CONTIGUOUS offset safely written (crash-safe resume).

    # Build the ordered list of independent batch jobs from the resumed offset.
    batch_jobs = []  # [(offset, size), ...] ascending
    off = retstart
    while off < max_records:
        size = min(args.page_size, max_records - off)
        batch_jobs.append((off, size))
        off += size

    results_by_offset = {}   # offset -> parsed records (filled as futures finish)
    write_state = {"idx": 0, "new_rows": 0}  # next batch index to persist / counter

    def persist_ready():
        """Flush completed batches to CSV in ascending-offset order."""
        while write_state["idx"] < len(batch_jobs):
            w_off, w_size = batch_jobs[write_state["idx"]]
            if w_off not in results_by_offset:
                break  # earlier batch not done yet; preserve order
            records = results_by_offset.pop(w_off)
            new_rows = 0
            for record in records:
                pmid = record["id"]
                if pmid in completed_ids:
                    logger.debug(f"  Skipping duplicate record PMID {pmid}")
                    continue
                meta_writer.writerow(record)
                completed_ids.add(pmid)
                new_rows += 1
            f_meta.flush()
            write_state["new_rows"] += new_rows
            write_state["idx"] += 1
            done_through = w_off + w_size
            logger.info(
                f"  Batch @retstart={w_off} done. Saved {new_rows} new records. "
                f"Progress: {done_through}/{max_records}"
            )
            # Checkpoint = highest contiguous offset written so far.
            with open(progress_file, "w", encoding="utf-8") as pf:
                json.dump({
                    "query": args.query,
                    "webenv": webenv,
                    "querykey": querykey,
                    "retstart": done_through,
                    "total_count": total_count,
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                }, pf, indent=2)

    try:
        if not batch_jobs:
            logger.info("Nothing to fetch (already complete or empty result set).")
        else:
            logger.info(
                f"Scheduled {len(batch_jobs)} batch(es) of up to {args.page_size} "
                f"records across {num_threads} worker thread(s)."
            )
            with ThreadPoolExecutor(max_workers=num_threads) as executor:
                future_to_off = {
                    executor.submit(fetch_and_parse_batch, webenv, querykey,
                                    b_off, b_size, api_key, email): b_off
                    for (b_off, b_size) in batch_jobs
                }
                for future in as_completed(future_to_off):
                    off_val, records = future.result()
                    results_by_offset[off_val] = records
                    persist_ready()  # write any now-contiguous completed batches
            logger.info(
                f"Crawl finished. Total new records saved this run: "
                f"{write_state['new_rows']}."
            )

    except KeyboardInterrupt:
        logger.warning("Harvesting interrupted by user. Progress saved.")
    except Exception as e:
        logger.error(f"Critical error during crawl: {e}", exc_info=True)
    finally:
        f_meta.close()
        logger.info("PubMed MEDLINE crawling session closed.")

if __name__ == "__main__":
    main()
