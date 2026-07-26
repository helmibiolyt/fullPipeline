#!/usr/bin/env python3
"""
Qatar Ministry of Public Health (MOPH) Scraper
Extracts pharmaceutical and health data from MOPH and related portals.
"""

import os
import sys
import csv
import json
import time
import logging
from collections import OrderedDict
from pathlib import Path

import requests
import pandas as pd
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DELAY = 0.5
MAX_RETRIES = 3
BACKOFF_FACTOR = 2

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("moph_scraper")
logger.setLevel(logging.DEBUG)

ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.INFO)
ch.setFormatter(log_fmt)
logger.addHandler(ch)

fh = logging.FileHandler(BASE_DIR / "scraper.log", mode="w", encoding="utf-8")
fh.setLevel(logging.DEBUG)
fh.setFormatter(log_fmt)
logger.addHandler(fh)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fetch(session: requests.Session, url: str, stream: bool = False,
          timeout: int = 60, **kwargs) -> requests.Response:
    """Fetch with retry and backoff."""
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, stream=stream, timeout=timeout, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_err = e
            wait = BACKOFF_FACTOR ** attempt
            logger.warning("Attempt %d/%d failed for %s: %s (retry in %ds)",
                           attempt, MAX_RETRIES, url, e, wait)
            time.sleep(wait)
    raise last_err


