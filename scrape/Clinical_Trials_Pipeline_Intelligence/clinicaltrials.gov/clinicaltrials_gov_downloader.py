#!/usr/bin/env python3
"""
clinicaltrials_gov_downloader.py
================================
Downloads clinical trial details from ClinicalTrials.gov API v2.
Funnels the nested JSON response into a flattened CSV format.

Features:
- Programmatic cursor-based pagination using nextPageToken.
- Resumable crawling via local checkpointing (checkpoint.json).
- Stream-writes page results to a local JSONL file to minimize memory footprint.
- Retries with exponential backoff on network errors/rate limits.
- Flattens all primary study fields (identification, status, sponsors, design, 
  eligibility, outcomes, and locations) into a single clean CSV.
"""

import os
import sys
import json
import csv
import time
import argparse
import logging
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

# --- Configuration Constants ---
API_BASE_URL = "https://clinicaltrials.gov/api/v2/studies"
DEFAULT_PAGE_SIZE = 1000

# Setup local paths relative to the script file
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "clinicaltrials_data"
JSONL_TEMP_FILE = OUTPUT_DIR / "temp_studies.jsonl"
CHECKPOINT_FILE = OUTPUT_DIR / "checkpoint.json"
FINAL_CSV_FILE = OUTPUT_DIR / "clinicaltrials_all.csv"

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("clinicaltrials_scraper")

