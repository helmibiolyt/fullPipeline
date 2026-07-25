#!/usr/bin/env python3
"""
jrct_downloader.py
========================
Automates downloading of trial registration data from the Japan Registry of Clinical Trials (jRCT).
Specifically targeting:
  Search list: https://jrct.mhlw.go.jp/search?language=en&searched=1&page=1&dis_op=0&free_op=1
  Details page: https://jrct.mhlw.go.jp/en-latest-detail/jRCT1042260071

Features:
  1. Iterates through search results pages to collect all Trial IDs.
  2. Visits each detail page, parses the structured HTML tables (handling rowspan/colspan).
  3. Flattens data fields into a dictionary with proper section prefixes to avoid key collision.
  4. Saves all trials to a unified CSV file in the output directory.
  5. Includes error retries, exponential backoffs, and respectful request delays.

Requirements:
    pip install requests beautifulsoup4
"""

import os
import sys
import time
import argparse
import csv
import random
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode
import requests
from bs4 import BeautifulSoup
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# Reconfigure output encoding for UTF-8 compatibility on Windows terminal
try:
    sys.stdout.reconfigure(encoding='utf-8')
except AttributeError:
    pass

class BlockedException(Exception):
    """
    Raised when the server blocks requests (e.g. returns HTTP 403 or 429).
    """
    pass


class FetchException(Exception):
    """
    Raised when a page fails to load due to HTTP errors other than blocking (e.g. 500, 404).
    """
    pass


# Try to dynamically load curl_cffi for TLS impersonation bypasses
try:
    from curl_cffi import requests as requests_cffi
except ImportError:
    requests_cffi = None


# Global synchronization lock for modifying proxy pool or executing VPN rotation
rotation_lock = threading.Lock()
last_vpn_rotation_time = 0.0

# Thread-local storage to hold persistent sessions for each worker thread
thread_local = threading.local()


# Define Browser Profiles containing matching TLS fingerprints, User-Agents, and headers
BROWSER_PROFILES = [
    {
        "name": "Chrome (Windows)",
        "impersonate": "chrome",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "headers": {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "en-US,en;q=0.9,ja;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "DNT": "1",
            "Sec-Ch-Ua": '"Google Chrome";v="124", "Chromium";v="124", "Not-A.Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"'
        }
    },
    {
        "name": "Firefox (Windows)",
        "impersonate": "firefox",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
        "headers": {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5,ja;q=0.3",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "DNT": "1"
        }
    },
    {
        "name": "Edge (Windows)",
        "impersonate": "edge",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
        "headers": {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "en-US,en;q=0.9,ja;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "DNT": "1",
            "Sec-Ch-Ua": '"Edge";v="124", "Chromium";v="124", "Not-A.Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"'
        }
    },
    {
        "name": "Safari (macOS)",
        "impersonate": "safari",
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        "headers": {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,ja;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none"
        }
    }
]


def build_profile_session(profile, proxy=None, impersonate_enabled=True):
    """
    Creates and configures a session object (curl_cffi or standard requests)
    pre-configured with specific browser profile headers and TLS fingerprints.
    """
    if impersonate_enabled and requests_cffi:
        session = requests_cffi.Session()
        session.impersonate = profile["impersonate"]
    else:
        session = requests.Session()
        
    # Copy headers into session
    headers = profile["headers"].copy()
    headers["User-Agent"] = profile["user_agent"]
    session.headers.update(headers)
    
    if proxy:
        session.proxies = {
            "http": proxy,
            "https": proxy
        }
    return session


def get_thread_session(proxies_list=None, impersonate_enabled=True, static_proxy=None):
    """
    Retrieves or initializes a thread-local persistent Session.
    Ensures that each concurrent thread acts as a unique browser instance.
    """
    if not hasattr(thread_local, "session"):
        profile = random.choice(BROWSER_PROFILES)
        proxy = static_proxy
        if not proxy and proxies_list:
            with rotation_lock:
                if proxies_list:
                    proxy = random.choice(proxies_list)
        
        session = build_profile_session(profile, proxy, impersonate_enabled)
        thread_local.session = session
        thread_local.profile = profile
        thread_local.proxy = proxy
        print(f"  [Thread {threading.current_thread().name}] Spawned independent session: {profile['name']} (Proxy: {proxy})")
    return thread_local.session


# Resolve all output paths relative to this script's folder
BASE_DIR = Path(__file__).resolve().parent

DEFAULT_SEARCH_URL = "https://jrct.mhlw.go.jp/search?language=en&searched=1&page=1&dis_op=0&free_op=1"


