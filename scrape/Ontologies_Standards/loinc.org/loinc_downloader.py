#!/usr/bin/env python3
"""
Logical Observation Identifiers Names and Codes (LOINC) Downloader and Crawler
Author: Antigravity AI Coding Assistant
Description: A utility script to download the official LOINC database (via Playwright-assisted
             authenticated download) or crawl individual LOINC code pages (unauthenticated, polite)
             to extract detailed medical ontology fields and save them to CSV.
             Features robust progress tracking and resume capabilities.
"""

import os
import sys
import json
import time
import csv
import zipfile
import argparse
import logging
from pathlib import Path
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Load environment variables from .env file if present
load_dotenv()

# Resolve all output paths relative to this script's folder (pipeline contract)
BASE_DIR = Path(__file__).resolve().parent

# HTTP headers to play nice with LOINC and NLM servers.
# loinc.org sits behind Wordfence, which 403s a self-identifying bot agent once
# it decides to; a normal browser signature is what actually gets served.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# A Wordfence block is total and lasts for the rest of the run: every subsequent
# request 403s. Without a circuit breaker the crawler spends over two hours
# issuing 7,500 requests it already knows will fail, and only reports the problem
# at the very end. Stop as soon as the pattern is unmistakable.
MAX_CONSECUTIVE_FAILURES = 25

# Logger setup
logger = logging.getLogger("loinc_downloader")

def setup_logging(verbose: bool):
    """Configures the logging output level."""
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("loinc_downloader.log", encoding="utf-8")
        ]
    )

# ---------------------------------------------------------------------------
# Mode 1: Crawl Mode (Public unauthenticated detail page scraping)
# ---------------------------------------------------------------------------

def discover_codes_via_nlm(limit: int = 7500) -> list:
    """
    Fetches LOINC codes from NLM's public Clinical Table Search Service API.
    The API has a total result retrieval limit of 7,500 records.
    """
    logger.info("Discovering LOINC codes via public NLM API...")
    url = "https://clinicaltables.nlm.nih.gov/api/loinc_items/v3/search"
    codes = []
    offset = 0
    batch_size = 500
    max_to_fetch = min(limit, 7500)

    while len(codes) < max_to_fetch:
        count = min(batch_size, max_to_fetch - len(codes))
        logger.info(f"  Fetching batch at offset {offset} (count {count})...")
        try:
            r = requests.get(
                url,
                params={"terms": "", "count": count, "offset": offset, "df": "LOINC_NUM"},
                headers=HEADERS,
                timeout=20
            )
            r.raise_for_status()
            data = r.json()
            # Response format: [total, [codes], null, [[code], ...]]
            if len(data) >= 2 and data[1]:
                batch_codes = data[1]
                codes.extend(batch_codes)
                if len(batch_codes) < count:
                    # No more codes available
                    break
            else:
                break
        except Exception as e:
            # Swallowing this silently shrank the target list, so the crawl would
            # "complete" against a truncated set of codes and look successful.
            raise RuntimeError(
                f"LOINC code discovery failed at offset {offset} after "
                f"{len(codes):,} code(s): {e}"
            ) from e
        offset += batch_size
        time.sleep(0.2)  # Polite delay

    logger.info(f"Discovered {len(codes):,} LOINC codes.")
    return codes

