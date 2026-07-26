#!/usr/bin/env python3
"""
OpenAlex Metadata Downloader & Harvester
Author: Antigravity AI Coding Assistant
Description: Extracts scholarly metadata from the OpenAlex API (focusing on Health Sciences/Medicine),
             reconstructs abstracts from inverted indexes, parses MeSH terms and authorships,
             and exports findings to structured flat or relational CSVs.
             Includes cursor-based resume capabilities, API key rotation, and parallel partitioning.
"""

import os
import sys
import json
import time
import csv
import argparse
import logging
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from dotenv import load_dotenv

# Load variables from .env
load_dotenv()

# Resolve all paths relative to this script, never the current working directory.
BASE_DIR = Path(__file__).resolve().parent

# Logger setup
logger = logging.getLogger("openalex_downloader")

def setup_logging(verbose: bool, log_file: Path):
    """Configures the logging output level and destination."""
    log_level = logging.DEBUG if verbose else logging.INFO
    
    # Ensure parent dir exists
    log_file.parent.mkdir(parents=True, exist_ok=True)
    
    # Configure root logger
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] (%(threadName)s) %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8")
        ]
    )

# ---------------------------------------------------------------------------
# API Key Rotation Manager
# ---------------------------------------------------------------------------

class APIKeyRotator:
    """Manages thread-safe rotation of multiple OpenAlex API keys."""
    def __init__(self, keys: list):
        self.keys = [k.strip() for k in keys if k.strip()]
        self.current_idx = 0
        self.lock = threading.Lock()
        
    def get_key(self) -> str:
        with self.lock:
            if not self.keys:
                return None
            return self.keys[self.current_idx]
            
    def rotate_key(self, failed_key: str) -> str:
        with self.lock:
            if not self.keys:
                return None
            # Only rotate if the key that failed is the currently active key
            # (avoids multiple concurrent threads rotating the key multiple times)
            if self.keys[self.current_idx] == failed_key:
                old_key = self.keys[self.current_idx]
                self.current_idx = (self.current_idx + 1) % len(self.keys)
                new_key = self.keys[self.current_idx]
                logger.warning(
                    f"API Key Exhausted! Rotated from key '{old_key[:5]}...' "
                    f"to key '{new_key[:5]}...' (Index {self.current_idx + 1}/{len(self.keys)})."
                )
            return self.keys[self.current_idx]
            
    def get_all_keys(self) -> list:
        return self.keys

# ---------------------------------------------------------------------------
# Abstract Reconstruction & Work Parsing
# ---------------------------------------------------------------------------

def reconstruct_abstract(inverted_index: dict) -> str:
    """
    Reconstructs the plaintext abstract from OpenAlex's abstract_inverted_index.
    """
    if not inverted_index or not isinstance(inverted_index, dict):
        return ""
    try:
        # Find the maximum position index to allocate the word list
        max_pos = -1
        for positions in inverted_index.values():
            if positions:
                max_pos = max(max_pos, max(positions))
        
        if max_pos < 0:
            return ""
        
        # Build array of words at their respective positions
        words = [None] * (max_pos + 1)
        for word, positions in inverted_index.items():
            for pos in positions:
                if 0 <= pos <= max_pos:
                    words[pos] = word
        
        # Join words, filtering out any missing/None positions
        cleaned_words = [w if w is not None else "" for w in words]
        return " ".join(cleaned_words).strip()
    except Exception as e:
        logger.debug(f"Failed to reconstruct abstract: {e}")
        return ""