def fetch_with_retry(session, url, proxies_list=None, vpn_rotate_cmd=None, retries=3, backoff=5, timeout=60, referer=None, cool_down=300):
    """
    Fetches a URL with a retry mechanism, exponential backoff, optional proxy rotation, or VPN rotation.
    When a proxy pool is active, dynamically retries across as many proxies as needed.
    This function is thread-safe and can be called concurrently.
    Mimics a real browser by generating realistic dynamic headers and introducing jitter to avoid WAF blocks.
    """
    global last_vpn_rotation_time
    
    # Introduce a random delay between 7.0 and 15.0 seconds as requested by the user
    sleep_time = random.uniform(7.0, 15.0)
    print(f"      Sleeping {sleep_time:.2f} seconds before request...")
    time.sleep(sleep_time)
    
    # When a proxy pool is active, try every proxy in the pool before giving up
    effective_retries = max(retries, 3)
    if proxies_list:
        effective_retries = max(effective_retries, len(proxies_list))

    last_status_code = None

    for attempt in range(1, effective_retries + 1):
        if referer:
            session.headers["Referer"] = referer
        else:
            session.headers.pop("Referer", None)
            
        try:
            # Shorter per-proxy timeout so dead proxies in a rotating LIST fail fast.
            # A single static proxy (e.g. Tor SOCKS) is higher-latency but reliable, so
            # it must use the full timeout — capping it at 5s makes Tor look "dead".
            effective_timeout = min(timeout, 5) if proxies_list else timeout
            
            resp = session.get(url, timeout=effective_timeout)
            last_status_code = resp.status_code
            if resp.status_code == 200:
                return resp
            print(f"  [!] HTTP {resp.status_code} on attempt {attempt}/{effective_retries} for {url}")
            
            # WAF/Rate limit block
            if resp.status_code in [403, 429]:
                current_proxy = session.proxies.get("http") if session.proxies else None
                if current_proxy and proxies_list:
                    print(f"      [Thread {threading.current_thread().name}] Proxy {current_proxy} blocked (403/429). Rotating proxy...")
                    with rotation_lock:
                        try:
                            if current_proxy in proxies_list:
                                proxies_list.remove(current_proxy)
                                print(f"      Removed blocked proxy. {len(proxies_list)} remaining.")
                        except ValueError:
                            pass
                        if proxies_list:
                            new_proxy = random.choice(proxies_list)
                            session.proxies = {"http": new_proxy, "https": new_proxy}
                            print(f"      Assigned new proxy to thread: {new_proxy}")
                        else:
                            session.proxies = {}
                elif vpn_rotate_cmd:
                    print(f"      Access denied (403/429). Executing VPN rotation...")
                    import subprocess
                    with rotation_lock:
                        current_time = time.time()
                        if current_time - last_vpn_rotation_time > 30:
                            try:
                                subprocess.run(vpn_rotate_cmd, shell=True, check=True)
                                print("      VPN command executed. Waiting 15s for connection to stabilize...")
                                time.sleep(15)
                                last_vpn_rotation_time = time.time()
                            except Exception as ve:
                                print(f"      [VPN Error] Rotation command failed: {ve}")
                        else:
                            print("      VPN was recently rotated by another thread. Skipping duplicate rotation.")
                else:
                    print(f"      [Thread {threading.current_thread().name}] WAF Block detected (403/429) on attempt {attempt}/{effective_retries}. cooling down for {cool_down}s...")
                    time.sleep(cool_down)
                
        except Exception as e:
            current_proxy = session.proxies.get("http") if session.proxies else None
            if current_proxy and proxies_list:
                print(f"      [Thread {threading.current_thread().name}] Connection error with proxy {current_proxy}: {e}. Rotating proxy...")
                with rotation_lock:
                    try:
                        if current_proxy in proxies_list:
                            proxies_list.remove(current_proxy)
                            print(f"      Dead proxy removed. {len(proxies_list)} remaining.")
                    except ValueError:
                        pass
                    if proxies_list:
                        new_proxy = random.choice(proxies_list)
                        session.proxies = {"http": new_proxy, "https": new_proxy}
                    else:
                        session.proxies = {}
            elif vpn_rotate_cmd:
                print(f"      Connection failure. Executing VPN rotation...")
                import subprocess
                with rotation_lock:
                    current_time = time.time()
                    if current_time - last_vpn_rotation_time > 30:
                        try:
                            subprocess.run(vpn_rotate_cmd, shell=True, check=True)
                            print("      VPN command executed. Waiting 15s for connection to stabilize...")
                            time.sleep(15)
                            last_vpn_rotation_time = time.time()
                        except Exception as ve:
                            print(f"      [VPN Error] Rotation command failed: {ve}")
                    else:
                        print("      VPN was recently rotated by another thread. Skipping duplicate rotation.")
            else:
                print(f"  [!] Connection error on attempt {attempt}/{effective_retries}: {e}")
                if attempt < effective_retries:
                    sleep_time = backoff * attempt
                    print(f"      Waiting {sleep_time}s before retrying...")
                    time.sleep(sleep_time)
            continue

    if last_status_code in [403, 429]:
        raise BlockedException(f"The server blocked access (HTTP {last_status_code}).")
    elif last_status_code:
        raise FetchException(f"Server returned HTTP {last_status_code}.")
    else:
        raise FetchException("Exhausted all retries due to connection errors.")


