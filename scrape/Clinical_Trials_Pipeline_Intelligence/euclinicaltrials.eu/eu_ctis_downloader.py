#!/usr/bin/env python3
"""
eu_ctis_downloader.py
====================
Downloads clinical trial details from the Clinical Trials Information System (CTIS) 
public portal (euclinicaltrials.eu) using their internal search and retrieve API endpoints.

Features:
- Incremental paging over the POST search endpoint.
- Resumable crawling via checkpoint.json.
- Stream-writes page data to a local temp_studies.jsonl file.
- Optional --full flag to fetch granular trial details (inclusion/exclusion criteria)
  for every trial.
- Auto-converts the JSONL results into a consolidated CSV.
"""

import os
import sys
import json
import csv
import time
import argparse
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

# --- Configuration Constants ---
SEARCH_URL = "https://euclinicaltrials.eu/ctis-public-api/search"
RETRIEVE_URL = "https://euclinicaltrials.eu/ctis-public-api/retrieve"
DEFAULT_PAGE_SIZE = 100

# Combined request rate cap across ALL threads (requests/second).
# CTIS public API rate limits are undocumented, so stay conservative.
DEFAULT_MAX_RPS = 5.0
DEFAULT_THREADS = 5


class RateLimiter:
    """Thread-safe global rate limiter (min-interval gate).

    Enforces a maximum combined request rate across every worker thread by
    handing out evenly spaced "slots". Each caller reserves the next slot
    under a lock, then sleeps (outside the lock) until that slot is due, so
    the aggregate outbound rate never exceeds ``rate_per_sec`` regardless of
    how many threads are calling.
    """

    def __init__(self, rate_per_sec):
        self.min_interval = 1.0 / float(rate_per_sec) if rate_per_sec > 0 else 0.0
        self._lock = threading.Lock()
        self._next_time = 0.0

    def acquire(self):
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            scheduled = max(self._next_time, now)
            self._next_time = scheduled + self.min_interval
        wait = scheduled - now
        if wait > 0:
            time.sleep(wait)


# Global limiter shared by all threads; rate is (re)set from CLI in main().
RATE_LIMITER = RateLimiter(DEFAULT_MAX_RPS)

# Setup local paths relative to the script file
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "ctis_data"
JSONL_TEMP_FILE = OUTPUT_DIR / "temp_studies.jsonl"
CHECKPOINT_FILE = OUTPUT_DIR / "checkpoint.json"
FINAL_CSV_FILE = OUTPUT_DIR / "ctis_all.csv"

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("ctis_scraper")

# CTIS Status Code Mapping
STATUS_MAPPING = {
    1: "Under evaluation",
    2: "Authorised",
    3: "Authorised with conditions",
    4: "Not authorised",
    5: "Withdrawn",
    6: "Suspended",
    7: "Revoked",
    8: "Lapsed",
    9: "Expired",
    10: "Ended",
    11: "Not authorised", # Observed 11 maps to Not authorised in detail
}

def get_session():
    """Returns a requests Session configured with retries and exponential backoff."""
    session = requests.Session()
    retry_strategy = Retry(
        total=5,
        backoff_factor=2.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )
    # Pool sized generously so concurrent worker threads don't contend for
    # connections (each thread reuses this shared, thread-safe Session).
    adapter = HTTPAdapter(
        max_retries=retry_strategy,
        pool_connections=20,
        pool_maxsize=20,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })
    return session

def load_checkpoint():
    """Loads previous crawl progress if it exists."""
    if CHECKPOINT_FILE.exists():
        try:
            with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to read checkpoint file: {e}. Starting fresh.")
    return None