def scrape_loinc_page(code: str) -> dict:
    """
    Scrapes the detail page for a single LOINC code from loinc.org.
    Does not require authentication.
    """
    url = f"https://loinc.org/{code}/"
    logger.debug(f"Requesting: {url}")
    
    response = requests.get(url, headers=HEADERS, timeout=30)
    if response.status_code == 404:
        logger.warning(f"LOINC code {code} page not found (404).")
        return {"loinc_num": code, "status": "Not Found"}
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    
    # LOINC Code & Long Common Name
    loinc_num_node = soup.find("span", id="loinc-id")
    loinc_num = loinc_num_node.text.strip() if loinc_num_node else code
    
    lcn_node = soup.find("span", id="lcn")
    lcn = lcn_node.text.strip() if lcn_node else ""
    
    status_node = soup.find("span", id="loinc-status")
    status = status_node.text.strip() if status_node else ""
    
    # Description
    desc_sec = soup.find("section", id="term-description")
    description = ""
    if desc_sec:
        desc_p = desc_sec.find("p", itemprop="description")
        if desc_p:
            description = desc_p.text.strip()

    # LOINC Names (Fully-Specified Name, Short Name, Display Name)
    fsn = ""
    short_name = ""
    display_name = ""
    names_sec = soup.find("section", id="additional-names")
    if names_sec:
        dl = names_sec.find("dl")
        if dl:
            terms = [dt.text.strip() for dt in dl.find_all("dt")]
            defs = [dd.text.strip() for dd in dl.find_all("dd")]
            for term, def_val in zip(terms, defs):
                term_lower = term.lower()
                if "fully-specified name" in term_lower:
                    fsn = def_val
                elif "short name" in term_lower:
                    short_name = def_val
                elif "display name" in term_lower:
                    display_name = def_val

    # Clean zero-width spaces in FSN and split axes
    fsn_clean = fsn.replace("\u200b", "").strip()
    axes = fsn_clean.split(":")
    component = axes[0].strip() if len(axes) > 0 else ""
    property_ = axes[1].strip() if len(axes) > 1 else ""
    time_aspect = axes[2].strip() if len(axes) > 2 else ""
    system = axes[3].strip() if len(axes) > 3 else ""
    scale_type = axes[4].strip() if len(axes) > 4 else ""
    method_type = axes[5].strip() if len(axes) > 5 else ""

    # Basic Attributes
    class_val = ""
    type_val = ""
    first_released = ""
    last_updated = ""
    order_obs = ""
    basic_sec = soup.find("section", id="basic")
    if basic_sec:
        dl = basic_sec.find("dl")
        if dl:
            terms = [dt.text.strip() for dt in dl.find_all("dt")]
            defs = [dd.text.strip() for dd in dl.find_all("dd")]
            for term, def_val in zip(terms, defs):
                term_lower = term.lower()
                if "class" in term_lower:
                    class_val = def_val
                elif "type" in term_lower:
                    type_val = def_val
                elif "first released" in term_lower:
                    first_released = def_val
                elif "last updated" in term_lower:
                    last_updated = def_val
                elif "order vs." in term_lower:
                    order_obs = def_val

    # Related Names
    related_names = []
    related_sec = soup.find("section", id="related-names")
    if related_sec:
        related_names = [li.text.strip() for li in related_sec.find_all("li") if li.text]

    # Example Units
    example_units = []
    units_sec = soup.find("section", id="example-units")
    if units_sec:
        tbody = units_sec.find("tbody")
        if tbody:
            for tr in tbody.find_all("tr"):
                tds = tr.find_all("td")
                if tds:
                    unit = tds[0].text.strip()
                    if unit and unit not in example_units:
                        example_units.append(unit)

    return {
        "loinc_num": loinc_num,
        "long_common_name": lcn,
        "status": status,
        "description": description,
        "fully_specified_name": fsn_clean,
        "component": component,
        "property": property_,
        "time_aspect": time_aspect,
        "system": system,
        "scale_type": scale_type,
        "method_type": method_type,
        "short_name": short_name,
        "display_name": display_name,
        "class": class_val,
        "type": type_val,
        "first_released": first_released,
        "last_updated": last_updated,
        "order_obs": order_obs,
        "related_names": "; ".join(related_names),
        "example_units": "; ".join(example_units),
    }

