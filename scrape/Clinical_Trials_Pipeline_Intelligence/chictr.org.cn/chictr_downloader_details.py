"""
chictr_downloader_details.py
============================
Consolidates ChiCTR scraped trial lists and downloads the detailed trial data concurrently using threads.
Once each XML is downloaded, it is parsed immediately in-memory, the detailed fields are extracted and appended
to a unified CSV file, and no XML is saved to disk (any existing XMLs on disk are processed, saved, and deleted).

Features:
  1. Merges 'Clinical Trials & Pipeline Intelligence/chictr_trials/chictr_list.csv' and 'chictr_trails2/chictr_list.csv' into a consolidated list.
  2. Processes any existing XML files in 'Clinical Trials & Pipeline Intelligence/chictr_trials/xml_details/', extracts their data, saves to the detailed CSV, and deletes them.
  3. Uses a ThreadPoolExecutor to concurrently download the remaining trials.
  4. Downloads XMLs directly in-memory (no disk writes for new XMLs), parses them on the fly, and appends to the CSV.
  5. Protects CSV writes with a thread lock to prevent corruption.
  6. Implements robust retry and backoff logic.
"""

import os
import sys
import time
import argparse
import csv
import threading
import concurrent.futures
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

thread_local = threading.local()

def get_thread_playwright_page():
    """
    Returns a thread-local Playwright page instance.
    Each worker thread gets its own browser process to avoid thread-switch errors.
    """
    if not hasattr(thread_local, "page") or thread_local.page is None:
        import sys
        p = sync_playwright().start()
        headless = True
        if "--visible" in sys.argv:
            headless = False
        b = p.chromium.launch(headless=headless)
        pg = b.new_page(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        thread_local.playwright = p
        thread_local.browser = b
        thread_local.page = pg
    return thread_local.page

def close_thread_playwright():
    """
    Closes the thread-local browser process cleanly.
    """
    if hasattr(thread_local, "page") and thread_local.page is not None:
        try:
            thread_local.browser.close()
            thread_local.playwright.stop()
        except Exception:
            pass
        thread_local.page = None
        thread_local.browser = None
        thread_local.playwright = None

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

CSV_FIELDS = [
    "trial_id", "utrn", "reg_name", "date_registration", "primary_sponsor", 
    "public_title", "acronym", "scientific_title", "scientific_acronym", 
    "date_enrolment", "type_enrolment", "target_size", "recruitment_status", 
    "url", "study_type", "study_design", "phase", "hc_freetext", "i_freetext", 
    "results_actual_enrolment", "results_date_completed", "results_url_link", 
    "results_summary", "results_date_posted", "results_date_first_publication", 
    "results_baseline_char", "results_participant_flow", "results_adverse_events", 
    "results_outcome_measures", "results_url_protocol", "results_IPD_plan", 
    "results_IPD_description",
    
    "scientific_contact_firstname", "scientific_contact_middlename", "scientific_contact_lastname",
    "scientific_contact_address", "scientific_contact_city", "scientific_contact_country1",
    "scientific_contact_zip", "scientific_contact_telephone", "scientific_contact_email",
    "scientific_contact_affiliation",
    
    "public_contact_firstname", "public_contact_middlename", "public_contact_lastname",
    "public_contact_address", "public_contact_city", "public_contact_country1",
    "public_contact_zip", "public_contact_telephone", "public_contact_email",
    "public_contact_affiliation",
    
    "countries", "inclusion_criteria", "exclusion_criteria", "agemin", "agemax", "gender",
    "hc_code", "hc_keyword", "i_code", "i_keyword", "prim_outcome", "sec_outcome",
    "secondary_sponsor", "source_support",
    
    "ethics_status", "ethics_approval_date", "ethics_contact_name", 
    "ethics_contact_address", "ethics_contact_phone", "ethics_contact_email"
]

# Global lock for thread-safe CSV writing
csv_write_lock = threading.Lock()

def parse_xml_content(xml_bytes):
    """
    Parses XML content and extracts a flat dictionary of detailed trial fields.
    Handles BOM and encoding issues.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        try:
            xml_str = xml_bytes.decode('utf-8-sig').strip()
            root = ET.fromstring(xml_str.encode('utf-8'))
        except Exception as e:
            raise ValueError(f"XML parsing failed: {e}")

    # The root might contain <trial> or be the <trial> itself
    trial = root.find('trial')
    if trial is None:
        trial = root
    
    data = {}
    
    def get_text(parent, path, default=""):
        node = parent.find(path)
        return node.text.strip() if node is not None and node.text else default

    # 1. Main fields
    main = trial.find('main')
    if main is not None:
        data['trial_id'] = get_text(main, 'trial_id')
        data['utrn'] = get_text(main, 'utrn')
        data['reg_name'] = get_text(main, 'reg_name')
        data['date_registration'] = get_text(main, 'date_registration')
        data['primary_sponsor'] = get_text(main, 'primary_sponsor')
        data['public_title'] = get_text(main, 'public_title')
        data['acronym'] = get_text(main, 'acronym')
        data['scientific_title'] = get_text(main, 'scientific_title')
        data['scientific_acronym'] = get_text(main, 'scientific_acronym')
        data['date_enrolment'] = get_text(main, 'date_enrolment')
        data['type_enrolment'] = get_text(main, 'type_enrolment')
        data['target_size'] = get_text(main, 'target_size')
        data['recruitment_status'] = get_text(main, 'recruitment_status')
        data['url'] = get_text(main, 'url')
        data['study_type'] = get_text(main, 'study_type')
        data['study_design'] = get_text(main, 'study_design')
        data['phase'] = get_text(main, 'phase')
        data['hc_freetext'] = get_text(main, 'hc_freetext')
        data['i_freetext'] = get_text(main, 'i_freetext')
        data['results_actual_enrolment'] = get_text(main, 'results_actual_enrolment')
        data['results_date_completed'] = get_text(main, 'results_date_completed')
        data['results_url_link'] = get_text(main, 'results_url_link')
        data['results_summary'] = get_text(main, 'results_summary')
        data['results_date_posted'] = get_text(main, 'results_date_posted')
        data['results_date_first_publication'] = get_text(main, 'results_date_first_publication')
        data['results_baseline_char'] = get_text(main, 'results_baseline_char')
        data['results_participant_flow'] = get_text(main, 'results_participant_flow')
        data['results_adverse_events'] = get_text(main, 'results_adverse_events')
        data['results_outcome_measures'] = get_text(main, 'results_outcome_measures')
        data['results_url_protocol'] = get_text(main, 'results_url_protocol')
        data['results_IPD_plan'] = get_text(main, 'results_IPD_plan')
        data['results_IPD_description'] = get_text(main, 'results_IPD_description')

    # 2. Contacts
    contacts = trial.find('contacts')
    scientific_contact = {}
    public_contact = {}
    if contacts is not None:
        for contact in contacts.findall('contact'):
            c_type = get_text(contact, 'type').lower()
            c_data = {
                'firstname': get_text(contact, 'firstname'),
                'middlename': get_text(contact, 'middlename'),
                'lastname': get_text(contact, 'lastname'),
                'address': get_text(contact, 'address'),
                'city': get_text(contact, 'city'),
                'country1': get_text(contact, 'country1'),
                'zip': get_text(contact, 'zip'),
                'telephone': get_text(contact, 'telephone'),
                'email': get_text(contact, 'email'),
                'affiliation': get_text(contact, 'affiliation')
            }
            if 'scientific' in c_type:
                scientific_contact = c_data
            elif 'public' in c_type:
                public_contact = c_data

    # Flatten contacts
    for k, v in scientific_contact.items():
        data[f'scientific_contact_{k}'] = v
    for k, v in public_contact.items():
        data[f'public_contact_{k}'] = v

    # 3. Countries
    countries = trial.find('countries')
    country_list = []
    if countries is not None:
        for c in countries.findall('country2'):
            if c.text:
                country_list.append(c.text.strip())
    data['countries'] = " | ".join(country_list)

    # 4. Criteria
    criteria = trial.find('criteria')
    if criteria is not None:
        data['inclusion_criteria'] = get_text(criteria, 'inclusion_criteria')
        data['exclusion_criteria'] = get_text(criteria, 'exclusion_criteria')
        data['agemin'] = get_text(criteria, 'agemin')
        data['agemax'] = get_text(criteria, 'agemax')
        data['gender'] = get_text(criteria, 'gender')

    # 5. Other fields
    data['hc_code'] = get_text(trial, 'health_condition_code/hc_code')
    
    # health condition keywords
    hc_keywords = []
    hc_keyword_parent = trial.find('health_condition_keyword')
    if hc_keyword_parent is not None:
        for kw in hc_keyword_parent.findall('hc_keyword'):
            if kw.text:
                hc_keywords.append(kw.text.strip())
    data['hc_keyword'] = " | ".join(hc_keywords)

    data['i_code'] = get_text(trial, 'intervention_code/i_code')
    
    # intervention keywords
    i_keywords = []
    i_keyword_parent = trial.find('intervention_keyword')
    if i_keyword_parent is not None:
        for kw in i_keyword_parent.findall('i_keyword'):
            if kw.text:
                i_keywords.append(kw.text.strip())
    data['i_keyword'] = " | ".join(i_keywords)

    data['prim_outcome'] = get_text(trial, 'primary_outcome/prim_outcome')
    data['sec_outcome'] = get_text(trial, 'secondary_outcome/sec_outcome')
    data['secondary_sponsor'] = get_text(trial, 'secondary_sponsor/sponsor_name')
    data['source_support'] = get_text(trial, 'source_support/source_name')

    # Ethics review
    ethics = trial.find('ethics_reviews/ethics_review')
    if ethics is not None:
        data['ethics_status'] = get_text(ethics, 'status')
        data['ethics_approval_date'] = get_text(ethics, 'approval_date')
        data['ethics_contact_name'] = get_text(ethics, 'contact_name')
        data['ethics_contact_address'] = get_text(ethics, 'contact_address')
        data['ethics_contact_phone'] = get_text(ethics, 'contact_phone')
        data['ethics_contact_email'] = get_text(ethics, 'contact_email')

    return data

def fetch_with_retry(session, url, retries=3, backoff=2):
    """
    Fetches a URL with simple retry and backoff mechanism.
    Routes to Playwright for HTML pages and requests for file downloads.
    """
    if "DownloadXml" in url or "DownloadFile" in url:
        for attempt in range(1, retries + 1):
            try:
                resp = session.get(url, headers=HEADERS, timeout=25)
                if resp.status_code == 200:
                    return resp
                if resp.status_code == 429:
                    time.sleep(backoff * attempt * 3)
                else:
                    time.sleep(backoff * attempt)
            except Exception:
                time.sleep(backoff * attempt)
        return None
    else:
        for attempt in range(1, retries + 1):
            try:
                pg = get_thread_playwright_page()
                resp = pg.goto(url, wait_until="networkidle", timeout=30000)
                if resp and resp.status == 200:
                    class MockResponse:
                        def __init__(self, content, text, status_code):
                            self.content = content
                            self.text = text
                            self.status_code = status_code
                    content_bytes = pg.content().encode('utf-8')
                    return MockResponse(content_bytes, pg.content(), 200)
            except Exception:
                pass
            time.sleep(backoff * attempt)
        return None

def download_and_parse_xml(session, detail_url):
    """
    Downloads and parses the XML content directly in memory.
    Returns the parsed detailed trial data dictionary, or None if failed.
    """
    # 1. Fetch detail HTML page via Playwright
    resp = fetch_with_retry(session, detail_url)
    if not resp:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    
    # 2. Extract XML download URL
    xml_href = None
    for a in soup.find_all("a", href=True):
        if "DownloadXml" in a["href"]:
            xml_href = a["href"]
            break

    if not xml_href:
        return None

    full_xml_url = urljoin(detail_url, xml_href)
    
    # 3. Fetch XML file directly in-memory via requests
    xml_resp = fetch_with_retry(session, full_xml_url)
    if not xml_resp or not xml_resp.content:
        return None

    # 4. Parse the in-memory content directly
    try:
        trial_data = parse_xml_content(xml_resp.content)
        return trial_data
    except Exception as e:
        print(f"\n[Warning] Failed to parse XML from {full_xml_url}: {e}")
        return None

def write_row_to_csv(csv_path, row_dict):
    """
    Appends a single row to the CSV file under a global thread lock.
    """
    with csv_write_lock:
        file_exists = csv_path.exists() and csv_path.stat().st_size > 0
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            if not file_exists:
                writer.writeheader()
            row_data = {k: row_dict.get(k, "") for k in CSV_FIELDS}
            writer.writerow(row_data)

def main():
    import atexit
    atexit.register(close_thread_playwright)

    parser = argparse.ArgumentParser(description="Consolidate ChiCTR list, parse existing XMLs, download missing XMLs in-memory, parse, and save to a detailed CSV.")
    parser.add_argument("--threads", type=int, default=15, help="Number of concurrent threads. Default: 15.")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of XMLs to download (for testing).")
    parser.add_argument("--visible", action="store_true", help="Run browser in visible (headful) mode to allow solving WAF challenges manually.")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

    # Paths
    base_dir = Path(r"c:\Users\LeMonde\Desktop\Biolyt_Intern")
    chictr_trials_dir = base_dir / "Test/Clinical Trials & Pipeline Intelligence/chictr_trials"
    chictr_trials_dir.mkdir(parents=True, exist_ok=True)
    
    xml_dir = chictr_trials_dir / "xml_details"
    detailed_csv = chictr_trials_dir / "chictr_detailed.csv"

    csv1 = base_dir / "Test/Clinical Trials & Pipeline Intelligence/chictr_trials" / "chictr_list.csv"
    csv2 = base_dir / "Test/chictr_trails2" / "chictr_list.csv"

    print("=" * 60)
    print("  ChiCTR Detailed Trial Downloader & Parser (In-Memory)")
    print(f"  Destination CSV: {detailed_csv}")
    print(f"  Threads        : {args.threads}")
    print("=" * 60)

    # 1. Merge basic CSVs to compile unique trials master index
    trials_dict = {}

    def load_basic_csv(path):
        if not path.exists():
            return
        print(f"Loading basic list from {path.name}...")
        try:
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    reg_no = row.get("registration_number")
                    if reg_no:
                        trials_dict[reg_no] = row
        except Exception as e:
            print(f"  [Warning] Failed to load {path.name}: {e}")

    load_basic_csv(csv1)
    load_basic_csv(csv2)
    print(f"Total unique trials in basic master index: {len(trials_dict):,}")

    # 2. Check already completed trials in the detailed CSV
    completed_trials = set()
    if detailed_csv.exists() and detailed_csv.stat().st_size > 0:
        print(f"Reading already completed trials from {detailed_csv.name}...")
        try:
            with open(detailed_csv, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    trial_id = row.get("trial_id")
                    if trial_id:
                        completed_trials.add(trial_id)
            print(f"Found {len(completed_trials):,} trials already completed in CSV.")
        except Exception as e:
            print(f"  [Warning] Failed to read detailed CSV: {e}")

    # 3. Process any existing XML files on disk first
    if xml_dir.exists():
        xml_files = list(xml_dir.glob("*.xml"))
        if xml_files:
            print(f"\nProcessing {len(xml_files):,} existing XML files on disk...")
            xml_success = 0
            for idx, xml_path in enumerate(xml_files, 1):
                reg_no = xml_path.stem
                if reg_no in completed_trials:
                    # Already in CSV, just delete the XML
                    try:
                        xml_path.unlink()
                    except Exception:
                        pass
                    continue
                
                try:
                    with open(xml_path, "rb") as f:
                        content = f.read()
                    
                    trial_data = parse_xml_content(content)
                    if trial_data and trial_data.get("trial_id"):
                        write_row_to_csv(detailed_csv, trial_data)
                        completed_trials.add(reg_no)
                        xml_success += 1
                    
                    # Delete the XML file after successful parse
                    xml_path.unlink()
                except Exception as e:
                    print(f"  [Warning] Failed to process/delete local XML {xml_path.name}: {e}")

                if idx % 1000 == 0 or idx == len(xml_files):
                    print(f"  Processed {idx}/{len(xml_files)} existing files...")
            print(f"Successfully migrated {xml_success} local XML files to the CSV and cleaned them up.")

    # 4. Identify remaining missing trials
    missing_trials = []
    for reg_no, trial in trials_dict.items():
        if reg_no not in completed_trials:
            missing_trials.append(trial)

    print(f"\nTotal trials missing details: {len(missing_trials):,}")

    if not missing_trials:
        print("All trials have been successfully downloaded and parsed! Execution complete.")
        return

    # Apply limit if specified
    if args.limit:
        print(f"Applying download limit of {args.limit} trials.")
        missing_trials = missing_trials[:args.limit]

    # 5. Download missing XMLs concurrently in-memory, parse on-the-fly, and save directly
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=args.threads * 2, pool_maxsize=args.threads * 2)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    completed_count = 0
    success_count = 0
    failed_count = 0
    start_time = time.time()

    print(f"\nStarting concurrent download and in-memory parsing of {len(missing_trials):,} trials...")

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.threads) as executor:
        future_to_trial = {}
        for trial in missing_trials:
            reg_no = trial["registration_number"]
            detail_url = trial["detail_url"]
            if detail_url:
                future = executor.submit(download_and_parse_xml, session, detail_url)
                future_to_trial[future] = trial
            else:
                failed_count += 1
                completed_count += 1

        for future in concurrent.futures.as_completed(future_to_trial):
            trial = future_to_trial[future]
            reg_no = trial["registration_number"]
            completed_count += 1

            try:
                trial_data = future.result()
                if trial_data:
                    # Add registration number as fallback if trial_id is missing in XML
                    if not trial_data.get("trial_id"):
                        trial_data["trial_id"] = reg_no
                    
                    write_row_to_csv(detailed_csv, trial_data)
                    success_count += 1
                else:
                    failed_count += 1
            except Exception:
                failed_count += 1

            # Print real-time progress
            elapsed = time.time() - start_time
            speed = completed_count / elapsed if elapsed > 0 else 0
            eta_s = (len(missing_trials) - completed_count) / speed if speed > 0 else 0
            eta_m = eta_s / 60
            pct = (completed_count / len(missing_trials)) * 100
            
            sys.stdout.write(
                f"\rProgress: {completed_count}/{len(missing_trials)} ({pct:.1f}%) | "
                f"Success: {success_count} | Failed: {failed_count} | "
                f"Speed: {speed:.1f}/s | ETA: {eta_m:.1f}m"
            )
            sys.stdout.flush()

    # Clean up browsers in all worker threads
    print("\nCleaning up thread browser instances...")
    def cleanup_thread():
        close_thread_playwright()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.threads) as cleanup_executor:
        futures = [cleanup_executor.submit(cleanup_thread) for _ in range(args.threads)]
        concurrent.futures.wait(futures)

    total_elapsed = time.time() - start_time
    print("\n" + "=" * 60)
    print("  *  DETAILED DOWNLOAD RUN COMPLETE")
    print(f"  Total processed       : {completed_count}")
    print(f"  Successfully saved    : {success_count}")
    print(f"  Failed/Errors         : {failed_count}")
    print(f"  Total elapsed time    : {total_elapsed/60:.1f} minutes")
    print(f"  Average speed         : {completed_count/total_elapsed:.2f} items/second")
    print(f"  Detailed dataset saved to: {detailed_csv}")
    print("=" * 60)

if __name__ == "__main__":
    main()