def save_checkpoint(page_num, total_trials):
    """Saves current crawl progress."""
    checkpoint = {
        "page_num": page_num,
        "total_trials": total_trials,
        "timestamp": time.time()
    }
    try:
        with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
            json.dump(checkpoint, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to write checkpoint file: {e}")

def clear_checkpoint():
    """Clears the checkpoint file once the crawl completes successfully."""
    if CHECKPOINT_FILE.exists():
        try:
            CHECKPOINT_FILE.unlink()
            logger.info("Cleared checkpoint file.")
        except Exception as e:
            logger.warning(f"Could not remove checkpoint file: {e}")

def flatten_study(study):
    """Flattens a single nested study JSON object (combining search and detail APIs)."""
    
    # 1. Basic Fields (from Search API)
    trial_id = study.get("ctNumber")
    
    # Handle status conversion
    ct_status = study.get("ctStatus")
    if isinstance(ct_status, int):
        overall_status = STATUS_MAPPING.get(ct_status, f"Unknown ({ct_status})")
    else:
        overall_status = ct_status or study.get("status")
        
    title = study.get("ctTitle")
    short_title = study.get("shortTitle")
    
    # Sponsors
    sponsor = study.get("sponsor")
    sponsor_type = study.get("sponsorType")
    
    trial_phase = study.get("trialPhase")
    decision_date = study.get("decisionDateOverall") or study.get("decisionDate")
    last_updated = study.get("lastUpdated")
    last_publication_update = study.get("lastPublicationUpdate")
    
    # Conditions & Therapeutic Areas
    conditions_val = study.get("conditions", "")
    if isinstance(conditions_val, list):
        conditions = " | ".join(conditions_val)
    else:
        conditions = conditions_val
        
    therapeutic_areas_val = study.get("therapeuticAreas", [])
    if all(isinstance(x, dict) for x in therapeutic_areas_val):
        therapeutic_areas = " | ".join([x.get("name", "") for x in therapeutic_areas_val])
    else:
        therapeutic_areas = " | ".join(therapeutic_areas_val)
        
    gender = study.get("gender")
    age_groups = study.get("ageGroup")
    total_enrolled = study.get("totalNumberEnrolled")
    
    # Countries
    trial_countries = study.get("trialCountries", [])
    member_states = " | ".join([c.split(":")[0] for c in trial_countries])
    
    # Products
    product_val = study.get("product", "")
    if isinstance(product_val, list):
        products = " | ".join(product_val)
    else:
        products = product_val
        
    # Endpoints
    primary_endpoint = study.get("primaryEndPoint")
    secondary_endpoint = study.get("endPoint")
    results_received = study.get("resultsFirstReceived")
    
    # 2. Detail Fields (only present if crawled with --full flag)
    inclusion_criteria = ""
    exclusion_criteria = ""
    main_objective = ""
    secondary_objectives = ""
    
    detail = study.get("detail_data")
    if detail:
        part1 = detail.get("authorizedApplication", {}).get("authorizedPartI", {})
        
        # Override fields with cleaner detail values if available
        if part1:
            # Objective
            trial_info = part1.get("trialDetails", {}).get("trialInformation", {})
            obj_info = trial_info.get("trialObjective", {})
            main_objective = obj_info.get("mainObjective", "")
            
            sec_objs = [o.get("secondaryObjective") for o in obj_info.get("secondaryObjectives", []) if o.get("secondaryObjective")]
            secondary_objectives = " | ".join(sec_objs)
            
            # Eligibility
            elig = trial_info.get("eligibilityCriteria", {})
            inclusions = [i.get("principalInclusionCriteria") for i in elig.get("principalInclusionCriteria", []) if i.get("principalInclusionCriteria")]
            inclusion_criteria = "\n".join(inclusions)
            
            exclusions = [e.get("principalExclusionCriteria") for e in elig.get("principalExclusionCriteria", []) if e.get("principalExclusionCriteria")]
            exclusion_criteria = "\n".join(exclusions)
            
            # Products override
            p_list = [p.get("productName") for p in part1.get("products", []) if p.get("productName")]
            if p_list:
                products = " | ".join(p_list)
                
            # Override title & status if detailed version is cleaner
            title = part1.get("trialDetails", {}).get("clinicalTrialIdentifiers", {}).get("publicTitle") or title
            overall_status = detail.get("ctStatus") or overall_status

    return {
        "trial_id": trial_id,
        "title": title,
        "short_title": short_title,
        "overall_status": overall_status,
        "sponsor_name": sponsor,
        "sponsor_type": sponsor_type,
        "trial_phase": trial_phase,
        "decision_date": decision_date,
        "last_updated": last_updated,
        "last_publication_update": last_publication_update,
        "conditions": conditions,
        "therapeutic_area": therapeutic_areas,
        "gender": gender,
        "age_groups": age_groups,
        "total_enrolled": total_enrolled,
        "member_states": member_states,
        "products": products,
        "primary_endpoint": primary_endpoint,
        "secondary_endpoint": secondary_endpoint,
        "results_received": results_received,
        "main_objective": main_objective,
        "secondary_objectives": secondary_objectives,
        "inclusion_criteria": inclusion_criteria,
        "exclusion_criteria": exclusion_criteria
    }

def convert_jsonl_to_csv():
    """Parses the JSONL file and compiles the consolidated CSV."""
    if not JSONL_TEMP_FILE.exists():
        logger.error(f"No downloaded data file found at {JSONL_TEMP_FILE}")
        return False
        
    logger.info(f"Converting downloaded trials from JSONL to CSV: {FINAL_CSV_FILE}")
    
    fieldnames = [
        "trial_id", "title", "short_title", "overall_status", "sponsor_name", "sponsor_type",
        "trial_phase", "decision_date", "last_updated", "last_publication_update", "conditions",
        "therapeutic_area", "gender", "age_groups", "total_enrolled", "member_states", "products",
        "primary_endpoint", "secondary_endpoint", "results_received", "main_objective", 
        "secondary_objectives", "inclusion_criteria", "exclusion_criteria"
    ]
    
    try:
        FINAL_CSV_FILE.parent.mkdir(parents=True, exist_ok=True)
        
        with open(JSONL_TEMP_FILE, "r", encoding="utf-8") as infile:
            with open(FINAL_CSV_FILE, "w", encoding="utf-8", newline="") as outfile:
                writer = csv.DictWriter(outfile, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                
                count = 0
                for line in infile:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        study = json.loads(line)
                        flat_record = flatten_study(study)
                        writer.writerow(flat_record)
                        count += 1
                    except Exception as e:
                        logger.error(f"Error parsing line during CSV conversion: {e}")
                        
        logger.info(f"[OK] Successfully wrote {count:,} trials to {FINAL_CSV_FILE.name}")
        return True
    except Exception as e:
        logger.error(f"Failed to compile CSV: {e}")
        return False

def fetch_detail(session, trial):
    """Fetch the full detail record for a single trial (thread worker).

    Mutates and returns the trial dict (adds ``detail_data`` on success).
    Every outbound request passes through the global RATE_LIMITER so the
    combined rate across all worker threads stays bounded. HTTP 429/503 are
    already retried with exponential backoff by the Session's Retry policy;
    a persistent failure is logged and the trial is kept without detail data.
    """
    trial_id = trial.get("ctNumber")
    if not trial_id:
        return trial

    RATE_LIMITER.acquire()
    try:
        det_resp = session.get(f"{RETRIEVE_URL}/{trial_id}", timeout=15)
        if det_resp.status_code == 200:
            trial["detail_data"] = det_resp.json()
        else:
            logger.warning(f"  [!] Failed to get detail for {trial_id}: HTTP {det_resp.status_code}")
    except Exception as e:
        logger.warning(f"  [!] Failed to get detail for {trial_id}: {e}")
    return trial


def main():
    parser = argparse.ArgumentParser(description="CTIS (euclinicaltrials.eu) Scraper")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of pages to download (for testing)")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE, help="Number of records per page (default: 100)")
    parser.add_argument("--resume", action="store_true", default=True, help="Resume download from checkpoint (default: True)")
    parser.add_argument("--no-resume", dest="resume", action="store_false", help="Ignore existing checkpoints")
    parser.add_argument("--full", action="store_true", help="Fetch detailed trial parameters (inclusion/exclusion criteria) for each trial (Slower)")
    parser.add_argument("--no-cleanup", action="store_true", help="Keep the temporary JSONL download file after merge")
    parser.add_argument("--csv-only", action="store_true", help="Only build CSV from the existing JSONL data, without calling the API")
    parser.add_argument("--threads", type=int, default=DEFAULT_THREADS,
                        help=f"Number of concurrent worker threads for per-trial detail fetches in --full mode (default: {DEFAULT_THREADS}; use 1 for sequential)")
    parser.add_argument("--max-rps", type=float, default=DEFAULT_MAX_RPS,
                        help=f"Combined request rate cap across ALL threads, in requests/second (default: {DEFAULT_MAX_RPS})")

    args = parser.parse_args()

    # Sanitize concurrency / rate options and (re)configure the global limiter.
    threads = max(1, args.threads)
    global RATE_LIMITER
    RATE_LIMITER = RateLimiter(args.max_rps)
    logger.info(f"Concurrency: {threads} thread(s); global rate cap: {args.max_rps} req/s")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.csv_only:
        success = convert_jsonl_to_csv()
        sys.exit(0 if success else 1)

    session = get_session()
    
    # Resuming logic
    page_num = 1
    total_trials = 0
    write_mode = "w"
    
    if args.resume:
        checkpoint = load_checkpoint()
        if checkpoint:
            page_num = checkpoint.get("page_num", 1)
            total_trials = checkpoint.get("total_trials", 0)
            write_mode = "a"
            logger.info(f"Resuming crawl from search page {page_num} (already saved {total_trials} trials)")
            
    # Open JSONL file
    with open(JSONL_TEMP_FILE, write_mode, encoding="utf-8") as outfile:
        while True:
            payload = {
                "pagination": {
                    "page": page_num,
                    "size": args.page_size
                },
                "searchCriteria": {
                    "containAll": "",
                    "containAny": ""
                }
            }
            
            logger.info(f"Fetching search page {page_num}...")
            try:
                RATE_LIMITER.acquire()
                resp = session.post(SEARCH_URL, json=payload, timeout=20)
                if resp.status_code != 200:
                    logger.error(f"HTTP error {resp.status_code} fetching page. Saving checkpoint.")
                    save_checkpoint(page_num, total_trials)
                    sys.exit(1)
                
                data = resp.json()
            except Exception as e:
                logger.error(f"Connection error or invalid response: {e}. Saving checkpoint.")
                save_checkpoint(page_num, total_trials)
                sys.exit(1)
                
            trials = data.get("data", [])
            if not trials:
                logger.info("No trials returned in search results. Crawl finished.")
                break
                
            # If downloading detailed data, the per-trial detail fetches are
            # independent, so run them concurrently across a bounded thread
            # pool. The global RATE_LIMITER keeps the combined request rate
            # safe. Detail fetches are gathered in the ORIGINAL page order
            # (ThreadPoolExecutor.map preserves input order) before writing,
            # so JSONL/CSV row order is identical to the sequential path.
            if args.full:
                logger.info(f"  Fetching details for {len(trials)} trials using {threads} thread(s)...")
                if threads == 1:
                    trials = [fetch_detail(session, t) for t in trials]
                else:
                    with ThreadPoolExecutor(max_workers=threads) as executor:
                        trials = list(executor.map(lambda t: fetch_detail(session, t), trials))

            for t in trials:
                outfile.write(json.dumps(t) + "\n")

            total_trials += len(trials)
            logger.info(f"  [OK] Processed page {page_num} - Saved {len(trials)} trials (Total: {total_trials:,})")
            
            # Check pagination end
            pagination_meta = data.get("pagination", {})
            total_pages = pagination_meta.get("totalPages", 1)
            
            if page_num >= total_pages:
                logger.info("Reached the last search page.")
                break
                
            if args.limit and page_num >= args.limit:
                logger.info(f"Limit of {args.limit} pages reached. Saving checkpoint and stopping.")
                save_checkpoint(page_num + 1, total_trials)
                break
                
            page_num += 1
            save_checkpoint(page_num, total_trials)
            # Pacing between search queries is handled by the global RATE_LIMITER.

    # Process conversion to CSV
    success = convert_jsonl_to_csv()
    
    if success:
        if not args.limit:
            clear_checkpoint()
            
        if not args.no_cleanup:
            if JSONL_TEMP_FILE.exists():
                try:
                    JSONL_TEMP_FILE.unlink()
                    logger.info("Cleaned up temporary JSONL file.")
                except Exception as e:
                    logger.warning(f"Could not remove temporary JSONL file: {e}")
    else:
        sys.exit(1)

if __name__ == "__main__":
    main()
