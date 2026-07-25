"""
chictr_downloader.py
====================
Automates downloading of trial registration data from the Chinese Clinical Trial Registry (ChiCTR).
Specifically targeting: https://www.chictr.org.cn/searchprojEN.html

Features:
  1. Implements "Virtual Page Sizing" (e.g., 50 trials per page). Since the ChiCTR server 
     only returns 10 items per page, a virtual page of size 50 is fetched by retrieving 5 
     consecutive server pages (each of 10 items) and merging them.
  2. Uses concurrency (ThreadPoolExecutor) to fetch multiple pages in parallel, drastically 
     speeding up execution and reducing total elapsed time.
  3. Compiles search results into a unified CSV file (Registration Number, Title, Sponsor, Type, Reg Date, Detail URL).
  4. Supports concurrent downloading of details XML files for each trial.
  5. Supports custom query URLs (with search filters), page limits, output directories, and configurable thread count.

Requirements:
    pip install requests beautifulsoup4
"""

import os
import sys
import time
import argparse
import csv
import math
import concurrent.futures
import threading
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs, urlencode
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# Resolve all output paths relative to this script's folder
BASE_DIR = Path(__file__).resolve().parent

# Standard Headers
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

DEFAULT_SEARCH_URL = (
    "https://www.chictr.org.cn/searchprojEN.html?page=1&title=&officialname=&subjectid=&regstatus=&regno=&"
    "secondaryid=&applier=&studyleader=&createyear=&sponsor=&secsponsor=&sourceofspends=&studyailment=&"
    "studyailmentcode=&studytype=&studystage=&studydesign=&recruitmentstatus=&gender=&agreetosign=&measure=&"
    "country=&province=&city=&institution=&institutionlevel=&intercode=&ethicalcommitteesanction=&"
    "whetherpublic=&minstudyexecutetime=&maxstudyexecutetime=&btngo=btn"
)