def parse_html_table(table):
    """
    Parses an HTML table into a 2D grid of strings, correctly
    accounting for 'rowspan' and 'colspan' attributes.
    """
    rows = table.find_all('tr')
    num_rows = len(rows)
    grid = [[] for _ in range(num_rows)]
    
    for r_idx, row in enumerate(rows):
        cells = row.find_all(['td', 'th'])
        c_idx = 0
        for cell in cells:
            rowspan = int(cell.get('rowspan', 1))
            colspan = int(cell.get('colspan', 1))
            text = cell.get_text(" ", strip=True)
            
            # Find the next empty slot in the row grid
            while c_idx < len(grid[r_idx]) and grid[r_idx][c_idx] is not None:
                c_idx += 1
                
            # Fill the grid cells spanned by rowspan/colspan
            for r in range(rowspan):
                for c in range(colspan):
                    target_row = r_idx + r
                    target_col = c_idx + c
                    if target_row < num_rows:
                        while len(grid[target_row]) <= target_col:
                            grid[target_row].append(None)
                        grid[target_row][target_col] = text
            c_idx += colspan
            
    # Convert any remaining None values to empty strings
    return [[cell or '' for cell in row] for row in grid]


def scrape_search_page(session, base_url, page_num, proxies_list=None, vpn_rotate_cmd=None, timeout=60, impersonate_enabled=True, cool_down=300, static_proxy=None):
    """
    Scrapes a search result listing page and extracts Trial IDs.
    Simulates organic referer navigation.
    """
    parsed = urlparse(base_url)
    query_params = parse_qs(parsed.query)
    query_params["page"] = [str(page_num)]
    
    # Reconstruct URL with the target page number
    new_query = urlencode(query_params, doseq=True)
    page_url = parsed._replace(query=new_query).geturl()
    
    # Simulate realistic referer: page 1 came from home, page 2+ came from the previous page
    referer = "https://jrct.mhlw.go.jp/" if page_num == 1 else f"https://jrct.mhlw.go.jp/search?language=en&searched=1&page={page_num-1}&dis_op=0&free_op=1"
    
    print(f"\n[Search Page {page_num}] Fetching list: {page_url}")
    sess = session or get_thread_session(proxies_list, impersonate_enabled, static_proxy)
    try:
        resp = fetch_with_retry(sess, page_url, proxies_list=proxies_list, vpn_rotate_cmd=vpn_rotate_cmd, timeout=timeout, referer=referer, cool_down=cool_down)
    except FetchException as fe:
        print(f"  [Error] Search page {page_num} failed with error: {fe}")
        return None
    if not resp:
        print(f"  [Error] Failed to load search page {page_num}")
        return None
        
    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table")
    if not table:
        print(f"  [Warning] No table found on search page {page_num}. Ending pagination.")
        return []
        
    trial_ids = []
    rows = table.find_all("tr")
    for row in rows:
        tds = row.find_all("td")
        if not tds:
            continue
        
        # Trial ID is located in the first cell (index 0)
        trial_id = tds[0].get_text(strip=True)
        if trial_id.startswith("jRCT"):
            trial_ids.append(trial_id)
            
    return trial_ids