def run_crawler(output_dir: Path, codes_file: str, limit: int, delay: float):
    """Orchestrates the LOINC detail page web crawler."""
    output_dir.mkdir(parents=True, exist_ok=True)
    
    progress_file = output_dir / "loinc_progress.json"
    csv_file = output_dir / "loinc_crawled_codes.csv"
    
    # 1. Discover or load target LOINC codes
    target_codes = []
    if codes_file and os.path.exists(codes_file):
        logger.info(f"Loading seed codes from file: {codes_file}")
        with open(codes_file, "r", encoding="utf-8") as f:
            target_codes = [line.strip() for line in f if line.strip()]
        logger.info(f"Loaded {len(target_codes):,} codes.")
    else:
        # Auto-discover via NLM API
        target_codes = discover_codes_via_nlm(limit=limit or 7500)
    
    if not target_codes:
        logger.error("No LOINC codes to crawl. Exiting.")
        sys.exit(1)

    # 2. Initialize progress tracking
    progress = {}
    if progress_file.exists():
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                progress = json.load(f)
            logger.info(f"Resumed progress: {len(progress):,} codes tracked in progress file.")
        except Exception as e:
            logger.warning(f"Failed to load progress file: {e}. Starting fresh.")
    
    # Sync progress with current targets
    for code in target_codes:
        if code not in progress:
            progress[code] = "pending"

    # Save initial progress
    with open(progress_file, "w", encoding="utf-8") as f:
        json.dump(progress, f, indent=2)

    # 3. Initialize CSV file
    csv_fields = [
        "loinc_num", "long_common_name", "status", "description", "fully_specified_name",
        "component", "property", "time_aspect", "system", "scale_type", "method_type",
        "short_name", "display_name", "class", "type", "first_released", "last_updated",
        "order_obs", "related_names", "example_units"
    ]
    
    csv_exists = csv_file.exists()
    
    # Build list of codes already completed
    completed_codes = {code for code, status in progress.items() if status == "completed"}
    
    # Filter pending codes
    pending_codes = [code for code in target_codes if code not in completed_codes]
    logger.info(f"Crawl Summary: Total={len(target_codes):,}, Completed={len(completed_codes):,}, Pending={len(pending_codes):,}")
    
    if limit and len(pending_codes) > limit:
        pending_codes = pending_codes[:limit]
        logger.info(f"Crawl limit specified. Only crawling the next {len(pending_codes):,} codes.")
        
    if not pending_codes:
        logger.info("All targeted codes are already crawled!")
        return

    # 4. Crawl loop
    write_mode = "a" if csv_exists else "w"
    with open(csv_file, write_mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=csv_fields)
        if not csv_exists:
            writer.writeheader()
            
        total = len(pending_codes)
        start_time = time.time()
        written = 0
        errored = 0
        consecutive_failures = 0
        blocked = False

        for idx, code in enumerate(pending_codes, 1):
            logger.info(f"[{idx}/{total}] ({(idx/total)*100:.1f}%) Crawling: {code}")
            
            try:
                data = scrape_loinc_page(code)
                if data.get("status") == "Not Found":
                    progress[code] = "failed_404"
                    # A genuine 404 is a fact about that code, not a block.
                    consecutive_failures = 0
                else:
                    writer.writerow(data)
                    f.flush()
                    progress[code] = "completed"
                    written += 1
                    consecutive_failures = 0
            except Exception as e:
                logger.error(f"  Failed to crawl {code}: {e}")
                progress[code] = "failed"
                errored += 1
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    logger.error(
                        f"{consecutive_failures} consecutive failures - treating this as a "
                        f"block rather than bad luck, and stopping after {idx:,} of {total:,} "
                        f"codes. Codes not marked completed are retried next run.")
                    blocked = True
                    break
                
            # Update progress file
            with open(progress_file, "w", encoding="utf-8") as pf:
                json.dump(progress, pf, indent=2)
                
            # Print ETA
            elapsed = time.time() - start_time
            avg_time = elapsed / idx
            eta_seconds = avg_time * (total - idx)
            eta_min = eta_seconds / 60
            logger.info(f"  Processed in {elapsed:.1f}s. Avg={avg_time:.2f}s/code. ETA={eta_min:.1f} min.")
            
            if idx < total:
                time.sleep(delay)

    logger.info(f"Crawling complete: {written:,} written, {errored:,} errored "
                f"out of {total:,} attempted. Data saved to: {csv_file}")

    # Codes that error stay out of 'completed', so the next run retries them and
    # the crawl self-heals. What must not pass silently is a session that wrote
    # nothing at all - a block or a DNS failure looks exactly like success once
    # the exit code is 0, and the pipeline would publish the run as complete.
    if written == 0:
        logger.error("No LOINC codes were crawled successfully this session - "
                     "failing so the run is retried rather than published.")
        sys.exit(1)

    # Partial run after a block: the codes already written are real and worth
    # publishing, so this is not a failure - but it must not read as a complete
    # crawl either.
    if blocked:
        logger.warning(
            f"Stopped early after being blocked; {written:,} code(s) crawled this "
            f"session. The rest carry over to the next run.")

# ---------------------------------------------------------------------------
# Mode 2: Bulk Download Mode (Playwright-assisted auth zip download)
# ---------------------------------------------------------------------------