def parse_work(work: dict) -> dict:
    """
    Parses a single work JSON object into flat metadata and relational schemas.
    """
    work_id = work.get("id", "")
    doi = work.get("doi", "") or ""
    title = work.get("title", "") or ""
    publication_year = work.get("publication_year")
    publication_date = work.get("publication_date", "") or ""
    type_ = work.get("type", "") or ""
    language = work.get("language", "") or ""
    
    # Source / Journal details
    primary_loc = work.get("primary_location", {}) or {}
    source = primary_loc.get("source", {}) or {}
    source_id = source.get("id", "") or ""
    journal_name = source.get("display_name", "") or ""
    publisher = source.get("host_organization_name", "") or ""
    issn_list = source.get("issn", []) or []
    issn = issn_list[0] if issn_list else source.get("issn_l", "") or ""
    
    # Open Access
    oa = work.get("open_access", {}) or {}
    is_oa = oa.get("is_oa", False)
    oa_status = oa.get("oa_status", "") or ""
    oa_url = oa.get("oa_url", "") or ""
    
    # Authors and Institutions parsing
    authors_list = []
    institutions_list = []
    countries_list = []
    
    rel_authors = []
    rel_institutions = []
    rel_affiliations = []
    
    first_author_inst = ""
    
    for idx, auth_data in enumerate(work.get("authorships", []) or []):
        author = auth_data.get("author", {}) or {}
        author_name = author.get("display_name", "") or ""
        author_id = author.get("id", "") or ""
        orcid = author.get("orcid", "") or ""
        author_pos = auth_data.get("author_position", "") or ""
        
        if author_name:
            authors_list.append(author_name)
            
        if author_id:
            rel_authors.append({
                "author_id": author_id,
                "display_name": author_name,
                "orcid": orcid
            })
            
        # Institutions for this author
        for inst_data in auth_data.get("institutions", []) or []:
            inst_name = inst_data.get("display_name", "") or ""
            inst_id = inst_data.get("id", "") or ""
            ror = inst_data.get("ror", "") or ""
            country = inst_data.get("country_code", "") or ""
            inst_type = inst_data.get("type", "") or ""
            
            if inst_name and inst_name not in institutions_list:
                institutions_list.append(inst_name)
            if country and country not in countries_list:
                countries_list.append(country)
                
            if inst_id:
                rel_institutions.append({
                    "institution_id": inst_id,
                    "display_name": inst_name,
                    "ror": ror,
                    "country_code": country,
                    "type": inst_type
                })
                if work_id and author_id:
                    rel_affiliations.append({
                        "work_id": work_id,
                        "author_id": author_id,
                        "institution_id": inst_id,
                        "author_position": author_pos
                    })
            
            # Capture first author's first institution name
            if idx == 0 and not first_author_inst:
                first_author_inst = inst_name

    # Topic fields
    prim_topic = work.get("primary_topic", {}) or {}
    primary_topic_name = prim_topic.get("display_name", "") or ""
    primary_topic_field = prim_topic.get("field", {}).get("display_name", "") or ""
    primary_topic_domain = prim_topic.get("domain", {}).get("display_name", "") or ""
    
    # MeSH terms
    mesh_terms = []
    major_mesh_terms = []
    for m in work.get("mesh", []) or []:
        desc = m.get("descriptor_name", "") or ""
        qual = m.get("qualifier_name", "") or ""
        is_major = m.get("is_major_topic", False)
        
        if desc:
            term = f"{desc}/{qual}" if qual else desc
            if term not in mesh_terms:
                mesh_terms.append(term)
            if is_major and term not in major_mesh_terms:
                major_mesh_terms.append(term)
                
    # Funders
    funders_list = []
    for f in work.get("funders", []) or []:
        f_name = f.get("display_name", "") or ""
        if f_name and f_name not in funders_list:
            funders_list.append(f_name)
            
    # Abstract reconstruction
    abstract = reconstruct_abstract(work.get("abstract_inverted_index", {}))
    
    # Source relational data
    rel_source = None
    if source_id:
        rel_source = {
            "source_id": source_id,
            "display_name": journal_name,
            "publisher": publisher,
            "issn": issn
        }
    
    flat_meta = {
        "openalex_id": work_id,
        "doi": doi,
        "title": title,
        "publication_year": publication_year,
        "publication_date": publication_date,
        "type": type_,
        "language": language,
        "journal_name": journal_name,
        "publisher": publisher,
        "issn": issn,
        "is_oa": is_oa,
        "oa_status": oa_status,
        "oa_url": oa_url,
        "authors": "; ".join(authors_list),
        "first_author_institution": first_author_inst,
        "institutions": "; ".join(institutions_list),
        "countries": "; ".join(countries_list),
        "cited_by_count": work.get("cited_by_count", 0),
        "primary_topic": primary_topic_name,
        "primary_topic_field": primary_topic_field,
        "primary_topic_domain": primary_topic_domain,
        "mesh_terms": "; ".join(mesh_terms),
        "major_mesh_terms": "; ".join(major_mesh_terms),
        "funders": "; ".join(funders_list),
        "abstract": abstract,
        "updated_date": work.get("updated_date", "") or ""
    }
    
    return {
        "flat_metadata": flat_meta,
        "relational_data": {
            "authors": rel_authors,
            "institutions": rel_institutions,
            "source": rel_source,
            "affiliations": rel_affiliations
        }
    }