def scrape_detail_page(session, trial_id, proxies_list=None, vpn_rotate_cmd=None, timeout=60, referer="https://jrct.mhlw.go.jp/search?language=en&searched=1&page=1&dis_op=0&free_op=1", impersonate_enabled=True, cool_down=300, static_proxy=None):
    """
    Downloads and parses a trial's detail page, converting structured
    tables into flat key-value pairs.
    Simulates organic referer navigation from the search listings page.
    """
    detail_url = f"https://jrct.mhlw.go.jp/en-latest-detail/{trial_id}"
    sess = session or get_thread_session(proxies_list, impersonate_enabled, static_proxy)
    try:
        from urllib.parse import quote
        safe_detail_url = f"https://jrct.mhlw.go.jp/en-latest-detail/{quote(trial_id)}"
        resp = fetch_with_retry(sess, safe_detail_url, proxies_list=proxies_list, vpn_rotate_cmd=vpn_rotate_cmd, timeout=timeout, referer=referer, cool_down=cool_down)
    except FetchException as fe:
        print(f"  [Warning] Trial {trial_id} detail page failed with error: {fe}. Skipping record.")
        return None
    if not resp:
        print(f"  [Error] Failed to fetch details for {trial_id}")
        return None
        
    soup = BeautifulSoup(resp.text, "html.parser")
    tables = soup.find_all("table")
    
    data_dict = {
        "Trial ID": trial_id,
        "Detail URL": detail_url
    }
    
    for table in tables:
        grid = parse_html_table(table)
        if not grid or len(grid) == 0:
            continue
            
        # Skip the revision history table which is table-meta of the registry itself
        if len(grid[0]) >= 2 and grid[0][0].strip() == 'No' and grid[0][1].strip() == 'Publication date':
            continue
            
        section_prefix = None
        for row in grid:
            if len(row) == 2:
                key = row[0].strip()
                val = row[1].strip()
                if not key:
                    continue
                
                # Context-aware prefix mapping to avoid key collision
                if key == 'Name of Certified Review Board':
                    section_prefix = 'Certified Review Board'
                    data_dict['Certified Review Board - Name'] = val
                elif section_prefix and key in ['Address', 'Telephone', 'E-Mail', 'Approval Status', 'Date of approval']:
                    data_dict[f"{section_prefix} - {key}"] = val
                else:
                    if key in data_dict and data_dict[key] != val:
                        suffix = 2
                        while f"{key} ({suffix})" in data_dict:
                            suffix += 1
                        data_dict[f"{key} ({suffix})"] = val
                    else:
                        data_dict[key] = val
                        
            elif len(row) == 3:
                key1 = row[0].strip()
                key2 = row[1].strip()
                val = row[2].strip()
                if key1 == key2:
                    data_dict[key1] = val
                else:
                    data_dict[f"{key1} - {key2}"] = val
                    
            elif len(row) >= 4:
                key1 = row[0].strip()
                key2 = row[1].strip()
                key3 = row[2].strip()
                val = " ".join(row[3:]).strip()
                data_dict[f"{key1} - {key2} - {key3}"] = val
                
    return data_dict


def test_proxy(proxy, test_url="https://jrct.mhlw.go.jp/", timeout=5):
    """
    Tests a single proxy with a fast timeout. Returns the proxy string if it works, else None.
    """
    profile = random.choice(BROWSER_PROFILES)
    headers = profile["headers"].copy()
    headers["User-Agent"] = profile["user_agent"]
    
    try:
        if requests_cffi:
            resp = requests_cffi.get(
                test_url,
                proxies={"http": proxy, "https": proxy},
                headers=headers,
                impersonate=profile["impersonate"],
                timeout=timeout
            )
        else:
            resp = requests.get(
                test_url,
                proxies={"http": proxy, "https": proxy},
                headers=headers,
                timeout=timeout
            )
        # Only accept genuine success/redirect — 403/429 means proxy IP is already blocked by jRCT
        if resp.status_code in [200, 301, 302]:
            return proxy
    except Exception:
        pass
    return None


