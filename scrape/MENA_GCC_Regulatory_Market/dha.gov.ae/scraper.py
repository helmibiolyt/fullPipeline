#!/usr/bin/env python3
"""
DHA (Dubai Health Authority) scraper — downloads drug pricing, circulars,
drug control documents, policies, and guidelines. Converts to CSV.
"""

import os
import re
import sys
import csv
import io
import json
import time
import logging
from pathlib import Path
from collections import OrderedDict
from urllib.parse import urlparse, quote, urlunparse, urljoin

import requests
from bs4 import BeautifulSoup
import pandas as pd

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from file_converter import convert_file_to_csv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
BASE_URL = "https://www.dha.gov.ae"

FOLDERS = {
    "pricing":    BASE_DIR / "Drug Pricing",
    "circulars":  BASE_DIR / "Circulars",
    "drugctrl":   BASE_DIR / "Drug Control",
    "policies":   BASE_DIR / "Policies",
    "guidelines": BASE_DIR / "Guidelines",
}

DOWNLOAD_EXTS = {".xlsx", ".xls", ".xlsm", ".csv", ".pdf", ".doc", ".docx"}

PRICE_LIST_URL = "https://www.dha.gov.ae/uploads/022022/PriceList%20en2022239251.xlsx"
CIRCULARS_URL = "https://www.dha.gov.ae/licensing-regulations-circulars"
DRUG_CONTROL_URL = "https://www.dha.gov.ae/HealthRegulationSector/DrugControl"
POLICIES_URL = "https://www.dha.gov.ae/licensing-regulations-policies"
GUIDELINES_URL = "https://www.dha.gov.ae/licensing-regulations-clinical-guidelines"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

MAX_RETRIES = 3
RETRY_BACKOFF = 2
REQUEST_DELAY = 0.5

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log = logging.getLogger("dha_scraper")
log.setLevel(logging.DEBUG)
fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.INFO)
ch.setFormatter(fmt)
log.addHandler(ch)

fh = logging.FileHandler(BASE_DIR / "scraper.log", mode="w", encoding="utf-8")
fh.setLevel(logging.DEBUG)
fh.setFormatter(fmt)
log.addHandler(fh)

# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def init_session():
    """Visit homepage to collect cookies."""
    try:
        SESSION.get(BASE_URL, timeout=15)
        log.info("Session initialized (homepage visited)")
    except Exception as e:
        log.warning(f"Could not visit homepage: {e}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe_name(text: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\n\r]', '', text).strip().rstrip('.')
    name = re.sub(r'\s+', ' ', name)
    return name[:200] if name else "file"


def encode_url(url: str) -> str:
    """Properly encode URL with spaces."""
    url_pre = url.replace(" ", "%20")
    p = urlparse(url_pre)
    encoded_path = quote(p.path, safe="/:@!$&'()*+,;=%")
    return urlunparse(p._replace(path=encoded_path))


def fetch_with_retry(url: str, stream: bool = False, timeout: int = 60, **kwargs) -> requests.Response:
    """Fetch URL with retry and backoff."""
    url = encode_url(url)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = SESSION.get(url, timeout=timeout, stream=stream, **kwargs)
            r.raise_for_status()
            return r
        except Exception as e:
            if attempt == MAX_RETRIES:
                raise
            wait = RETRY_BACKOFF ** attempt
            log.warning(f"Retry {attempt}/{MAX_RETRIES} for {url}: {e} (wait {wait}s)")
            time.sleep(wait)


def fetch_page(url: str) -> str | None:
    """Fetch HTML page, return text or None."""
    try:
        r = fetch_with_retry(url)
        ct = r.headers.get("Content-Type", "")
        if "html" not in ct and "json" not in ct:
            return None
        return r.text
    except Exception as e:
        log.warning(f"Could not fetch {url}: {e}")
        return None


def find_file_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Find all downloadable file links in HTML."""
    soup = BeautifulSoup(html, "lxml")
    results = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        ext = Path(href.split("?")[0]).suffix.lower()
        if ext not in DOWNLOAD_EXTS:
            continue
        label = a.get_text(strip=True) or Path(href.split("?")[0]).stem
        if href.startswith("http"):
            full = href
        elif href.startswith("/"):
            p = urlparse(base_url)
            full = f"{p.scheme}://{p.netloc}{href}"
        else:
            full = base_url.rstrip("/") + "/" + href
        if full not in seen:
            seen.add(full)
            results.append((label, full))
    return results


def download_file(url: str) -> tuple[bytes, str]:
    """Download a file and return (content, extension)."""
    r = fetch_with_retry(url, stream=True,
                         headers={"Referer": BASE_URL + "/"})
    ct = r.headers.get("Content-Type", "")
    if "html" in ct:
        raise ValueError("Got HTML instead of file (likely error page)")
    content = b"".join(r.iter_content(65536))
    ext = Path(url.split("?")[0]).suffix.lower()
    # Handle encoded extensions
    if not ext:
        ext = ".bin"
    return content, ext


# ---------------------------------------------------------------------------
# Conversion — only CSV/Excel handled inline; PDF/Word/PPT handled by post-processor
# ---------------------------------------------------------------------------

def convert_to_csv(data: bytes, ext: str, out_dir: Path, base_name: str) -> list[Path]:
    if ext in (".xlsx", ".xls", ".xlsm"):
        return convert_file_to_csv(data, ext, out_dir, base_name)
    elif ext == ".csv":
        out = out_dir / f"{base_name}.csv"
        out.write_bytes(data)
        return [out]
    else:
        log.info(f"  [DEFER] {ext} will be processed by MiniMax post-processor")
        return []


# ---------------------------------------------------------------------------
# Process a single file
# ---------------------------------------------------------------------------

def process_file(label: str, url: str, folder: Path):
    name = safe_name(label)
    log.info(f"  -> {label}")
    try:
        data, ext = download_file(url)
    except Exception as e:
        log.error(f"     Download failed: {e}")
        return
    log.info(f"     {len(data):,} bytes  ext={ext}")
    # Save original file
    orig_path = folder / f"{name}{ext}"
    orig_path.write_bytes(data)
    log.info(f"     Saved: {orig_path.name}")
    try:
        csvs = convert_to_csv(data, ext, folder, name)
    except Exception as e:
        log.error(f"     Convert failed: {e}")
        return
    if csvs:
        for c in csvs:
            log.info(f"     CSV: {c.name}")
    else:
        log.warning(f"     No CSV output for {name}")
    time.sleep(REQUEST_DELAY)


# ---------------------------------------------------------------------------
# Scrape pages for file links
# ---------------------------------------------------------------------------

def scrape_page_for_files(url: str, folder: Path) -> int:
    log.info(f"[Page] {url}")
    html = fetch_page(url)
    if not html:
        return 0
    links = find_file_links(html, url)
    log.info(f"  Found {len(links)} file link(s)")
    for label, file_url in links:
        process_file(label, file_url, folder)
    return len(links)


# ---------------------------------------------------------------------------
# A) Drug Price List
# ---------------------------------------------------------------------------

def scrape_drug_pricing():
    folder = FOLDERS["pricing"]
    log.info("=" * 60)
    log.info("A) Drug Price List")
    log.info("=" * 60)

    # Download the known price list
    process_file("Drug Price List", PRICE_LIST_URL, folder)

    # Also scrape Drug Control page for any other Excel/PDF files
    log.info("Checking Drug Control page for additional files...")
    html = fetch_page(DRUG_CONTROL_URL)
    if html:
        links = find_file_links(html, DRUG_CONTROL_URL)
        pricing_links = [(l, u) for l, u in links if u.endswith((".xlsx", ".xls"))]
        for label, url in pricing_links:
            if url != PRICE_LIST_URL:
                process_file(label, url, folder)


# ---------------------------------------------------------------------------
# B) Regulatory Circulars
# ---------------------------------------------------------------------------

def scrape_circulars():
    folder = FOLDERS["circulars"]
    log.info("=" * 60)
    log.info("B) Regulatory Circulars")
    log.info("=" * 60)

    all_circulars = []

    for page_num in range(1, 7):  # 6 pages
        url = f"{CIRCULARS_URL}?page={page_num}"
        log.info(f"  Fetching page {page_num}...")
        html = fetch_page(url)
        if not html:
            # Try alternative pagination
            url = f"{CIRCULARS_URL}/{page_num}"
            html = fetch_page(url)
        if not html:
            log.warning(f"  Could not fetch page {page_num}")
            continue

        soup = BeautifulSoup(html, "lxml")

        # Try to find embedded JSON data (Next.js pattern)
        for script in soup.find_all("script", type="application/json"):
            try:
                data = json.loads(script.string or "")
                circulars = extract_circulars_from_json(data)
                if circulars:
                    all_circulars.extend(circulars)
                    log.info(f"  Page {page_num}: {len(circulars)} circulars from JSON")
                    continue
            except (json.JSONDecodeError, TypeError):
                pass

        # Try script tags with __NEXT_DATA__ or similar
        for script in soup.find_all("script"):
            text = script.string or ""
            if "__NEXT_DATA__" in text or "circulars" in text.lower():
                # Try to extract JSON from the script
                match = re.search(r'({.*})', text, re.DOTALL)
                if match:
                    try:
                        data = json.loads(match.group(1))
                        circulars = extract_circulars_from_json(data)
                        if circulars:
                            all_circulars.extend(circulars)
                            log.info(f"  Page {page_num}: {len(circulars)} circulars from script")
                    except (json.JSONDecodeError, TypeError):
                        pass

        # Fallback: parse HTML structure for circular items
        if not all_circulars or page_num > 1:
            circulars = extract_circulars_from_html(soup)
            if circulars:
                # Avoid duplicates
                existing_titles = {c.get("title", "") for c in all_circulars}
                new_circulars = [c for c in circulars if c.get("title", "") not in existing_titles]
                all_circulars.extend(new_circulars)
                log.info(f"  Page {page_num}: {len(new_circulars)} circulars from HTML")

        time.sleep(REQUEST_DELAY)

    # Save circulars to CSV
    if all_circulars:
        out = folder / "circulars.csv"
        fieldnames = ["reference", "title", "date", "category", "link"]
        with open(out, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL,
                                    extrasaction="ignore")
            writer.writeheader()
            writer.writerows(all_circulars)
        log.info(f"  Saved {len(all_circulars)} circulars to {out.name}")
    else:
        log.warning("  No circulars extracted")

    # Also download any file links found on circulars pages
    html = fetch_page(CIRCULARS_URL)
    if html:
        links = find_file_links(html, CIRCULARS_URL)
        for label, url in links:
            process_file(label, url, folder)


def extract_circulars_from_json(data, results=None) -> list[dict]:
    """Recursively search JSON for circular-like data."""
    if results is None:
        results = []
    if isinstance(data, dict):
        # Check if this dict looks like a circular
        keys_lower = {k.lower() for k in data.keys()}
        if any(k in keys_lower for k in ("title", "name", "subject")) and \
           any(k in keys_lower for k in ("date", "publishdate", "createdon", "created")):
            circular = {}
            for k, v in data.items():
                kl = k.lower()
                if "ref" in kl or "number" in kl or "code" in kl:
                    circular["reference"] = str(v)
                elif kl in ("title", "name", "subject"):
                    circular["title"] = str(v)
                elif "date" in kl or "publish" in kl:
                    circular["date"] = str(v)
                elif "category" in kl or "type" in kl:
                    circular["category"] = str(v)
                elif "link" in kl or "url" in kl or "slug" in kl:
                    circular["link"] = str(v)
            if circular.get("title"):
                results.append(circular)
        # Recurse into values
        for v in data.values():
            extract_circulars_from_json(v, results)
    elif isinstance(data, list):
        for item in data:
            extract_circulars_from_json(item, results)
    return results


def extract_circulars_from_html(soup) -> list[dict]:
    """Extract circulars from HTML structure."""
    results = []
    # Look for common patterns: cards, list items, table rows
    # Try cards/articles
    for item in soup.select(".circular-item, .card, .list-item, article, .item"):
        circular = {}
        # Title
        title_el = item.select_one("h2, h3, h4, .title, a")
        if title_el:
            circular["title"] = title_el.get_text(strip=True)
            if title_el.name == "a" and title_el.get("href"):
                circular["link"] = urljoin(CIRCULARS_URL, title_el["href"])
        # Date
        date_el = item.select_one(".date, time, .meta")
        if date_el:
            circular["date"] = date_el.get_text(strip=True)
        # Reference
        ref_el = item.select_one(".reference, .ref, .number")
        if ref_el:
            circular["reference"] = ref_el.get_text(strip=True)
        # Category
        cat_el = item.select_one(".category, .tag, .badge")
        if cat_el:
            circular["category"] = cat_el.get_text(strip=True)
        if circular.get("title"):
            results.append(circular)

    # Try table rows
    if not results:
        for tr in soup.select("table tr"):
            tds = tr.find_all("td")
            if len(tds) >= 2:
                circular = {"title": tds[0].get_text(strip=True)}
                if len(tds) >= 3:
                    circular["date"] = tds[1].get_text(strip=True)
                    circular["reference"] = tds[2].get_text(strip=True) if len(tds) > 3 else ""
                a = tr.find("a", href=True)
                if a:
                    circular["link"] = urljoin(CIRCULARS_URL, a["href"])
                results.append(circular)

    return results


# ---------------------------------------------------------------------------
# C) Drug Control Forms & Documents
# ---------------------------------------------------------------------------

def scrape_drug_control():
    folder = FOLDERS["drugctrl"]
    log.info("=" * 60)
    log.info("C) Drug Control Forms & Documents")
    log.info("=" * 60)
    scrape_page_for_files(DRUG_CONTROL_URL, folder)

    # Try sub-pages that might be linked
    html = fetch_page(DRUG_CONTROL_URL)
    if html:
        soup = BeautifulSoup(html, "lxml")
        # Find internal page links that might have more documents
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/HealthRegulationSector/" in href or "/DrugControl" in href:
                full_url = urljoin(DRUG_CONTROL_URL, href)
                if full_url != DRUG_CONTROL_URL and "dha.gov.ae" in full_url:
                    log.info(f"  Checking sub-page: {full_url}")
                    scrape_page_for_files(full_url, folder)


# ---------------------------------------------------------------------------
# D) Pharmaceutical Policies
# ---------------------------------------------------------------------------

def scrape_policies():
    folder = FOLDERS["policies"]
    log.info("=" * 60)
    log.info("D) Pharmaceutical Policies")
    log.info("=" * 60)
    scrape_page_for_files(POLICIES_URL, folder)


# ---------------------------------------------------------------------------
# E) Clinical Guidelines
# ---------------------------------------------------------------------------

def scrape_guidelines():
    folder = FOLDERS["guidelines"]
    log.info("=" * 60)
    log.info("E) Clinical Guidelines")
    log.info("=" * 60)
    scrape_page_for_files(GUIDELINES_URL, folder)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("=" * 60)
    log.info("DHA (Dubai Health Authority) Scraper")
    log.info("=" * 60)

    # Create folders
    for folder in FOLDERS.values():
        folder.mkdir(parents=True, exist_ok=True)

    # Initialize session
    init_session()

    # Run all scrapers
    scrape_drug_pricing()
    scrape_circulars()
    scrape_drug_control()
    scrape_policies()
    scrape_guidelines()

    # Summary
    log.info("")
    log.info("=" * 60)
    log.info("Summary")
    log.info("=" * 60)
    for key, folder in FOLDERS.items():
        csvs = list(folder.glob("*.csv"))
        pdfs = list(folder.glob("*.pdf"))
        xlsxs = list(folder.glob("*.xlsx")) + list(folder.glob("*.xls"))
        log.info(f"  {folder.name}: {len(csvs)} CSV, {len(pdfs)} PDF, {len(xlsxs)} Excel")

    # Post-process: extract CSVs from PDF/Word/PPT files using MiniMax
    from process_files import process_scraper
    log.info("Post-processing downloaded files with MiniMax...")
    process_scraper(BASE_DIR)


if __name__ == "__main__":
    main()