# ---------------------------------------------------------------------------
# Thread-safe Relational CSV Writer
# ---------------------------------------------------------------------------

class RelationalCSVWriter:
    """Manages thread-safe writing of flat metadata and relational CSV tables."""
    def __init__(self, output_dir: Path, enable_relational: bool = False, file_prefix: str = "openalex_works"):
        self.output_dir = output_dir
        self.enable_relational = enable_relational
        self.lock = threading.Lock()
        
        # Core Output Files
        self.works_file = output_dir / f"{file_prefix}.csv"
        self.works_fields = [
            "openalex_id", "doi", "title", "publication_year", "publication_date", "type", "language",
            "journal_name", "publisher", "issn", "is_oa", "oa_status", "oa_url", "authors",
            "first_author_institution", "institutions", "countries", "cited_by_count",
            "primary_topic", "primary_topic_field", "primary_topic_domain", "mesh_terms",
            "major_mesh_terms", "funders", "abstract", "updated_date"
        ]
        
        # Initialize Core CSV
        self._init_file(self.works_file, self.works_fields)
        
        if self.enable_relational:
            self.authors_file = output_dir / f"{file_prefix}_authors.csv"
            self.institutions_file = output_dir / f"{file_prefix}_institutions.csv"
            self.sources_file = output_dir / f"{file_prefix}_sources.csv"
            self.affiliations_file = output_dir / f"{file_prefix}_affiliations.csv"
            
            self.authors_fields = ["author_id", "display_name", "orcid"]
            self.institutions_fields = ["institution_id", "display_name", "ror", "country_code", "type"]
            self.sources_fields = ["source_id", "display_name", "publisher", "issn"]
            self.affiliations_fields = ["work_id", "author_id", "institution_id", "author_position"]
            
            self._init_file(self.authors_file, self.authors_fields)
            self._init_file(self.institutions_file, self.institutions_fields)
            self._init_file(self.sources_file, self.sources_fields)
            self._init_file(self.affiliations_file, self.affiliations_fields)
            
            # De-duplication structures
            self.seen_authors = set()
            self.seen_institutions = set()
            self.seen_sources = set()
            self.seen_affiliations = set()
            
            self._load_seen_ids()

    def _init_file(self, file_path: Path, fields: list):
        if not file_path.exists():
            with open(file_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(fields)

    def _load_seen_ids(self):
        """Loads unique entities from existing files to allow flawless resume operations."""
        if self.authors_file.exists():
            try:
                with open(self.authors_file, "r", encoding="utf-8") as f:
                    reader = csv.reader(f)
                    next(reader, None)
                    for row in reader:
                        if row:
                            self.seen_authors.add(row[0])
            except Exception as e:
                logger.debug(f"Could not load existing authors: {e}")
                
        if self.institutions_file.exists():
            try:
                with open(self.institutions_file, "r", encoding="utf-8") as f:
                    reader = csv.reader(f)
                    next(reader, None)
                    for row in reader:
                        if row:
                            self.seen_institutions.add(row[0])
            except Exception as e:
                logger.debug(f"Could not load existing institutions: {e}")
                
        if self.sources_file.exists():
            try:
                with open(self.sources_file, "r", encoding="utf-8") as f:
                    reader = csv.reader(f)
                    next(reader, None)
                    for row in reader:
                        if row:
                            self.seen_sources.add(row[0])
            except Exception as e:
                logger.debug(f"Could not load existing sources: {e}")
                
        if self.affiliations_file.exists():
            try:
                with open(self.affiliations_file, "r", encoding="utf-8") as f:
                    reader = csv.reader(f)
                    next(reader, None)
                    for row in reader:
                        if len(row) >= 3:
                            self.seen_affiliations.add((row[0], row[1], row[2]))
            except Exception as e:
                logger.debug(f"Could not load existing affiliations: {e}")

    def write_records(self, works_data: list):
        """Safely writes a list of parsed works to CSVs using locks."""
        with self.lock:
            # Open files to append
            with open(self.works_file, "a", newline="", encoding="utf-8") as f_w:
                w_writer = csv.DictWriter(f_w, fieldnames=self.works_fields)
                
                if self.enable_relational:
                    f_a = open(self.authors_file, "a", newline="", encoding="utf-8")
                    f_i = open(self.institutions_file, "a", newline="", encoding="utf-8")
                    f_s = open(self.sources_file, "a", newline="", encoding="utf-8")
                    f_aff = open(self.affiliations_file, "a", newline="", encoding="utf-8")
                    
                    a_writer = csv.writer(f_a)
                    i_writer = csv.writer(f_i)
                    s_writer = csv.writer(f_s)
                    aff_writer = csv.writer(f_aff)
                
                try:
                    for work in works_data:
                        w_writer.writerow(work["flat_metadata"])
                        
                        if self.enable_relational:
                            # Unique Authors
                            for aut in work["relational_data"]["authors"]:
                                aut_id = aut["author_id"]
                                if aut_id and aut_id not in self.seen_authors:
                                    a_writer.writerow([aut_id, aut["display_name"], aut["orcid"]])
                                    self.seen_authors.add(aut_id)
                                    
                            # Unique Institutions
                            for inst in work["relational_data"]["institutions"]:
                                inst_id = inst["institution_id"]
                                if inst_id and inst_id not in self.seen_institutions:
                                    i_writer.writerow([
                                        inst_id, inst["display_name"], inst["ror"], 
                                        inst["country_code"], inst["type"]
                                    ])
                                    self.seen_institutions.add(inst_id)
                                    
                            # Unique Sources
                            src = work["relational_data"]["source"]
                            if src:
                                src_id = src["source_id"]
                                if src_id and src_id not in self.seen_sources:
                                    s_writer.writerow([
                                        src_id, src["display_name"], src["publisher"], src["issn"]
                                    ])
                                    self.seen_sources.add(src_id)
                                    
                            # Affiliations Map
                            for aff in work["relational_data"]["affiliations"]:
                                aff_key = (aff["work_id"], aff["author_id"], aff["institution_id"])
                                if aff_key not in self.seen_affiliations:
                                    aff_writer.writerow([
                                        aff["work_id"], aff["author_id"], aff["institution_id"], 
                                        aff["author_position"]
                                    ])
                                    self.seen_affiliations.add(aff_key)
                finally:
                    f_w.flush()
                    if self.enable_relational:
                        f_a.close()
                        f_i.close()
                        f_s.close()
                        f_aff.close()

# ---------------------------------------------------------------------------
# Core HTTP Request Engine
# ---------------------------------------------------------------------------

def fetch_page(cursor: str, rotator: APIKeyRotator, filters: str, query: str, per_page: int = 100) -> dict:
    """
    Sends a query page request to OpenAlex works endpoint, handling 
    throttling retries on 429 rate limiting and budget rotation.
    """
    url = "https://api.openalex.org/works"
    params = {
        "filter": filters,
        "per_page": per_page,
        "cursor": cursor
    }
    if query:
        params["search"] = query
        
    headers = {
        "User-Agent": "BiolytInternOpenAlexDownloader/1.0 (mailto:intern@biolyt.com)"
    }
    
    max_retries = 6
    backoff_factor = 2.0
    
    for attempt in range(max_retries):
        api_key = rotator.get_key() if rotator else None
        if api_key:
            params["api_key"] = api_key
        elif "api_key" in params:
            del params["api_key"]
            
        try:
            r = requests.get(url, params=params, headers=headers, timeout=45)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:
                # Check response body to see if it is an Insufficient budget error
                is_budget_error = False
                try:
                    resp_json = r.json()
                    err_msg = resp_json.get("message", "").lower()
                    if "budget" in err_msg or "insufficient" in err_msg:
                        is_budget_error = True
                except Exception:
                    pass
                
                # If budget limit reached and we have multiple keys configured
                if is_budget_error and rotator and len(rotator.get_all_keys()) > 1:
                    logger.warning(f"Key budget exhausted for key '{api_key[:5]}...'. Rotating API Key.")
                    rotator.rotate_key(api_key)
                    time.sleep(0.5) # Polite delay
                    continue
                else:
                    # Normal rate limit backoff
                    wait_time = (backoff_factor ** attempt) + 2.0
                    logger.warning(f"Rate limited (429). Retrying in {wait_time:.1f}s (attempt {attempt+1}/{max_retries})...")
                    time.sleep(wait_time)
            elif r.status_code == 403:
                # 403 Forbidden might indicate an invalid API key. Try rotating to be safe.
                logger.error(f"Forbidden (403) for key '{api_key[:5] if api_key else 'none'}...'")
                if rotator and len(rotator.get_all_keys()) > 1:
                    rotator.rotate_key(api_key)
                    time.sleep(0.5)
                    continue
                r.raise_for_status()
            elif r.status_code >= 500:
                wait_time = (backoff_factor ** attempt) + 2.0
                logger.warning(f"OpenAlex server error ({r.status_code}). Retrying in {wait_time:.1f}s (attempt {attempt+1}/{max_retries})...")
                time.sleep(wait_time)
            else:
                logger.error(f"HTTP error {r.status_code}: {r.text}")
                r.raise_for_status()
        except requests.exceptions.RequestException as e:
            wait_time = (backoff_factor ** attempt) + 2.0
            logger.warning(f"Network error: {e}. Retrying in {wait_time:.1f}s (attempt {attempt+1}/{max_retries})...")
            time.sleep(wait_time)
            
    raise RuntimeError(f"Failed to fetch page from OpenAlex API after {max_retries} attempts.")

# ---------------------------------------------------------------------------
# State Checkpoint & Limit Engine
# ---------------------------------------------------------------------------

class SafeProgressTracker:
    """Manages thread-safe read/write operations on the JSON progress state file."""
    def __init__(self, progress_file: Path):
        self.progress_file = progress_file
        self.lock = threading.Lock()
        
    def get_partition_state(self, key: str) -> dict:
        with self.lock:
            if not self.progress_file.exists():
                return {"cursor": "*", "total_downloaded": 0, "completed": False}
            try:
                with open(self.progress_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data.get(key, {"cursor": "*", "total_downloaded": 0, "completed": False})
            except Exception as e:
                logger.warning(f"Could not parse progress checkpoint file: {e}")
                return {"cursor": "*", "total_downloaded": 0, "completed": False}

    def update_partition_state(self, key: str, cursor: str, count_inc: int, completed: bool = False):
        with self.lock:
            data = {}
            if self.progress_file.exists():
                try:
                    with open(self.progress_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    pass
            
            if key not in data:
                data[key] = {"cursor": "*", "total_downloaded": 0, "completed": False}
                
            data[key]["cursor"] = cursor
            data[key]["total_downloaded"] += count_inc
            data[key]["completed"] = completed
            
            with open(self.progress_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)


class ThreadSafeCounter:
    """Tracks global limit criteria across multiple parallel worker threads."""
    def __init__(self, limit: int = None):
        self.limit = limit
        self.value = 0
        self.lock = threading.Lock()
        
    def add(self, amount: int) -> int:
        with self.lock:
            self.value += amount
            return self.value
            
    def is_reached(self) -> bool:
        if self.limit is None:
            return False
        with self.lock:
            return self.value >= self.limit

# ---------------------------------------------------------------------------
# Parallel Worker Task
# ---------------------------------------------------------------------------

def run_partition_worker(
    partition_name: str,
    filters: str,
    query: str,
    rotator: APIKeyRotator,
    writer: RelationalCSVWriter,
    tracker: SafeProgressTracker,
    global_counter: ThreadSafeCounter
):
    """Processes a single partition (e.g. a year) sequentially using cursor pagination."""
    logger.info(f"Worker started for partition: {partition_name} (Filters: {filters})")
    
    # 1. Load progress checkpoint
    state = tracker.get_partition_state(partition_name)
    if state.get("completed", False):
        logger.info(f"Partition {partition_name} is already marked as completed. Skipping.")
        return False
        
    cursor = state.get("cursor", "*")
    per_page = 100
    
    # If running with a global limit, adjust per_page if needed
    if global_counter.limit:
        remaining_budget = global_counter.limit - global_counter.value
        if remaining_budget <= 0:
            logger.info(f"Global download limit reached. Stopping partition {partition_name}.")
            return
        per_page = min(100, remaining_budget)
        
    page_num = 1
    
    while True:
        # Check global limit
        if global_counter.is_reached():
            logger.info(f"Global download limit reached. Stopping partition {partition_name}.")
            break
            
        logger.info(f"[{partition_name}] Requesting page {page_num} with cursor: {cursor[:15]}...")
        
        try:
            payload = fetch_page(cursor, rotator, filters, query, per_page=per_page)
            results = payload.get("results", [])
            meta = payload.get("meta", {})
            next_cursor = meta.get("next_cursor")
            
            # Print page status
            total_matches = meta.get("count", 0)
            logger.debug(f"[{partition_name}] Received {len(results)} items (Total matches in query: {total_matches}).")
            
            if not results:
                logger.info(f"[{partition_name}] No more results returned by API. Marking partition as completed.")
                tracker.update_partition_state(partition_name, cursor, 0, completed=True)
                break
                
            # Parse results
            parsed_works = []
            for work in results:
                parsed_works.append(parse_work(work))
                
            # Thread-safe write to CSV
            writer.write_records(parsed_works)
            
            # Increment global and local counters
            actual_saved = len(parsed_works)
            global_counter.add(actual_saved)
            
            # Save checkpoint state
            cursor = next_cursor
            tracker.update_partition_state(partition_name, cursor, actual_saved, completed=False)
            
            page_num += 1
            
            # Terminal loop condition: if next_cursor is same as current or missing
            if not next_cursor or next_cursor == state.get("cursor"):
                logger.info(f"[{partition_name}] Reached the end of pagination stream (next_cursor identical/empty).")
                tracker.update_partition_state(partition_name, cursor, 0, completed=True)
                break
                
            # Polite throttle delay if running without API keys
            if not rotator or not rotator.get_all_keys():
                time.sleep(1.0)
            else:
                time.sleep(0.1) # Safe default delay to prevent slamming OpenAlex servers
                
            # Re-evaluate page request size based on remaining limit
            if global_counter.limit:
                remaining_budget = global_counter.limit - global_counter.value
                if remaining_budget <= 0:
                    break
                per_page = min(100, remaining_budget)
                
        except Exception as e:
            logger.error(f"[{partition_name}] Critical error in partition loop: {e}")
            # Abandoning the partition mid-stream leaves it incomplete. Report that
            # upward instead of falling through and exiting 0 with partial data.
            return True

    return False

# ---------------------------------------------------------------------------
# Main Orchestration Loop
# ---------------------------------------------------------------------------

def run_downloader(
    output_dir: Path,
    domain_id: str,
    field_id: str,
    query: str,
    start_year: int,
    end_year: int,
    limit: int,
    api_keys: list,
    threads: int,
    enable_relational: bool,
    extra_filters: str
):
    """Orchestrates partitioned multithreading or sequential metadata harvesting."""
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Setup logger file destination
    setup_logging(verbose=True, log_file=output_dir / "openalex_downloader.log")
    
    logger.info("=" * 60)
    logger.info("      OPENALEX BIOMEDICAL LITERATURE METADATA HARVESTER")
    logger.info("=" * 60)
    
    # Setup API Key Rotator
    rotator = APIKeyRotator(api_keys)
    if not rotator.get_all_keys():
        logger.warning(
            "No active API keys found. Running in Public Sandbox mode with strict "
            "credit/rate limit controls. Consider adding keys to your .env file."
        )
    else:
        logger.info(f"Loaded {len(rotator.get_all_keys())} API Key(s) for automatic rotation.")
        
    # Build filter tokens
    base_filter_tokens = []
    if field_id:
        base_filter_tokens.append(f"primary_topic.field.id:{field_id}")
        logger.info(f"Target Field: {field_id}")
    elif domain_id:
        base_filter_tokens.append(f"primary_topic.domain.id:{domain_id}")
        logger.info(f"Target Domain: {domain_id}")
        
    if extra_filters:
        for f in extra_filters.split(","):
            if f.strip():
                base_filter_tokens.append(f.strip())
        logger.info(f"Extra user-defined filters: {extra_filters}")
        
    # Setup Writers and Trackers
    prefix = extra_filters_dict.get("prefix", "openalex") if 'extra_filters_dict' in locals() else "openalex"
    # Wait, let's just make it a formal argument in run_downloader
def run_downloader(
    output_dir: Path,
    domain_id: str,
    field_id: str,
    query: str,
    start_year: int,
    end_year: int,
    limit: int,
    api_keys: list,
    threads: int,
    enable_relational: bool,
    extra_filters: str,
    file_prefix: str
):
    """Orchestrates partitioned multithreading or sequential metadata harvesting."""
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Setup logger file destination
    setup_logging(verbose=True, log_file=output_dir / f"{file_prefix}_downloader.log")
    
    logger.info("=" * 60)
    logger.info("      OPENALEX BIOMEDICAL LITERATURE METADATA HARVESTER")
    logger.info("=" * 60)
    
    # Setup API Key Rotator
    rotator = APIKeyRotator(api_keys)
    if not rotator.get_all_keys():
        logger.warning(
            "No active API keys found. Running in Public Sandbox mode with strict "
            "credit/rate limit controls. Consider adding keys to your .env file."
        )
    else:
        logger.info(f"Loaded {len(rotator.get_all_keys())} API Key(s) for automatic rotation.")
        
    # Build filter tokens
    base_filter_tokens = []
    if field_id:
        base_filter_tokens.append(f"primary_topic.field.id:{field_id}")
        logger.info(f"Target Field: {field_id}")
    elif domain_id:
        base_filter_tokens.append(f"primary_topic.domain.id:{domain_id}")
        logger.info(f"Target Domain: {domain_id}")
        
    if extra_filters:
        for f in extra_filters.split(","):
            if f.strip():
                base_filter_tokens.append(f.strip())
        logger.info(f"Extra user-defined filters: {extra_filters}")
        
    # Setup Writers and Trackers
    writer = RelationalCSVWriter(output_dir, enable_relational=enable_relational, file_prefix=file_prefix)
    tracker = SafeProgressTracker(output_dir / f"{file_prefix}_progress.json")
    global_counter = ThreadSafeCounter(limit=limit)
    
    # Establish Partitions
    partitions = []
    if threads > 1 and start_year and end_year:
        # Multithreading partition: split query by year
        logger.info(f"Multithreaded execution active. Dividing query into year partitions from {start_year} to {end_year}.")
        for y in range(start_year, end_year + 1):
            y_filter = ",".join(base_filter_tokens + [f"publication_year:{y}"])
            partitions.append({
                "name": str(y),
                "filter": y_filter
            })
    else:
        # Single-threaded query partition
        logger.info("Sequential single-threaded execution active.")
        all_filter_tokens = list(base_filter_tokens)
        if start_year:
            all_filter_tokens.append(f"from_publication_date:{start_year}-01-01")
        if end_year:
            all_filter_tokens.append(f"to_publication_date:{end_year}-12-31")
            
        all_filter = ",".join(all_filter_tokens)
        partitions.append({
            "name": "all",
            "filter": all_filter
        })
        
    # Partitions that aborted mid-stream; any of them means an incomplete harvest.
    partition_failures = 0

    # Run loop
    if len(partitions) > 1:
        # Spin up thread pool executor
        logger.info(f"Spawning thread pool executor with max_workers={threads}...")
        with ThreadPoolExecutor(max_workers=threads, thread_name_prefix="Worker") as executor:
            futures = []
            for p in partitions:
                f = executor.submit(
                    run_partition_worker,
                    partition_name=p["name"],
                    filters=p["filter"],
                    query=query,
                    rotator=rotator,
                    writer=writer,
                    tracker=tracker,
                    global_counter=global_counter
                )
                futures.append(f)
                
            for fut in as_completed(futures):
                try:
                    if fut.result():
                        partition_failures += 1
                except Exception as e:
                    logger.error(f"Thread worker raised unhandled exception: {e}")
                    partition_failures += 1
    else:
        # Run sequentially
        p = partitions[0]
        if run_partition_worker(
            partition_name=p["name"],
            filters=p["filter"],
            query=query,
            rotator=rotator,
            writer=writer,
            tracker=tracker,
            global_counter=global_counter
        ):
            partition_failures += 1

    logger.info("=" * 60)
    if partition_failures:
        logger.error(
            f"   HARVEST INCOMPLETE: {partition_failures} partition(s) aborted early. "
            f"Parsed & saved so far: {global_counter.value}"
        )
        logger.info("=" * 60)
        # Non-zero exit keeps the pipeline from publishing a partial harvest.
        sys.exit(1)
    logger.info(f"   HARVEST PROCESS COMPLETE. Total parsed & saved records: {global_counter.value}")
    logger.info("=" * 60)

# ---------------------------------------------------------------------------
# Script Entrypoint
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Harvest biomedical literature metadata from OpenAlex API into flat or relational CSVs."
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(BASE_DIR / "openalex"),
        help="Directory to save output CSV datasets and progress tracking logs."
    )
    parser.add_argument(
        "--domain-id",
        type=str,
        default="4",
        help="OpenAlex domain ID filter. Default '4' represents 'Health Sciences'. Use 'none' to disable."
    )
    parser.add_argument(
        "--field-id",
        type=str,
        default=None,
        help="OpenAlex field ID filter (e.g. '27' represents 'Medicine'). If set, overrides domain filter."
    )
    parser.add_argument(
        "--query",
        type=str,
        default=None,
        help="Optional search query keyword to restrict results (e.g. 'oncology', 'clinical trial')."
    )
    parser.add_argument(
        "--start-year",
        type=int,
        default=None,
        help="Filter publication year starting from this year (inclusive)."
    )
    parser.add_argument(
        "--end-year",
        type=int,
        default=None,
        help="Filter publication year up to this year (inclusive)."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Total maximum number of works to download in this execution session."
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=1,
        help="Number of parallel worker threads. Requires --start-year and --end-year to slice partitions."
    )
    parser.add_argument(
        "--relational",
        action="store_true",
        help="If specified, also exports separate relational mapping files for authors, institutions, and sources."
    )
    parser.add_argument(
        "--extra-filters",
        type=str,
        default=None,
        help="Comma-separated string of additional key:value filter parameters (e.g., 'is_oa:true,has_abstract:true')."
    )
    parser.add_argument(
        "--prefix",
        type=str,
        default="openalex_works",
        help="File prefix for saved datasets and logs."
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enables full debug logging output."
    )
    
    args = parser.parse_args()
    
    # Process "none" string arguments
    domain = None if args.domain_id.lower() == "none" else args.domain_id
    field = args.field_id
    
    output_path = Path(args.output_dir)
    
    # Load API keys from environment
    keys_list = []
    # Check for single key first
    single_key = os.getenv("OPENALEX_API_KEY")
    if single_key:
        keys_list.append(single_key)
        
    # Check for comma-separated list
    multi_keys = os.getenv("OPENALEX_API_KEYS")
    if multi_keys:
        for k in multi_keys.split(","):
            if k.strip() and k.strip() not in keys_list:
                keys_list.append(k.strip())
                
    run_downloader(
        output_dir=output_path,
        domain_id=domain,
        field_id=field,
        query=args.query,
        start_year=args.start_year,
        end_year=args.end_year,
        limit=args.limit,
        api_keys=keys_list,
        threads=args.threads,
        enable_relational=args.relational,
        extra_filters=args.extra_filters,
        file_prefix=args.prefix
    )

if __name__ == "__main__":
    main()