def get_session():
    """Returns a requests Session configured with retries and exponential backoff."""
    session = requests.Session()
    retry_strategy = Retry(
        total=5,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
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

def save_checkpoint(next_page_token, pages_crawled, total_trials):
    """Saves current crawl progress."""
    checkpoint = {
        "next_page_token": next_page_token,
        "pages_crawled": pages_crawled,
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
    """
    Flattens a single nested study JSON object into a flat dict mapping to CSV columns.
    """
    proto = study.get("protocolSection", {})
    
    # Modules
    ident = proto.get("identificationModule", {})
    status = proto.get("statusModule", {})
    sponsor = proto.get("sponsorCollaboratorsModule", {})
    oversight = proto.get("oversightModule", {})
    desc = proto.get("descriptionModule", {})
    cond = proto.get("conditionsModule", {})
    design = proto.get("designModule", {})
    elig = proto.get("eligibilityModule", {})
    outcomes = proto.get("outcomesModule", {})
    contacts_locs = proto.get("contactsLocationsModule", {})
    
    # 1. Identification
    nct_id = ident.get("nctId")
    brief_title = ident.get("briefTitle")
    official_title = ident.get("officialTitle")
    org_study_id = ident.get("orgStudyIdInfo", {}).get("id")
    
    # 2. Status
    overall_status = status.get("overallStatus")
    start_date = status.get("startDateStruct", {}).get("date")
    start_date_type = status.get("startDateStruct", {}).get("type")
    primary_completion_date = status.get("primaryCompletionDateStruct", {}).get("date")
    primary_completion_date_type = status.get("primaryCompletionDateStruct", {}).get("type")
    completion_date = status.get("completionDateStruct", {}).get("date")
    completion_date_type = status.get("completionDateStruct", {}).get("type")
    first_posted_date = status.get("studyFirstPostDateStruct", {}).get("date")
    last_update_posted_date = status.get("lastUpdatePostDateStruct", {}).get("date")
    
    # 3. Sponsors & Collaborators
    lead_sponsor = sponsor.get("leadSponsor", {}).get("name")
    lead_sponsor_class = sponsor.get("leadSponsor", {}).get("class")
    
    collabs_list = [c.get("name") for c in sponsor.get("collaborators", []) if c.get("name")]
    collaborators = " | ".join(collabs_list) if collabs_list else ""
    
    responsible_party_type = sponsor.get("responsibleParty", {}).get("type")
    responsible_party_investigator = sponsor.get("responsibleParty", {}).get("investigatorFullName")
    
    # 4. Oversight
    has_dmc = oversight.get("oversightHasDmc")
    is_fda_regulated_drug = oversight.get("isFdaRegulatedDrug")
    is_fda_regulated_device = oversight.get("isFdaRegulatedDevice")
    
    # 5. Description
    brief_summary = desc.get("briefSummary")
    detailed_description = desc.get("detailedDescription")
    
    # 6. Conditions & Keywords
    conditions = " | ".join(cond.get("conditions", []))
    keywords = " | ".join(cond.get("keywords", []))
    
    # 7. Design
    study_type = design.get("studyType")
    phases = " | ".join(design.get("phases", []))
    
    design_info = design.get("designInfo", {})
    allocation = design_info.get("allocation")
    intervention_model = design_info.get("interventionModel")
    primary_purpose = design_info.get("primaryPurpose")
    masking = design_info.get("maskingInfo", {}).get("masking")
    
    enrollment = design.get("enrollmentInfo", {}).get("count")
    enrollment_type = design.get("enrollmentInfo", {}).get("type")
    
    # 8. Arms & Interventions
    arms_module = proto.get("armsInterventionsModule", {})
    arm_list = []
    for g in arms_module.get("armGroups", []):
        label = g.get("label", "")
        g_type = g.get("type", "")
        arm_list.append(f"{label} ({g_type})" if g_type else label)
    arms = " | ".join(arm_list)
    
    interv_list = []
    for i in arms_module.get("interventions", []):
        i_type = i.get("type", "")
        name = i.get("name", "")
        interv_list.append(f"{i_type}: {name}" if i_type else name)
    interventions = " | ".join(interv_list)
    
    # 9. Outcomes
    primary_list = [o.get("measure") for o in outcomes.get("primaryOutcomes", []) if o.get("measure")]
    primary_outcomes = " | ".join(primary_list) if primary_list else ""
    
    secondary_list = [o.get("measure") for o in outcomes.get("secondaryOutcomes", []) if o.get("measure")]
    secondary_outcomes = " | ".join(secondary_list) if secondary_list else ""
    
    # 10. Eligibility
    eligibility_criteria = elig.get("eligibilityCriteria")
    sex = elig.get("sex")
    minimum_age = elig.get("minimumAge")
    maximum_age = elig.get("maximumAge")
    healthy_volunteers = elig.get("healthyVolunteers")
    
    std_ages_list = elig.get("stdAges", [])
    std_ages = " | ".join(std_ages_list) if std_ages_list else ""
    
    # 11. Locations & Contacts
    loc_list = []
    for loc in contacts_locs.get("locations", []):
        facility = loc.get("facility", "")
        city = loc.get("city", "")
        state = loc.get("state", "")
        country = loc.get("country", "")
        loc_str = facility
        geo_parts = [p for p in [city, state, country] if p]
        if geo_parts:
            loc_str += f" ({', '.join(geo_parts)})"
        loc_list.append(loc_str)
    locations = " | ".join(loc_list)
    
    contacts_list = []
    for c in contacts_locs.get("centralContacts", []):
        name = c.get("name", "")
        email = c.get("email", "")
        phone = c.get("phone", "")
        contact_parts = [p for p in [email, phone] if p]
        if name:
            contacts_list.append(f"{name} ({', '.join(contact_parts)})" if contact_parts else name)
        elif contact_parts:
            contacts_list.append(", ".join(contact_parts))
    contacts = " | ".join(contacts_list)
    
    # 12. Results Availability
    has_results = study.get("hasResults", False)
    
    return {
        "nct_id": nct_id,
        "brief_title": brief_title,
        "official_title": official_title,
        "org_study_id": org_study_id,
        "overall_status": overall_status,
        "study_type": study_type,
        "start_date": start_date,
        "start_date_type": start_date_type,
        "primary_completion_date": primary_completion_date,
        "primary_completion_date_type": primary_completion_date_type,
        "completion_date": completion_date,
        "completion_date_type": completion_date_type,
        "lead_sponsor": lead_sponsor,
        "lead_sponsor_class": lead_sponsor_class,
        "collaborators": collaborators,
        "responsible_party_type": responsible_party_type,
        "responsible_party_investigator": responsible_party_investigator,
        "has_dmc": has_dmc,
        "is_fda_regulated_drug": is_fda_regulated_drug,
        "is_fda_regulated_device": is_fda_regulated_device,
        "brief_summary": brief_summary,
        "detailed_description": detailed_description,
        "conditions": conditions,
        "keywords": keywords,
        "phases": phases,
        "allocation": allocation,
        "intervention_model": intervention_model,
        "primary_purpose": primary_purpose,
        "masking": masking,
        "enrollment": enrollment,
        "enrollment_type": enrollment_type,
        "arms": arms,
        "interventions": interventions,
        "primary_outcomes": primary_outcomes,
        "secondary_outcomes": secondary_outcomes,
        "eligibility_criteria": eligibility_criteria,
        "sex": sex,
        "minimum_age": minimum_age,
        "maximum_age": maximum_age,
        "healthy_volunteers": healthy_volunteers,
        "std_ages": std_ages,
        "contacts": contacts,
        "locations": locations,
        "first_posted_date": first_posted_date,
        "last_update_posted_date": last_update_posted_date,
        "has_results": has_results
    }

def convert_jsonl_to_csv():
    """
    Parses the temporary JSONL file line-by-line, flattens the nested structures, 
    and writes them sequentially to the final CSV file.
    """
    if not JSONL_TEMP_FILE.exists():
        logger.error(f"No downloaded data file found at {JSONL_TEMP_FILE}")
        return False
        
    logger.info(f"Converting downloaded trials from JSONL to CSV: {FINAL_CSV_FILE}")
    
    # We define the fieldnames in order
    fieldnames = [
        "nct_id", "brief_title", "official_title", "org_study_id", "overall_status", "study_type",
        "start_date", "start_date_type", "primary_completion_date", "primary_completion_date_type",
        "completion_date", "completion_date_type", "lead_sponsor", "lead_sponsor_class", "collaborators",
        "responsible_party_type", "responsible_party_investigator", "has_dmc", "is_fda_regulated_drug",
        "is_fda_regulated_device", "brief_summary", "detailed_description", "conditions", "keywords",
        "phases", "allocation", "intervention_model", "primary_purpose", "masking", "enrollment",
        "enrollment_type", "arms", "interventions", "primary_outcomes", "secondary_outcomes",
        "eligibility_criteria", "sex", "minimum_age", "maximum_age", "healthy_volunteers", "std_ages",
        "contacts", "locations", "first_posted_date", "last_update_posted_date", "has_results"
    ]
    
    try:
        # Create parent directory if needed
        FINAL_CSV_FILE.parent.mkdir(parents=True, exist_ok=True)
        
        # Read JSONL line-by-line and write to CSV row-by-row
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

def main():
    parser = argparse.ArgumentParser(description="ClinicalTrials.gov API v2 Scraper")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of pages to download (for testing)")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE, help="Number of records per page (default: 1000)")
    parser.add_argument("--resume", action="store_true", default=True, help="Resume download from checkpoint (default: True)")
    parser.add_argument("--no-resume", dest="resume", action="store_false", help="Ignore existing checkpoints")
    parser.add_argument("--no-cleanup", action="store_true", help="Keep the temporary JSONL download file after merge")
    parser.add_argument("--csv-only", action="store_true", help="Only build CSV from the existing JSONL data, without calling the API")
    parser.add_argument("--query", type=str, default=None, help="Search query filter (e.g. condition name)")
    
    args = parser.parse_args()
    
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    if args.csv_only:
        success = convert_jsonl_to_csv()
        sys.exit(0 if success else 1)
        
    session = get_session()
    
    # Resuming logic
    next_page_token = None
    pages_crawled = 0
    total_trials = 0
    write_mode = "w"
    
    if args.resume:
        checkpoint = load_checkpoint()
        if checkpoint:
            next_page_token = checkpoint.get("next_page_token")
            pages_crawled = checkpoint.get("pages_crawled", 0)
            total_trials = checkpoint.get("total_trials", 0)
            write_mode = "a"
            logger.info(f"Resuming crawl from page {pages_crawled + 1} using token: {next_page_token}")
            
    # Open JSONL in write or append mode
    with open(JSONL_TEMP_FILE, write_mode, encoding="utf-8") as outfile:
        while True:
            params = {
                "pageSize": args.page_size
            }
            if next_page_token:
                params["pageToken"] = next_page_token
            if args.query:
                params["query.cond"] = args.query
                
            logger.info(f"Fetching page {pages_crawled + 1} (pageSize={args.page_size})...")
            
            try:
                resp = session.get(API_BASE_URL, params=params, timeout=30)
                if resp.status_code != 200:
                    logger.error(f"HTTP error {resp.status_code} fetching page. Saving checkpoint.")
                    save_checkpoint(next_page_token, pages_crawled, total_trials)
                    sys.exit(1)
                    
                data = resp.json()
            except Exception as e:
                logger.error(f"Connection error or invalid response: {e}. Saving checkpoint.")
                save_checkpoint(next_page_token, pages_crawled, total_trials)
                sys.exit(1)
                
            studies = data.get("studies", [])
            if not studies:
                logger.info("No studies returned in page. Crawl finished.")
                break
                
            # Write page to JSONL incrementally
            for study in studies:
                outfile.write(json.dumps(study) + "\n")
                
            total_trials += len(studies)
            pages_crawled += 1
            logger.info(f"  [OK] Saved {len(studies)} trials (Total: {total_trials:,})")
            
            # Fetch next page token
            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                logger.info("Crawl complete. No more page tokens.")
                break
                
            # Check limits
            if args.limit and pages_crawled >= args.limit:
                logger.info(f"Limit of {args.limit} pages reached. Saving checkpoint and stopping.")
                save_checkpoint(next_page_token, pages_crawled, total_trials)
                break
                
            # Save checkpoint at the end of every page for safety
            save_checkpoint(next_page_token, pages_crawled, total_trials)
            
            # Polite throttle delay
            time.sleep(0.5)
            
    # Process conversion to CSV
    success = convert_jsonl_to_csv()
    
    if success:
        # If successfully crawled to the end (no break early due to limit), clear the checkpoint
        if not args.limit:
            clear_checkpoint()
            
        # Cleanup temporary files
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