def save_csv(rows, filepath: Path, headers=None):
    """Save list of lists to CSV."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if headers:
            w.writerow(headers)
        w.writerows(rows)
    logger.info("Saved CSV: %s (%d rows)", filepath, len(rows))


def group_tables_by_columns(tables):
    """Group extracted tables by column count."""
    groups = OrderedDict()
    for rows in tables:
        if not rows:
            continue
        key = len(rows[0])
        if key not in groups:
            groups[key] = []
        groups[key].extend(rows)
    return list(groups.values())


def xlsx_to_csv(xlsx_path: Path, out_dir: Path):
    """Convert XLSX to CSV, merging sheets by column count."""
    logger.info("Converting XLSX to CSV: %s", xlsx_path)
    try:
        xls = pd.ExcelFile(xlsx_path)
    except Exception as e:
        logger.error("Failed to open XLSX %s: %s", xlsx_path, e)
        return

    all_tables = []
    for sheet in xls.sheet_names:
        df = pd.read_excel(xls, sheet_name=sheet, header=None)
        if df.empty:
            continue
        rows = df.values.tolist()
        all_tables.append(rows)

    groups = group_tables_by_columns(all_tables)
    stem = xlsx_path.stem
    for i, group in enumerate(groups):
        suffix = f"_{i+1}" if len(groups) > 1 else ""
        csv_path = out_dir / f"{stem}{suffix}.csv"
        save_csv(group, csv_path)


def pdf_to_csv(pdf_path: Path, out_dir: Path):
    """Skip PDF conversion -- will be processed by post-processor with MiniMax LLM."""
    logger.info("Skipping PDF conversion for %s, will be processed by post-processor",
                pdf_path)


# ---------------------------------------------------------------------------
# Scrapers
# ---------------------------------------------------------------------------

def scrape_priced_products(session: requests.Session):
    """Download priced products XLSX and convert to CSV."""
    out_dir = BASE_DIR / "Drug Pricing"
    out_dir.mkdir(parents=True, exist_ok=True)

    url = ("https://www.moph.gov.qa/_layouts/15/download.aspx?"
           "SourceUrl=/Admin/Lists/PublicationsAttachments/Attachments/66/"
           "Priced%20Products%20(15-08-2025).xlsx")

    xlsx_path = out_dir / "Priced Products (15-08-2025).xlsx"

    logger.info("Attempting to download priced products XLSX...")
    try:
        # Try with referer to look like a browser click
        resp = fetch(session, url, stream=True, headers={
            "Referer": "https://www.moph.gov.qa/english/strategies-and-policies/"
                       "policies-and-guidelines/Pages/default.aspx",
        })
        with open(xlsx_path, "wb") as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)

        # Verify it's actually an Excel file (not an HTML error page)
        with open(xlsx_path, "rb") as f:
            magic = f.read(4)

        if magic[:2] == b"PK" or magic[:4] == b"\xd0\xcf\x11\xe0":
            logger.info("Downloaded XLSX successfully (%d bytes)", xlsx_path.stat().st_size)
            xlsx_to_csv(xlsx_path, out_dir)
        else:
            logger.warning(
                "Downloaded file is NOT a valid Excel file (likely WAF/CAPTCHA block). "
                "The MOPH site uses Imperva WAF. Manual download required: %s", url
            )
            xlsx_path.unlink(missing_ok=True)

    except requests.RequestException as e:
        logger.error(
            "Failed to download priced products (WAF likely blocking): %s\n"
            "Manual download URL: %s", e, url
        )

    time.sleep(DELAY)


def scrape_open_data(session: requests.Session):
    """Download health datasets from data.gov.qa OpenDataSoft API."""
    out_dir = BASE_DIR / "Open Data"
    out_dir.mkdir(parents=True, exist_ok=True)

    catalog_url = ("https://www.data.gov.qa/api/explore/v2.1/catalog/datasets"
                   "?where=theme%3D%22Health%22&limit=100")

    # Known datasets to try as fallback
    known_datasets = [
        "number-of-health-facilities-by-type-of-health-facility-2019",
        "number-of-government-and-private-hospitals-and-health-centers",
        "health-statistics-number-and-rate-for-health-indicators-in-government-sector",
        "health-statistics-number-and-rate-for-health-indicators-in-private-sector",
        "phcc-annual-statistical-summary",
    ]

    dataset_ids = set()

    # Discover health datasets from catalog
    logger.info("Searching data.gov.qa for health datasets...")
    try:
        resp = fetch(session, catalog_url)
        data = resp.json()
        results = data.get("results", [])
        logger.info("Found %d health datasets in catalog", len(results))
        for ds in results:
            ds_id = ds.get("dataset_id") or ds.get("dataset_uid")
            if ds_id:
                dataset_ids.add(ds_id)
                title = ds.get("metas", {}).get("default", {}).get("title", ds_id)
                logger.info("  - %s (%s)", title, ds_id)
    except Exception as e:
        logger.warning("Catalog search failed: %s, using known dataset IDs", e)

    # Add known datasets
    for ds_id in known_datasets:
        dataset_ids.add(ds_id)

    # Download each dataset
    downloaded = 0
    for ds_id in sorted(dataset_ids):
        csv_path = out_dir / f"{ds_id}.csv"

        logger.info("Downloading dataset: %s", ds_id)
        all_records = []
        offset = 0
        limit = 100

        while True:
            api_url = (f"https://www.data.gov.qa/api/explore/v2.1/catalog/datasets/"
                       f"{ds_id}/records?limit={limit}&offset={offset}")
            try:
                resp = fetch(session, api_url)
                data = resp.json()
                records = data.get("results", [])
                if not records:
                    break
                all_records.extend(records)
                total = data.get("total_count", 0)
                offset += limit
                logger.debug("  fetched %d/%d records", len(all_records), total)
                if offset >= total:
                    break
                time.sleep(DELAY)
            except Exception as e:
                logger.warning("Failed to fetch records for %s at offset %d: %s",
                               ds_id, offset, e)
                break

        if all_records:
            df = pd.json_normalize(all_records)
            df.to_csv(csv_path, index=False, encoding="utf-8")
            logger.info("Saved %s (%d records, %d columns)",
                        csv_path.name, len(df), len(df.columns))
            downloaded += 1
        else:
            logger.warning("No records found for dataset: %s", ds_id)

        time.sleep(DELAY)

    logger.info("Open data: downloaded %d datasets", downloaded)


def scrape_practitioners(session: requests.Session):
    """Try to scrape DHP licensed practitioners search."""
    out_dir = BASE_DIR / "Licensed Practitioners"
    out_dir.mkdir(parents=True, exist_ok=True)

    url = "https://dhp.moph.gov.qa/en/Pages/SearchPractitionersPage.aspx"
    logger.info("Attempting to scrape DHP practitioners page...")

    try:
        resp = fetch(session, url)
        html = resp.text

        # Save raw HTML for analysis
        raw_path = out_dir / "SearchPractitionersPage.html"
        with open(raw_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Saved raw HTML (%d bytes)", len(html))

        soup = BeautifulSoup(html, "lxml")

        # Look for any embedded data, API endpoints, or form actions
        scripts = soup.find_all("script")
        api_hints = []
        for script in scripts:
            text = script.string or ""
            # Look for API URLs or data endpoints
            for keyword in ["api", "search", "practitioner", "endpoint", "serviceUrl",
                            "ajax", "fetch", "xmlhttp", "/_api/", "/_vti_bin/"]:
                if keyword.lower() in text.lower():
                    api_hints.append(text[:500])
                    break

        if api_hints:
            logger.info("Found %d scripts with potential API references", len(api_hints))
            hints_path = out_dir / "api_hints.txt"
            with open(hints_path, "w", encoding="utf-8") as f:
                for i, hint in enumerate(api_hints):
                    f.write(f"--- Script {i+1} ---\n{hint}\n\n")

        # Try SharePoint REST API for lists
        sp_api_url = ("https://dhp.moph.gov.qa/en/_api/web/lists/"
                      "getbytitle('Practitioners')/items?$top=100")
        try:
            sp_resp = session.get(sp_api_url, headers={
                "Accept": "application/json;odata=verbose",
                **HEADERS
            }, timeout=30)
            if sp_resp.status_code == 200:
                sp_data = sp_resp.json()
                items = sp_data.get("d", {}).get("results", [])
                if items:
                    df = pd.json_normalize(items)
                    df.to_csv(out_dir / "practitioners.csv", index=False)
                    logger.info("Got %d practitioners from SharePoint API", len(items))
                    return
            logger.info("SharePoint API returned status %d", sp_resp.status_code)
        except Exception as e:
            logger.debug("SharePoint API attempt failed: %s", e)

        # Check if page has useful data even without JS
        tables = soup.find_all("table")
        if tables:
            logger.info("Found %d tables in practitioners page", len(tables))
            for i, table in enumerate(tables):
                rows_data = []
                for tr in table.find_all("tr"):
                    cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                    if cells:
                        rows_data.append(cells)
                if rows_data:
                    save_csv(rows_data, out_dir / f"table_{i+1}.csv")

        logger.warning(
            "Practitioners page likely requires JavaScript rendering. "
            "The search form is SharePoint-based and may need browser automation."
        )

    except requests.RequestException as e:
        logger.error("Failed to access practitioners page: %s", e)

    time.sleep(DELAY)


def scrape_guidelines(session: requests.Session):
    """Download pharmacist guidelines PDF and extract tables."""
    out_dir = BASE_DIR / "Guidelines"
    out_dir.mkdir(parents=True, exist_ok=True)

    url = "https://dhp.moph.gov.qa/en/Documents/Guidelines%20for%20Pharmacists.pdf"
    pdf_path = out_dir / "Guidelines for Pharmacists.pdf"

    logger.info("Downloading pharmacist guidelines PDF...")
    try:
        resp = fetch(session, url, stream=True)

        with open(pdf_path, "wb") as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)

        size = pdf_path.stat().st_size
        logger.info("Downloaded PDF (%d bytes)", size)

        # Verify it's a PDF
        with open(pdf_path, "rb") as f:
            magic = f.read(5)

        if magic == b"%PDF-":
            pdf_to_csv(pdf_path, out_dir)
        else:
            logger.warning("Downloaded file is not a valid PDF")
            pdf_path.unlink(missing_ok=True)

    except requests.RequestException as e:
        logger.error("Failed to download guidelines PDF: %s", e)

    time.sleep(DELAY)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    logger.info("=" * 60)
    logger.info("Qatar MOPH Scraper - Starting")
    logger.info("=" * 60)
    logger.info("Output directory: %s", BASE_DIR)

    session = requests.Session()
    session.headers.update(HEADERS)

    # A) Priced Products
    logger.info("-" * 40)
    logger.info("Section A: Priced Products")
    logger.info("-" * 40)
    scrape_priced_products(session)

    # B) Open Data
    logger.info("-" * 40)
    logger.info("Section B: Open Data (data.gov.qa)")
    logger.info("-" * 40)
    scrape_open_data(session)

    # C) Licensed Practitioners
    logger.info("-" * 40)
    logger.info("Section C: DHP Licensed Practitioners")
    logger.info("-" * 40)
    scrape_practitioners(session)

    # D) Pharmacist Guidelines
    logger.info("-" * 40)
    logger.info("Section D: Pharmacist Guidelines")
    logger.info("-" * 40)
    scrape_guidelines(session)

    logger.info("=" * 60)
    logger.info("Scraper completed")
    logger.info("=" * 60)

    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
if __name__ == "__main__":
    main()
