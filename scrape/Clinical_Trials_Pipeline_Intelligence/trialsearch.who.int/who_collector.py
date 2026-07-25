import os
import sys
import time
import argparse
import csv
import xml.etree.ElementTree as ET
from pathlib import Path
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, quote

# Resolve all output paths relative to this script's folder
BASE_DIR = Path(__file__).resolve().parent

BASE_URL = "https://trialsearch.who.int/"
LIST_URL = "https://trialsearch.who.int/ListBy.aspx?TypeListing=1"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def get_asp_fields(html_content):
    """
    Parses HTML content and extracts ASP.NET postback state fields (hidden inputs)
    along with the form action.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    form = soup.find("form")
    action = form.get("action") if form else ""
    
    fields = {}
    for inp in soup.find_all("input"):
        name = inp.get("name")
        val = inp.get("value", "")
        if name and inp.get("type") != "submit":
            fields[name] = val
            
    return action, fields, soup

def get_countries(session):
    """
    Fetches the ListBy page and parses the list of countries and their advanced search links.
    Returns a list of dicts: [{'name': 'Algeria', 'url': 'AdvSearch.aspx?...'}]
    """
    print(f"Fetching country listing from {LIST_URL}...")
    resp = session.get(LIST_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    
    soup = BeautifulSoup(resp.text, "html.parser")
    
    # Countries are links of format: AdvSearch.aspx?Country=...
    links = soup.find_all("a", href=True)
    countries = []
    
    for link in links:
        href = link["href"]
        name = link.get_text(strip=True)
        if "AdvSearch.aspx?Country=" in href:
            countries.append({
                "name": name,
                "url": href
            })
            
    print(f"Found {len(countries)} countries in total.")
    return countries

def download_country_xml(session, country_name, relative_url, output_path):
    """
    Executes the 4-step ASP.NET postback sequence to download the trials XML for a country.
    """
    # 1. GET page
    search_url = urljoin(BASE_URL, relative_url)
    print(f"  [Step 1/4] GET search results page...")
    resp = session.get(search_url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    
    action, fields, soup = get_asp_fields(resp.text)
    post_url = urljoin(BASE_URL, action) if action else search_url
    
    # 2. Click "Export results to XML"
    print("  [Step 2/4] POST 'Export results to XML'...")
    fields_step2 = fields.copy()
    fields_step2["ctl00$ContentPlaceHolder1$btnLaunchDialogTerms"] = "Export results to XML"
    
    resp_step2 = session.post(post_url, data=fields_step2, headers=HEADERS, timeout=30)
    resp_step2.raise_for_status()
    
    action3, fields3, soup3 = get_asp_fields(resp_step2.text)
    post_url3 = urljoin(BASE_URL, action3) if action3 else search_url
    
    # Verify agree button exists
    if not soup3.find(id="ctl00_ContentPlaceHolder1_btnExport"):
        print("  [!] Warning: 'I agree' button not found on terms dialog.")
    
    # 3. Click 'I agree'
    print("  [Step 3/4] POST 'I agree' (terms accept)...")
    fields_step3 = fields3.copy()
    fields_step3["ctl00$ContentPlaceHolder1$btnExport"] = "I agree"
    
    resp_step3 = session.post(post_url3, data=fields_step3, headers=HEADERS, timeout=30)
    resp_step3.raise_for_status()
    
    action4, fields4, soup4 = get_asp_fields(resp_step3.text)
    post_url4 = urljoin(BASE_URL, action4) if action4 else search_url
    
    # Verify export button exists
    if not soup4.find(id="ctl00_ContentPlaceHolder1_ucExportDefault_butExportAllTrials"):
        print("  [!] Warning: 'Export all trials to XML' button not found.")
        
    # 4. Click 'Export all trials to XML'
    print("  [Step 4/4] POST 'Export all trials to XML'...")
    fields_step4 = fields4.copy()
    fields_step4["ctl00$ContentPlaceHolder1$ucExportDefault$butExportAllTrials"] = "Export all trials to XML"
    
    post_headers = HEADERS.copy()
    post_headers["Referer"] = post_url4
    
    # Export can take a while for larger countries
    resp_step4 = session.post(post_url4, data=fields_step4, headers=post_headers, timeout=120)
    resp_step4.raise_for_status()
    
    # Check that we actually got an XML download
    content_type = resp_step4.headers.get("Content-Type", "")
    content_disp = resp_step4.headers.get("Content-Disposition", "")
    
    is_xml = "xml" in content_type.lower() or "xml" in content_disp.lower() or resp_step4.content.startswith(b"<?xml")
    if not is_xml:
        raise ValueError(f"Expected XML download but got Content-Type: '{content_type}'")
        
    # Save the file
    with open(output_path, "wb") as f:
        f.write(resp_step4.content)
        
    print(f"  [OK] Successfully downloaded XML (~{len(resp_step4.content)/1024:.1f} KB) -> {output_path}")

def clean_filename(name):
    """
    Cleans country name to be safe for filenames.
    """
    return "".join(c for c in name if c.isalnum() or c in (" ", "_", "-")).strip().replace(" ", "_")

DEFAULT_FIELDS = [
    "TrialID", "Last_Refreshed_on", "Public_title", "Scientific_title", "Acronym",
    "Primary_sponsor", "Secondary_Sponsor", "Source_Register", "web_address",
    "Prospective_registration", "Date_registration", "Date_registration3",
    "Recruitment_Status", "other_records", "Inclusion_agemin", "Inclusion_agemax",
    "Inclusion_gender", "Date_enrollement", "Target_size", "Study_type",
    "Study_design", "Phase", "Countries", "Contact_Firstname", "Contact_Lastname",
    "Contact_Address", "Contact_Email", "Contact_Tel", "Contact_Affiliation",
    "Inclusion_Criteria", "Exclusion_Criteria", "Condition", "Intervention",
    "Primary_outcome", "Secondary_outcome", "Secondary_ID", "Source_Support",
    "Ethics_review_status", "Ethics_review_approval_date", "Ethics_review_contact_name",
    "Ethics_review_contact_address", "Ethics_review_contact_phone", "Ethics_review_contact_email",
    "results_date_posted", "results_url_link", "results_url_protocol", "results_date_completed",
    "results_date_first_publication", "results_summary", "results_baseline_char",
    "results_adverse_events", "results_outcome_measures", "results_ipd_plan",
    "results_ipd_description", "results_yes_no", "Export_date", "Internal_Number"
]

def convert_xmls_to_csv(xml_dir, csv_dir):
    """
    Compiles all XML files inside xml_dir into a single CSV file inside csv_dir.
    """
    if not os.path.exists(xml_dir):
        print(f"XML directory '{xml_dir}' does not exist. Cannot compile CSV.")
        return

    os.makedirs(csv_dir, exist_ok=True)
    csv_file_path = os.path.join(csv_dir, "who_trials.csv")
    print(f"\nCompiling XML files from '{xml_dir}' into a single CSV file at:\n  {csv_file_path}")

    xml_files = [f for f in os.listdir(xml_dir) if f.endswith(".xml")]
    if not xml_files:
        print("No XML files found to convert.")
        return

    # Sort files to ensure deterministic merging order
    xml_files.sort()

    start_time = time.time()
    total_trials = 0
    success_files = 0
    fail_files = 0

    with open(csv_file_path, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=DEFAULT_FIELDS, extrasaction="ignore")
        writer.writeheader()

        for filename in xml_files:
            filepath = os.path.join(xml_dir, filename)
            trial_count = 0
            try:
                context = ET.iterparse(filepath, events=("end",))
                context = iter(context)
                event, root = next(context)
                for event, elem in context:
                    if elem.tag == "Trial":
                        row = {}
                        for child in elem:
                            row[child.tag] = (child.text or "").strip()
                        writer.writerow(row)
                        trial_count += 1
                        elem.clear()
                root.clear()
                total_trials += trial_count
                success_files += 1
            except ET.ParseError as e:
                print(f"  [!] XML Parse Error in {filename}: {e}. Skipping remainder of this file.")
                fail_files += 1
            except Exception as e:
                print(f"  [!] Error processing {filename}: {e}. Skipping.")
                fail_files += 1

    duration = time.time() - start_time
    print("=" * 60)
    print("CSV CONVERSION COMPLETE")
    print(f"  Processed XML files : {len(xml_files)}")
    print(f"  Succeeded           : {success_files}")
    print(f"  Failed              : {fail_files}")
    print(f"  Total trials written: {total_trials}")
    print(f"  Time taken          : {duration:.2f} seconds")
    print(f"  CSV file size       : {os.path.getsize(csv_file_path) / (1024 * 1024):.2f} MB")
    print("=" * 60)

def main():
    parser = argparse.ArgumentParser(description="Download WHO ICTRP clinical trials XML data country-by-country.")
    parser.add_argument("--output-dir", "-o", default=str(BASE_DIR / "who_trials_xml"), help="Directory to save XML files.")
    parser.add_argument("--limit", "-l", type=int, default=None, help="Maximum number of countries to download.")
    parser.add_argument("--country", "-c", help="Download only a single specific country (exact match).")
    parser.add_argument("--delay", "-d", type=float, default=2.0, help="Delay in seconds between country downloads.")
    parser.add_argument("--csv-dir", help="Directory to save the compiled CSV file. If specified, XML files are merged into who_trials.csv.")
    parser.add_argument("--csv-only", action="store_true", help="Skip downloading and only convert existing XML files to CSV.")
    
    args = parser.parse_args()
    
    if args.csv_only:
        if not args.csv_dir:
            parser.error("--csv-only requires --csv-dir to be specified.")
        convert_xmls_to_csv(args.output_dir, args.csv_dir)
        return

    os.makedirs(args.output_dir, exist_ok=True)
    
    session = requests.Session()
    
    try:
        countries = get_countries(session)
    except Exception as e:
        print(f"Error fetching country list: {e}")
        sys.exit(1)
        
    if args.country:
        matched = [c for c in countries if c["name"].lower() == args.country.lower()]
        if not matched:
            print(f"Country '{args.country}' not found in the country list.")
            sys.exit(1)
        countries = matched
        print(f"Filtering to download single country: {countries[0]['name']}")
        
    if args.limit and not args.country:
        countries = countries[:args.limit]
        print(f"Limit applied. Downloading first {args.limit} countries.")
        
    success_count = 0
    fail_count = 0
    
    for idx, country in enumerate(countries, 1):
        name = country["name"]
        safe_name = clean_filename(name)
        file_path = os.path.join(args.output_dir, f"{safe_name}.xml")
        
        print("-" * 60)
        print(f"[{idx}/{len(countries)}] Country: {name}")
        
        # Check if already exists to enable resuming
        if os.path.exists(file_path):
            print(f"  [~] File {file_path} already exists. Skipping.")
            continue
            
        try:
            download_country_xml(session, name, country["url"], file_path)
            success_count += 1
        except Exception as e:
            print(f"  [X] Failed to download {name}: {e}")
            fail_count += 1
            
        # Respectful delay between countries
        if idx < len(countries):
            print(f"  Sleeping for {args.delay} seconds...")
            time.sleep(args.delay)
            
    print("=" * 60)
    print("DOWNLOAD TASK COMPLETE")
    print(f"  Total processed : {len(countries)}")
    print(f"  Succeeded       : {success_count}")
    print(f"  Failed          : {fail_count}")
    print("=" * 60)
    
    if args.csv_dir:
        convert_xmls_to_csv(args.output_dir, args.csv_dir)

if __name__ == "__main__":
    main()