class PlaywrightFetcher:
    """
    A synchronized, thread-safe wrapper around Playwright to bypass WAF.
    """
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.page = None
        self.lock = threading.Lock()

    def start(self):
        import sys
        self.playwright = sync_playwright().start()
        headless = True
        if "--visible" in sys.argv:
            headless = False
        self.browser = self.playwright.chromium.launch(headless=headless)
        self.page = self.browser.new_page(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

    def fetch_html(self, url, retries=3, backoff=2):
        with self.lock:
            if not self.page:
                self.start()
            for attempt in range(1, retries + 1):
                try:
                    resp = self.page.goto(url, wait_until="networkidle", timeout=30000)
                    if resp and resp.status == 200:
                        class MockResponse:
                            def __init__(self, content, text, status_code):
                                self.content = content
                                self.text = text
                                self.status_code = status_code
                        content_bytes = self.page.content().encode('utf-8')
                        return MockResponse(content_bytes, self.page.content(), 200)
                    status = resp.status if resp else "No Response"
                    print(f"  [!] Playwright status {status} on attempt {attempt}/{retries} for {url}")
                except Exception as e:
                    print(f"  [!] Playwright error on attempt {attempt}/{retries}: {e}")
                time.sleep(backoff * attempt)
            return None

    def close(self):
        with self.lock:
            if self.page:
                try:
                    self.browser.close()
                    self.playwright.stop()
                except Exception:
                    pass
                self.page = None


import atexit
# Global fetcher instance
fetcher = PlaywrightFetcher()
atexit.register(fetcher.close)


def get_asp_total_count(soup):
    """
    Extracts the total number of trials from the HTML.
    """
    total_span = soup.find("span", id="data-totalEN")
    if total_span:
        try:
            return int(total_span.get_text(strip=True))
        except ValueError:
            pass
    return None


def fetch_with_retry(session, url, retries=3, backoff=2):
    """
    Fetches a URL with a simple retry mechanism.
    Routes to Playwright for HTML pages (WAF bypass) and requests for file downloads.
    """
    if "DownloadXml" in url or "DownloadFile" in url:
        for attempt in range(1, retries + 1):
            try:
                resp = session.get(url, headers=HEADERS, timeout=30)
                if resp.status_code == 200:
                    return resp
                print(f"  [!] HTTP {resp.status_code} on attempt {attempt}/{retries} for {url}")
            except Exception as e:
                print(f"  [!] Connection error on attempt {attempt}/{retries}: {e}")
            if attempt < retries:
                time.sleep(backoff * attempt)
        return None
    else:
        return fetcher.fetch_html(url, retries=retries, backoff=backoff)


def build_page_url(base_url, page_num):
    """
    Replaces or adds the 'page' parameter in the query URL.
    """
    parsed = urlparse(base_url)
    query_params = parse_qs(parsed.query)
    query_params["page"] = [str(page_num)]
    
    # Re-encode and reconstruct
    new_query = urlencode(query_params, doseq=True)
    new_url = parsed._replace(query=new_query).geturl()
    return new_url


def scrape_list_page(session, url, page_num):
    """
    Scrapes a single listing page and extracts metadata of trials.
    Returns: (list of trial dicts, total_count_from_page)
    """
    page_url = build_page_url(url, page_num)
    resp = fetch_with_retry(session, page_url)
    if not resp:
        print(f"  [Error] Failed to load list page {page_num}")
        return [], None

    soup = BeautifulSoup(resp.text, "html.parser")
    total_count = get_asp_total_count(soup)
    
    table = soup.find("table", class_="table1")
    if not table:
        return [], total_count

    trials = []
    rows = table.find_all("tr")
    for row in rows:
        tds = row.find_all("td")
        if len(tds) < 5:
            continue  # Skip header row or empty rows

        reg_no = tds[1].get_text(strip=True)
        
        # Public Title and Sponsor are in Column 2 (Index 2)
        title_a = tds[2].find("a")
        title = title_a.get_text(strip=True) if title_a else ""
        href = title_a.get("href") if title_a else ""
        
        sponsor_p = tds[2].find("p")
        sponsor = sponsor_p.get_text(strip=True) if sponsor_p else ""
        
        study_type = tds[3].get_text(strip=True)
        reg_date = tds[4].get_text(strip=True)

        detail_url = urljoin(page_url, href) if href else ""

        trials.append({
            "registration_number": reg_no,
            "public_title": title,
            "primary_sponsor": sponsor,
            "study_type": study_type,
            "registration_date": reg_date,
            "detail_url": detail_url
        })

    return trials, total_count


def scrape_physical_pages_concurrently(session, base_url, page_numbers, max_workers=5):
    """
    Fetches multiple physical pages sequentially on the main thread to avoid
    Playwright greenlet thread-switch errors, while keeping the interface compatible.
    """
    results = {}
    if not page_numbers:
        return results

    for page_num in page_numbers:
        print(f"  Fetching server page {page_num} sequentially (main thread)...")
        trials, total_count = scrape_list_page(session, base_url, page_num)
        results[page_num] = (trials, total_count)
        # Apply a minor polite delay
        time.sleep(0.5)
    return results


def resolve_xml_url_from_detail_page(detail_url):
    """
    Loads the detail HTML page via Playwright and extracts the XML download URL.
    Must be called from the main thread.
    """
    resp = fetch_with_retry(None, detail_url)
    if not resp:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    xml_href = None
    for a in soup.find_all("a", href=True):
        if "DownloadXml" in a["href"]:
            xml_href = a["href"]
            break

    if not xml_href:
        return None

    return urljoin(detail_url, xml_href)


def download_xml_file_requests(session, xml_url, reg_no, xml_dir):
    """
    Downloads the XML file using requests.
    Can be safely called concurrently from background worker threads.
    """
    xml_path = xml_dir / f"{reg_no}.xml"
    if xml_path.exists() and xml_path.stat().st_size > 0:
        return True

    resp = fetch_with_retry(session, xml_url)
    if not resp or not resp.content:
        return False

    try:
        with open(xml_path, "wb") as f:
            f.write(resp.content)
        return True
    except Exception as e:
        print(f"    [Error] Failed to write XML file for {reg_no}: {e}")
        return False


def download_details_concurrently(session, trials, xml_dir, max_workers=5):
    """
    Resolves XML download links sequentially in the main thread (WAF-safe),
    then downloads the raw XML files concurrently in background worker threads (high performance).
    """
    if not trials:
        return

    print("  Resolving XML detail download URLs sequentially (to bypass WAF safely)...")
    xml_tasks = []
    for idx, trial in enumerate(trials, 1):
        reg_no = trial["registration_number"]
        detail_url = trial["detail_url"]
        xml_path = xml_dir / f"{reg_no}.xml"

        if xml_path.exists() and xml_path.stat().st_size > 0:
            xml_tasks.append((idx, reg_no, "ALREADY_EXISTS"))
            continue

        if detail_url:
            print(f"    ({idx}/{len(trials)}) Resolving URL for {reg_no}...")
            xml_url = resolve_xml_url_from_detail_page(detail_url)
            if xml_url:
                xml_tasks.append((idx, reg_no, xml_url))
            else:
                xml_tasks.append((idx, reg_no, "FAILED_URL"))
            time.sleep(0.3)
        else:
            xml_tasks.append((idx, reg_no, "NO_LINK"))

    print(f"  Downloading XML detail files concurrently (threads={max_workers})...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_trial = {}
        for idx, reg_no, xml_url in xml_tasks:
            trial = trials[idx - 1]
            if xml_url == "ALREADY_EXISTS":
                trial["xml_file"] = f"xml_details/{reg_no}.xml"
            elif xml_url == "NO_LINK":
                trial["xml_file"] = "NO_LINK"
            elif xml_url == "FAILED_URL":
                trial["xml_file"] = "FAILED"
            else:
                future = executor.submit(download_xml_file_requests, session, xml_url, reg_no, xml_dir)
                future_to_trial[future] = (idx, trial)

        for future in concurrent.futures.as_completed(future_to_trial):
            idx, trial = future_to_trial[future]
            reg_no = trial["registration_number"]
            try:
                success = future.result()
                if success:
                    trial["xml_file"] = f"xml_details/{reg_no}.xml"
                    print(f"    ({idx}/{len(trials)}) Successfully downloaded XML for {reg_no}")
                else:
                    trial["xml_file"] = "FAILED"
                    print(f"    ({idx}/{len(trials)}) Failed to download XML for {reg_no}")
            except Exception as e:
                trial["xml_file"] = "ERROR"
                print(f"    ({idx}/{len(trials)}) Exception downloading XML for {reg_no}: {e}")


def save_csv(csv_path, trials, download_xml):
    """
    Saves the list of trials to a CSV file. It writes to a temporary file
    and renames it to prevent corruption in case of interruption.
    """
    fieldnames = ["registration_number", "public_title", "primary_sponsor", "study_type", "registration_date", "detail_url"]
    if download_xml:
        fieldnames.append("xml_file")

    unique_trials = []
    seen = set()
    for t in trials:
        reg = t.get("registration_number")
        if reg and reg not in seen:
            seen.add(reg)
            unique_trials.append(t)

    try:
        temp_csv = csv_path.with_suffix(".tmp")
        with open(temp_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for t in unique_trials:
                row_data = {k: t.get(k, "") for k in fieldnames}
                writer.writerow(row_data)
        if csv_path.exists():
            csv_path.unlink()
        temp_csv.rename(csv_path)
    except Exception as e:
        print(f"  [Error] Failed to save CSV to {csv_path}: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Scrape the Chinese Clinical Trial Registry (ChiCTR) using Virtual Page Sizes and Concurrency.",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_SEARCH_URL,
        help="The ChiCTR search results page URL to scrape from. Paste the URL containing your filters."
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=50,
        help="Virtual page size (number of trials per batch). Default is 50."
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Maximum number of VIRTUAL pages to scrape (each containing --page-size trials)."
    )
    parser.add_argument(
        "--download-xml",
        action="store_true",
        help="Visit each detail page and download the trial XML file."
    )
    parser.add_argument(
        "--output-dir",
        default=str(BASE_DIR / "chictr_trails2"),
        help="Output directory to save results. Defaults to '<script_dir>/chictr_trails2'"
    )
    parser.add_argument(
        "--visible",
        action="store_true",
        help="Run browser in headful (visible) mode to allow manual solving of WAF slider challenges."
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=5,
        help="Number of concurrent threads to use for page fetches and XML downloads. Set to 1 for sequential. Default: 5."
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Polite delay in seconds between sequential requests (only active if --threads is 1)."
    )

    args = parser.parse_args()

    # Reconfigure output encoding for UTF-8 compatibility on Windows terminal
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

    # Configure Directories
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    xml_dir = output_dir / "xml_details"
    if args.download_xml:
        xml_dir.mkdir(parents=True, exist_ok=True)

    csv_path = output_dir / "chictr_list.csv"

    print("=" * 60)
    print("  Chinese Clinical Trial Registry (ChiCTR) Downloader")
    print(f"  Target Base URL : {args.url[:80]}...")
    print(f"  Virtual Page Size: {args.page_size}")
    print(f"  Max Virtual Pages: {args.max_pages if args.max_pages else 'All Available'}")
    print(f"  Output CSV      : {csv_path}")
    print(f"  Download XML    : {'YES' if args.download_xml else 'NO'}")
    if args.download_xml:
        print(f"  XML Folder      : {xml_dir}")
    print(f"  Concurrency     : {args.threads} threads")
    print("=" * 60)

    session = requests.Session()
    all_trials = []
    virtual_page = 1
    total_trials = None
    total_virtual_pages = None
    
    # Load existing progress if CSV exists
    if csv_path.exists():
        print(f"\n[Resume] Found existing CSV at {csv_path}. Reading completed trials...")
        try:
            with open(csv_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames:
                    seen_reg_nos = set()
                    for row in reader:
                        reg_no = row.get("registration_number")
                        if reg_no and reg_no not in seen_reg_nos:
                            seen_reg_nos.add(reg_no)
                            all_trials.append(row)
            print(f"  Loaded {len(all_trials)} previously scraped trials.")
            if all_trials:
                virtual_page = (len(all_trials) // args.page_size) + 1
                print(f"  Resuming from Virtual Page {virtual_page} (starting at trial index {len(all_trials)})...")
        except Exception as e:
            print(f"  [Warning] Failed to read existing CSV (it might be empty or corrupt): {e}")

    # We will fetch the first page sequentially first if we don't know the total trials count,
    # or we can jump straight into concurrent fetching. 
    # To determine the total count, we fetch physical page 1.
    print("\n[Init] Fetching page 1 to determine total trials count...")
    first_page_trials, total_count = scrape_list_page(session, args.url, 1)
    
    if total_count is None:
        print("  [Warning] Could not determine total trials. Proceeding sequentially...")
        total_trials = len(first_page_trials)
    else:
        total_trials = total_count
        total_virtual_pages = math.ceil(total_trials / args.page_size)
        print(f"  Total registered trials in query: {total_trials:,} (~{total_virtual_pages:,} virtual pages of size {args.page_size})")

    # Keep track of what we have fetched. Physical page 1 is already fetched.
    # We store physical page data in a dictionary: page_num -> list of trials
    fetched_physical_pages = {1: first_page_trials}

    try:
        while True:
            # Check maximum pages constraint
            if args.max_pages and virtual_page > args.max_pages:
                print(f"\n[Limit reached] Stopping after virtual page limit of {args.max_pages} pages.")
                break

            # Calculate physical pages needed for this virtual page
            start_idx = (virtual_page - 1) * args.page_size
            end_idx = virtual_page * args.page_size

            # If start index exceeds total trials (if known), we are done
            if total_trials is not None and start_idx >= total_trials:
                print("\nAll available trials have been covered.")
                break

            # Calculate physical page range
            start_phys_page = (start_idx // 10) + 1
            end_phys_page = ((end_idx - 1) // 10) + 1

            print(f"\n[Virtual Page {virtual_page}] Retrieving trials {start_idx + 1} to {end_idx}...")
            print(f"  Maps to server pages {start_phys_page} to {end_phys_page}")

            # Identify which physical pages we still need to fetch
            pages_to_fetch = [p for p in range(start_phys_page, end_phys_page + 1) if p not in fetched_physical_pages]

            if pages_to_fetch:
                # Fetch missing pages concurrently
                fetched_data = scrape_physical_pages_concurrently(session, args.url, pages_to_fetch, max_workers=args.threads)
                for p_num, (trials, _) in fetched_data.items():
                    fetched_physical_pages[p_num] = trials

            # Compile trials for this virtual page
            virtual_page_trials = []
            for p_num in range(start_phys_page, end_phys_page + 1):
                virtual_page_trials.extend(fetched_physical_pages.get(p_num, []))

            # Slice the merged trials to fit the exact virtual page boundaries
            slice_start = start_idx - (start_phys_page - 1) * 10
            slice_end = slice_start + args.page_size
            sliced_virtual_trials = virtual_page_trials[slice_start:slice_end]

            if not sliced_virtual_trials:
                print("  No trials found in this range. Reached the end of results.")
                break

            print(f"  Gathered {len(sliced_virtual_trials)} trials for Virtual Page {virtual_page}.")

            # Detail Download step if requested
            if args.download_xml:
                download_details_concurrently(session, sliced_virtual_trials, xml_dir, max_workers=args.threads)
            
            all_trials.extend(sliced_virtual_trials)
            
            # Save progress incrementally
            print(f"  Saving progress incrementally ({len(all_trials)} trials total)...")
            save_csv(csv_path, all_trials, args.download_xml)
            
            # If we collected 0 trials, we have reached the end of results
            if not sliced_virtual_trials:
                print("\nReached the end of available trials (no more trials returned).")
                break
            
            # If we reached the total_trials count (if known)
            if total_trials is not None and len(all_trials) >= total_trials:
                print("\nReached the end of available trials (total count reached).")
                break

            virtual_page += 1

    except KeyboardInterrupt:
        print("\n[!] Execution interrupted by user. Saving gathered results so far...")
    
    # Save results to CSV final safety check
    if all_trials:
        save_csv(csv_path, all_trials, args.download_xml)
        
        # Count unique successfully downloaded XMLs for reporting
        unique_trials = []
        seen = set()
        for t in all_trials:
            reg = t.get("registration_number")
            if reg and reg not in seen:
                seen.add(reg)
                unique_trials.append(t)
                
        print("\n" + "=" * 60)
        print("  *  COLLECTION COMPLETE")
        print(f"  Total records collected : {len(unique_trials)}")
        print(f"  List saved to           : {csv_path}")
        if args.download_xml:
            successful_xmls = sum(1 for t in unique_trials if t.get("xml_file", "").startswith("xml_details"))
            print(f"  XML detail files saved  : {successful_xmls} in {xml_dir}")
        print("=" * 60)
    else:
        print("\n[!] No records were successfully collected.")


if __name__ == "__main__":
    main()