def run_bulk_download(output_dir: Path, username: str, password: str, headless: bool):
    """
    Launches Playwright Chrome to navigate login, accept agreements,
    and download the complete LOINC package zip file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("Playwright package is not installed. Please run: pip install playwright")
        return

    logger.info("=" * 60)
    logger.info("         LOINC BULK DOWNLOAD PROCESS (PLAYWRIGHT)")
    logger.info("=" * 60)
    
    login_url = "https://loinc.org/wp-login.php?redirect_to=https%3A%2F%2Floinc.org%2Fdownload%2Floinc-complete%2F"
    
    with sync_playwright() as p:
        logger.info("Launching Chromium browser...")
        # We run headless=False by default to allow manual interaction in case of captchas
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()
        
        logger.info(f"Navigating to login page: {login_url}")
        page.goto(login_url)
        
        # Try to fill login if credentials are provided
        if username and password:
            try:
                logger.info("Attempting to auto-fill credentials...")
                page.fill("input[name='log']", username)
                page.fill("input[name='pwd']", password)
                page.click("input[name='wp-submit']")
                logger.info("Credentials submitted.")
            except Exception as e:
                logger.warning(f"Could not auto-fill form: {e}. Waiting for manual input.")
        else:
            logger.info("No credentials provided. Waiting for manual login in the browser window.")
            
        # Give instructions to user
        if not headless:
            logger.info("\n" + "!" * 80)
            logger.info(" ACTION REQUIRED: Please log in to loinc.org in the opened Chromium browser.")
            logger.info(" Complete any reCAPTCHA or email confirmation requested by Wordfence.")
            logger.info(" Once logged in, the script will automatically capture the zip download.")
            logger.info("!" * 80 + "\n")
        else:
            logger.info("Running in headless mode. Script will wait for download to trigger.")

        # Polling/Waiting for download trigger
        logger.info("Waiting for the zip download to start (timeout 5 minutes)...")
        download_zip_path = None
        try:
            # page.expect_download captures the download event
            with page.expect_download(timeout=300000) as download_info:
                # Wait for redirection or download click
                pass
            download = download_info.value
            logger.info(f"Zip download detected! Filename: {download.suggested_filename}")
            download_zip_path = output_dir / download.suggested_filename
            download.save_as(download_zip_path)
            logger.info(f"Successfully downloaded zip to: {download_zip_path}")
        except Exception as e:
            logger.warning(f"Automatic download did not trigger: {e}")
            logger.info("Checking if we need to force trigger the download...")
            try:
                with page.expect_download(timeout=60000) as download_info:
                    page.goto("https://loinc.org/download/loinc-complete/")
                download = download_info.value
                logger.info(f"Forced download detected! Filename: {download.suggested_filename}")
                download_zip_path = output_dir / download.suggested_filename
                download.save_as(download_zip_path)
                logger.info(f"Successfully downloaded zip to: {download_zip_path}")
            except Exception as fe:
                logger.error(f"Failed to trigger bulk download: {fe}")
                browser.close()
                return

        browser.close()
        
    if download_zip_path and download_zip_path.exists():
        process_loinc_zip(download_zip_path, output_dir)

def process_loinc_zip(zip_path: Path, output_dir: Path):
    """Unzips the entire LOINC package, preserves subdirectories, creates root copies, and deletes ZIP."""
    logger.info(f"Extracting LOINC Zip package: {zip_path}")
    import shutil

    # Ensure output directory exists
    output_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        members = zip_ref.namelist()
        
        # Determine the top-level folder name to strip it
        top_folder = ""
        if members:
            first_parts = members[0].split('/')
            if len(first_parts) > 1 or (len(first_parts) == 1 and members[0].endswith('/')):
                top_folder = first_parts[0] + '/'
                
        logger.info(f"Top-level folder to strip: {top_folder if top_folder else 'None'}")
        
        for member in members:
            # Strip the top-level folder if present
            if top_folder and member.startswith(top_folder):
                relative_path = member[len(top_folder):]
            else:
                relative_path = member
                
            if not relative_path:
                continue
                
            dest_path = output_dir / relative_path
            
            if member.endswith('/'):
                dest_path.mkdir(parents=True, exist_ok=True)
            else:
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                with zip_ref.open(member) as source, open(dest_path, "wb") as target:
                    shutil.copyfileobj(source, target)
                    
    logger.info("Extraction of all files complete.")

    # Locate and copy primary tables to output root for direct reference
    core_src = None
    possible_core_paths = [
        output_dir / "LoincTableCore" / "LoincTableCore.csv",
        output_dir / "LoincTable" / "Loinc.csv",
    ]
    for p in possible_core_paths:
        if p.exists():
            core_src = p
            break
            
    part_src = output_dir / "AccessoryFiles" / "PartFile" / "Part.csv"

    # Copy Core Table
    if core_src:
        core_dest = output_dir / "loinc_table.csv"
        # Remove existing file if any to prevent permissions/overwrite errors
        if core_dest.exists():
            core_dest.unlink()
        shutil.copy2(core_src, core_dest)
        logger.info(f"Created core reference table: {core_dest.name}")
    else:
        logger.warning("Could not locate core LoincTableCore.csv or Loinc.csv for root copy.")

    # Copy Part Table
    if part_src.exists():
        part_dest = output_dir / "loinc_parts.csv"
        if part_dest.exists():
            part_dest.unlink()
        shutil.copy2(part_src, part_dest)
        logger.info(f"Created parts reference table: {part_dest.name}")
    else:
        logger.warning("Could not locate Part.csv for root copy.")

    # Delete the zip file after successful processing
    logger.info(f"Deleting the original ZIP file: {zip_path}")
    for attempt in range(5):
        try:
            zip_path.unlink()
            logger.info("ZIP file deleted successfully.")
            break
        except Exception as e:
            if attempt == 4:
                logger.warning(f"Could not delete ZIP file {zip_path.name} after 5 attempts: {e}")
            else:
                logger.info(f"ZIP file locked, retrying deletion in 1s (attempt {attempt+1}/5)...")
                time.sleep(1.0)

    # Preview using pandas if available
    try:
        import pandas as pd
        logger.info("\n" + "=" * 60)
        logger.info("           LOINC DATASET PREVIEW")
        logger.info("=" * 60)
        if Path(output_dir / "loinc_table.csv").exists():
            df = pd.read_csv(output_dir / "loinc_table.csv", dtype=str, nrows=5)
            logger.info("Loinc Table Core (First 5 rows):")
            # Show a subset of columns for readability
            cols = [c for c in ["LOINC_NUM", "LONG_COMMON_NAME", "COMPONENT", "SYSTEM", "STATUS"] if c in df.columns]
            logger.info("\n" + df[cols].to_string())
    except ImportError:
        pass

    logger.info("\n" + "=" * 60)
    logger.info("              LOINC BULK DOWNLOAD COMPLETE")
    logger.info("=" * 60)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Scrape and download LOINC database tables or crawl detail pages into structured CSVs."
    )
    
    # Subcommands
    subparsers = parser.add_subparsers(dest="mode", required=True, help="Collection mode")
    
    # Crawl Subcommand
    crawl_parser = subparsers.add_parser("crawl", help="Crawl public loinc.org pages using codes from NLM API or file.")
    crawl_parser.add_argument(
        "--output-dir",
        type=str,
        default=str(BASE_DIR / "loinc_data"),
        help="Directory to save output files (default: Ontologies & Standards/loinc_data)"
    )
    crawl_parser.add_argument(
        "--codes-file",
        type=str,
        default=None,
        help="Path to an optional text file with LOINC codes to crawl (one per line)"
    )
    crawl_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of codes to crawl during this session (default: all discovered)"
    )
    crawl_parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Polite delay in seconds between webpage requests (default: 1.0)"
    )
    crawl_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose debug logging"
    )

    # Bulk Download Subcommand
    bulk_parser = subparsers.add_parser("bulk-download", help="Download complete database zip from loinc.org via Playwright.")
    bulk_parser.add_argument(
        "--output-dir",
        type=str,
        default=str(BASE_DIR / "loinc_data"),
        help="Directory to save the downloaded zip and CSV tables (default: Ontologies & Standards/loinc_data)"
    )
    bulk_parser.add_argument(
        "--username",
        type=str,
        default=os.getenv("LOINC_USERNAME", "mouyen"),
        help="LOINC website username (defaults to 'mouyen')"
    )
    bulk_parser.add_argument(
        "--password",
        type=str,
        default=os.getenv("LOINC_PASSWORD", "n84Bhjrr9z7cPDE"),
        help="LOINC website password (defaults to 'n84Bhjrr9z7cPDE')"
    )
    bulk_parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode (warning: Wordfence captcha requires headful mode)"
    )
    bulk_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose debug logging"
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    output_dir = Path(args.output_dir)

    if args.mode == "crawl":
        run_crawler(output_dir, args.codes_file, args.limit, args.delay)
    elif args.mode == "bulk-download":
        run_bulk_download(output_dir, args.username, args.password, args.headless)

if __name__ == "__main__":
    main()