def fetch_free_proxies(validate=True, max_workers=50):
    """
    Automatically fetches a list of free public HTTP/HTTPS proxies from public APIs,
    then concurrently validates them to keep only the live ones.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    print("\n>>> Fetching free public proxies for rotation...")
    urls = [
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000&country=all&ssl=all&anonymity=all",
        "https://www.proxy-list.download/api/v1/get?type=https",
        "https://www.proxy-list.download/api/v1/get?type=http",
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
        "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    raw_proxies = []
    for url in urls:
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                for line in resp.text.splitlines():
                    line = line.strip()
                    if line and ":" in line and not line.startswith("#"):
                        raw_proxies.append(f"http://{line}")
        except Exception:
            pass

    raw_proxies = list(set(raw_proxies))
    print(f"    Collected {len(raw_proxies)} raw proxies. Validating concurrently (this may take ~30s)...")

    if not validate:
        return raw_proxies

    live_proxies = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(test_proxy, p): p for p in raw_proxies}
        done = 0
        for future in as_completed(futures):
            done += 1
            result = future.result()
            if result:
                live_proxies.append(result)
            # Print progress every 100 tested
            if done % 100 == 0:
                print(f"    Tested {done}/{len(raw_proxies)}... {len(live_proxies)} live proxies so far.")

    print(f"    Validation complete: {len(live_proxies)} live proxies out of {len(raw_proxies)} tested.")
    return live_proxies


def main():
    parser = argparse.ArgumentParser(
        description="Scrape and extract trial records from the Japan Registry of Clinical Trials (jRCT).",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_SEARCH_URL,
        help="Initial jRCT search result listing page URL containing query filters."
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Maximum search result pages to crawl (50 trials per page)."
    )
    parser.add_argument(
        "--output-dir",
        default=str(BASE_DIR / "jrct_trials"),
        help="Target folder to save output CSV. Defaults to '<script_dir>/jrct_trials'."
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Polite wait delay in seconds between details page requests (default: 1.0s)."
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Connection and read timeout in seconds (default: 60s)."
    )
    parser.add_argument(
        "--max-trials",
        type=int,
        default=None,
        help="Maximum number of trial detail pages to fetch and parse."
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=5,
        help="Number of detail pages to scrape concurrently in each batch (default: 5)."
    )
    parser.add_argument(
        "--proxy",
        default=None,
        help="Proxy URL (e.g. 'http://user:pass@ip:port' or SOCKS5 'socks5h://127.0.0.1:9050')."
    )
    parser.add_argument(
        "--proxy-file",
        default=None,
        help="File path containing a list of proxies (one proxy per line) to rotate."
    )
    parser.add_argument(
        "--auto-proxy",
        action="store_true",
        help="Automatically fetch a list of free public proxies to use for rotation."
    )
    parser.add_argument(
        "--vpn-rotate",
        default=None,
        help="Shell command to run to rotate your VPN IP address when blocked (e.g. 'nordvpn connect')."
    )
    parser.add_argument(
        "--no-impersonate",
        action="store_true",
        help="Force standard requests library instead of curl_cffi impersonation."
    )
    parser.add_argument(
        "--cool-down",
        type=int,
        default=300,
        help="Cool-down wait time in seconds when WAF blocks requests (default: 300s)."
    )

    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "jrct_list.csv"

    # Load existing records for resume functionality
    existing_records = []
    scraped_ids = set()
    skipped_ids = set()
    if csv_path.exists():
        try:
            with open(csv_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames:
                    for row in reader:
                        clean_row = {k: (v or "").strip() for k, v in row.items() if k}
                        trial_id = clean_row.get("Trial ID")
                        if trial_id:
                            existing_records.append(clean_row)
                            scraped_ids.add(trial_id)
            print(f"Loaded {len(existing_records)} existing records from {csv_path}. Resume mode active.")
        except Exception as e:
            print(f"[Warning] Failed to read existing CSV {csv_path} ({e}). Starting fresh.")

    print("=" * 70)
    print("  Japan Registry of Clinical Trials (jRCT) Downloader")
    print(f"  Initial Search URL : {args.url}")
    print(f"  Output CSV         : {csv_path}")
    print(f"  Polite Delay       : {args.delay}s")
    print(f"  Timeout            : {args.timeout}s")
    if requests_cffi and not args.no_impersonate:
        print("  TLS Impersonation  : Enabled (using curl_cffi)")
    else:
        print("  TLS Impersonation  : Disabled (using standard requests)")
    print(f"  Cool-down Time     : {args.cool_down}s")
    if args.max_pages:
        print(f"  Max Search Pages   : {args.max_pages} (~{args.max_pages * 50} trials)")
    if args.max_trials:
        print(f"  Max Trials to Fetch: {args.max_trials}")
    print("=" * 70)

    # Load and configure proxies
    proxies_list = []
    if args.proxy:
        proxies_list.append(args.proxy)
    if args.proxy_file:
        p_path = Path(args.proxy_file)
        if p_path.exists():
            with open(p_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        proxies_list.append(line)
            print(f"Loaded {len(proxies_list)} proxies from {args.proxy_file}")
        else:
            print(f"[Warning] Proxy file {args.proxy_file} not found.")

    if args.auto_proxy:
        free_proxies = fetch_free_proxies()
        proxies_list.extend(free_proxies)

    # Remove duplicates from list keeping order
    seen = set()
    proxies_list = [p for p in proxies_list if not (p in seen or seen.add(p))]

    static_proxy = None
    
    # If using a single static proxy (e.g. a Tor SOCKS endpoint), store it AND clear
    # the rotating pool: a static proxy must not be treated as a throwaway pool entry
    # (which caps its timeout at 5s and drops it from rotation on the first slow
    # response — fatal for higher-latency proxies like Tor).
    if len(proxies_list) == 1:
        static_proxy = proxies_list[0]
        proxies_list = []
        print(f"Configured static proxy: {static_proxy}")

    all_trial_ids = []
    ids_cache_path = output_dir / "jrct_ids.txt"  # Persisted ID list for resume
    ids_collection_complete = False

    # Resume search ID collection from saved cache if it exists
    if ids_cache_path.exists():
        try:
            with open(ids_cache_path, "r", encoding="utf-8") as f:
                cached_lines = [l.strip() for l in f if l.strip()]
            # Last line is a sentinel when collection finished cleanly
            if cached_lines and cached_lines[-1] == "__COMPLETE__":
                all_trial_ids = cached_lines[:-1]
                ids_collection_complete = True
                print(f"Loaded {len(all_trial_ids)} Trial IDs from cache (collection was complete).")
            else:
                all_trial_ids = cached_lines
                print(f"Loaded {len(all_trial_ids)} Trial IDs from partial cache. Resuming search page collection...")
        except Exception as e:
            print(f"[Warning] Could not read ID cache ({e}). Starting fresh.")

    # Convert scraped_ids to a set for fast lookup
    all_scraped_records = list(existing_records)
    scraped_ids = {row.get("Trial ID") for row in all_scraped_records if row.get("Trial ID")}

    # Calculate starting search page based on already cached IDs
    # (Since each search page yields exactly 50 IDs, page is len(all_trial_ids) // 50 + 1)
    search_page = (len(all_trial_ids) // 50) + 1

    print("=" * 70)
    print(">>> PIPELINED CONCURRENT SCRAPING STARTED")
    print(f"    Already scraped in CSV: {len(scraped_ids)}")
    print(f"    Already discovered IDs : {len(all_trial_ids)}")
    print("=" * 70)

    # Helper function to save the CSV file dynamically
    def save_csv():
        if not all_scraped_records:
            return
        # Dynamically compile fieldnames
        all_keys = set()
        for trial in all_scraped_records:
            all_keys.update(trial.keys())
            
        first_cols = [
            "Trial ID",
            "Scientific Title",
            "Public Title",
            "Recruitment status",
            "Date of registration",
            "Last modified on",
            "Study Type",
            "Detail URL"
        ]
        first_cols = [c for c in first_cols if c in all_keys]
        other_cols = sorted(list(all_keys - set(first_cols)))
        fieldnames = first_cols + other_cols
        
        try:
            # Write to a temp file first, then rename to prevent corruption
            temp_path = csv_path.with_suffix(".tmp")
            with open(temp_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for trial in all_scraped_records:
                    row_data = {k: trial.get(k, "") for k in fieldnames}
                    writer.writerow(row_data)
            
            # Atomic rename
            if csv_path.exists():
                csv_path.unlink()
            temp_path.rename(csv_path)
            print(f"    [Save] CSV successfully updated. Total records: {len(all_scraped_records)}")
        except Exception as csv_err:
            print(f"    [Save Error] Failed to write CSV file: {csv_err}")

    # Special initialization if starting completely fresh
    if not ids_collection_complete and len(all_trial_ids) == 0:
        print("\n>>> Fresh Start: Fetching Search Page 1 to discover initial Trial IDs...")
        try:
            page_ids = scrape_search_page(
                None,
                args.url,
                1,
                proxies_list=proxies_list,
                vpn_rotate_cmd=args.vpn_rotate,
                timeout=args.timeout,
                impersonate_enabled=(not args.no_impersonate),
                cool_down=args.cool_down,
                static_proxy=static_proxy
            )
            if page_ids:
                all_trial_ids.extend(page_ids)
                print(f"    Collected {len(page_ids)} initial Trial IDs from Page 1.")
                with open(ids_cache_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(all_trial_ids))
                search_page = 2
                
                # Wait 5 seconds after collecting page 1
                print(f"    Waiting 5 seconds before entering pipelined concurrency...")
                time.sleep(5.0)
            else:
                print("    [Warning] No Trial IDs found on Search Page 1.")
                ids_collection_complete = True
        except BlockedException as e:
            print(f"\n[!] Block Detected during initial Page 1 fetch: {e}")
            print("    Exiting. Please change your VPN/IP before restarting.")
            return

    try:
        batch_index = 1
        while True:
            # Determine which trials need to be fetched in this batch
            unscraped_ids = [tid for tid in all_trial_ids if tid not in scraped_ids and tid not in skipped_ids]
            
            # Apply max_trials limit if specified
            if args.max_trials:
                # Calculate how many more we are allowed to fetch
                already_fetched_new = len(all_scraped_records) - len(existing_records)
                remaining_quota = args.max_trials - already_fetched_new
                if remaining_quota <= 0:
                    print(f"\n[Limit] Reached specified max trials quota of {args.max_trials}. Stopping.")
                    break
                # Truncate unscraped_ids to stay within remaining quota
                unscraped_ids = unscraped_ids[:remaining_quota]
                
            batch_to_fetch = unscraped_ids[:args.batch_size]
            
            # Determine if we should also request the next search page
            fetch_search = False
            if not ids_collection_complete:
                if not args.max_pages or search_page <= args.max_pages:
                    fetch_search = True
                else:
                    print(f"\n[Limit] Reached search page limit of {args.max_pages}. Stopping search collection.")
                    ids_collection_complete = True
                    with open(ids_cache_path, "w", encoding="utf-8") as f:
                        f.write("\n".join(all_trial_ids) + "\n__COMPLETE__")

            # Check termination condition:
            if not batch_to_fetch and not fetch_search:
                print("\n>>> All trials have been successfully scraped and search collection is complete.")
                break

            if args.batch_size == 1:
                print(f"\n>>> [Batch {batch_index}] Processing sequential request...")
                if fetch_search:
                    print(f"    - Requesting Search Page {search_page}")
                if batch_to_fetch:
                    print(f"    - Requesting trial details: {', '.join(batch_to_fetch)}")

                new_page_ids = None
                scraped_details_batch = []
                search_blocked = False
                detail_blocked = False

                if fetch_search:
                    try:
                        new_page_ids = scrape_search_page(
                            None,
                            args.url,
                            search_page,
                            proxies_list=proxies_list,
                            vpn_rotate_cmd=args.vpn_rotate,
                            timeout=args.timeout,
                            impersonate_enabled=(not args.no_impersonate),
                            cool_down=args.cool_down,
                            static_proxy=static_proxy
                        )
                    except BlockedException as e:
                        search_blocked = True
                        print(f"    [Block] Search page {search_page} hit a block exception.")
                    except Exception as e:
                        print(f"    [Error] Search page {search_page} failed: {e}")

                for trial_id in batch_to_fetch:
                    try:
                        trial_data = scrape_detail_page(
                            None,
                            trial_id,
                            proxies_list=proxies_list,
                            vpn_rotate_cmd=args.vpn_rotate,
                            timeout=args.timeout,
                            referer=args.url,
                            impersonate_enabled=(not args.no_impersonate),
                            cool_down=args.cool_down,
                            static_proxy=static_proxy
                        )
                        if trial_data:
                            scraped_details_batch.append(trial_data)
                        else:
                            print(f"    [Warning] Failed to scrape details for {trial_id}.")
                            skipped_ids.add(trial_id)
                    except BlockedException as e:
                        detail_blocked = True
                        print(f"    [Block] Trial {trial_id} detail page hit a block exception.")
                    except Exception as e:
                        print(f"    [Error] Trial {trial_id} detail page failed: {e}")
                        skipped_ids.add(trial_id)
            else:
                print(f"\n>>> [Batch {batch_index}] Processing concurrent requests...")
                if fetch_search:
                    print(f"    - Concurrently requesting Search Page {search_page}")
                if batch_to_fetch:
                    print(f"    - Concurrently requesting {len(batch_to_fetch)} trial details: {', '.join(batch_to_fetch)}")

                # Execute requests concurrently using a ThreadPoolExecutor
                max_workers = (1 if fetch_search else 0) + len(batch_to_fetch)
                
                new_page_ids = None
                scraped_details_batch = []
                search_blocked = False
                detail_blocked = False

                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures_detail = {}
                    future_search = None
                    
                    # Submit search page task
                    if fetch_search:
                        future_search = executor.submit(
                            scrape_search_page,
                            None,
                            args.url,
                            search_page,
                            proxies_list=proxies_list,
                            vpn_rotate_cmd=args.vpn_rotate,
                            timeout=args.timeout,
                            impersonate_enabled=(not args.no_impersonate),
                            cool_down=args.cool_down,
                            static_proxy=static_proxy
                        )
                    
                    # Submit detail page tasks
                    for trial_id in batch_to_fetch:
                        future = executor.submit(
                            scrape_detail_page,
                            None,
                            trial_id,
                            proxies_list=proxies_list,
                            vpn_rotate_cmd=args.vpn_rotate,
                            timeout=args.timeout,
                            referer=args.url,
                            impersonate_enabled=(not args.no_impersonate),
                            cool_down=args.cool_down,
                            static_proxy=static_proxy
                        )
                        futures_detail[future] = trial_id

                    # Wait for search page task to complete if submitted
                    if future_search:
                        try:
                            new_page_ids = future_search.result()
                        except BlockedException as e:
                            search_blocked = True
                            print(f"    [Block] Search page {search_page} hit a block exception.")
                        except Exception as e:
                            print(f"    [Error] Search page {search_page} failed: {e}")

                    # Wait for detail page tasks to complete
                    for future in as_completed(futures_detail):
                        trial_id = futures_detail[future]
                        try:
                            trial_data = future.result()
                            if trial_data:
                                scraped_details_batch.append(trial_data)
                            else:
                                print(f"    [Warning] Failed to scrape details for {trial_id}.")
                                skipped_ids.add(trial_id)
                        except BlockedException as e:
                            detail_blocked = True
                            print(f"    [Block] Trial {trial_id} detail page hit a block exception.")
                        except Exception as e:
                            print(f"    [Error] Trial {trial_id} detail page failed: {e}")
                            skipped_ids.add(trial_id)

            # Process search page results
            if fetch_search and not search_blocked:
                if new_page_ids:
                    # Filter out any duplicate IDs
                    unique_new_ids = [tid for tid in new_page_ids if tid not in all_trial_ids]
                    all_trial_ids.extend(unique_new_ids)
                    print(f"    [Search Page {search_page}] Collected {len(unique_new_ids)} new Trial IDs (Total discovered: {len(all_trial_ids)})")
                    
                    # Update ID cache file immediately
                    with open(ids_cache_path, "w", encoding="utf-8") as f:
                        f.write("\n".join(all_trial_ids))
                        
                    search_page += 1
                elif new_page_ids == []:
                    # Returned empty list, meaning end of search pagination
                    print(f"    [Search Page {search_page}] No more Trial IDs found. Search collection complete.")
                    ids_collection_complete = True
                    with open(ids_cache_path, "w", encoding="utf-8") as f:
                        f.write("\n".join(all_trial_ids) + "\n__COMPLETE__")

            # Process detail page results & save
            if scraped_details_batch:
                all_scraped_records.extend(scraped_details_batch)
                scraped_ids.update({trial.get("Trial ID") for trial in scraped_details_batch if trial.get("Trial ID")})
                save_csv()

            # Handle blocks with a smart, resilient failover strategy
            if detail_blocked:
                print(f"\n[!] Block Detected on detail pages: Detail scraping is blocked by the server's firewall.")
                print("    Current progress successfully saved. Please change your VPN/IP before restarting.")
                break
                
            if search_blocked:
                # Calculate how many un-scraped IDs we have left in memory (excluding the current batch)
                remaining_cached = len(unscraped_ids) - len(batch_to_fetch)
                if remaining_cached > 0:
                    print(f"\n[!] Block Detected on Search Page {search_page}. jRCT WAF is blocking search queries.")
                    print(f"    [Resilience] Skipping search page collection for this run and continuing with {remaining_cached} already cached Trial IDs...")
                    # Temporarily stop requesting search pages for this run so we can consume the cached queue
                    ids_collection_complete = True
                else:
                    print(f"\n[!] Block Detected on Search Page {search_page} and no cached Trial IDs remain.")
                    print("    Current progress successfully saved. Please change your VPN/IP before restarting.")
                    break

            # Polite wait delay after the batch completes
            if args.batch_size == 1:
                wait_time = 0.0
            else:
                wait_time = max(args.delay, 5.0)
                
            if wait_time > 0:
                print(f"    Waiting {wait_time} seconds before repeating...")
                time.sleep(wait_time)
            batch_index += 1

    except KeyboardInterrupt:
        print("\n[!] Execution interrupted by user. Saving current progress...")
        save_csv()
        return

    print("\n" + "=" * 70)
    print("  *  SCRAPING PROCESS FINISHED")
    print(f"  Total records saved : {len(all_scraped_records)}")
    print(f"  CSV file path       : {csv_path}")
    
    # Clean up intermediate ID cache text file upon successful completion
    try:
        if ids_cache_path.exists():
            ids_cache_path.unlink()
            print(f"  Cleaned up text cache: {ids_cache_path.name}")
        http_txt = output_dir / "http.txt"
        if http_txt.exists():
            http_txt.unlink()
            print(f"  Cleaned up http log: {http_txt.name}")
    except Exception as e:
        print(f"  (Warning: Could not remove text cache files: {e})")

    print("=" * 70)


if __name__ == "__main__":
    main()
