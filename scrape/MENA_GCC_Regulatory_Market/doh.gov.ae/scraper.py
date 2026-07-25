#!/usr/bin/env python3
"""
DOH (Abu Dhabi Department of Health) scraper — downloads circulars,
policies, manuals, medications/supplements awareness materials,
guidelines, and standards. Converts PDFs/Excel to CSV.
"""

import os
import re
import sys
import csv
import io
import time
import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from collections import OrderedDict
from urllib.parse import urlparse, quote, urlunparse, urljoin

import requests
from bs4 import BeautifulSoup
import pandas as pd

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
BASE_URL = "https://www.doh.gov.ae"

FOLDERS = {
    "circulars":    BASE_DIR / "Circulars",
    "policies":     BASE_DIR / "Policies",
    "medications":  BASE_DIR / "Medications & Supplements",
    "guidelines":   BASE_DIR / "Guidelines",
    "standards":    BASE_DIR / "Standards",
}

DOWNLOAD_EXTS = {".xlsx", ".xls", ".xlsm", ".csv", ".pdf", ".doc", ".docx", ".ashx"}

CIRCULARS_RSS_URL = "https://www.doh.gov.ae/en/resources/circulars-rss-feed"
POLICIES_URL = "https://www.doh.gov.ae/en/resources/policies"
SUPPLEMENTS_URL = "https://www.doh.gov.ae/en/resources/Supplement-Product"
GUIDELINES_URL = "https://www.doh.gov.ae/en/resources/guidelines"
STANDARDS_URL = "https://www.doh.gov.ae/en/resources/standards"

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
log = logging.getLogger("doh_scraper")
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
        return r.text
    except Exception as e:
        log.warning(f"Could not fetch {url}: {e}")
        return None


def find_file_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Find all downloadable file links in HTML, including Sitecore /-/media/*.ashx links."""
    soup = BeautifulSoup(html, "lxml")
    results = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        # Accept standard download extensions or Sitecore media handler
        ext = Path(href.split("?")[0]).suffix.lower()
        is_sitecore_media = "/-/media/" in href and href.split("?")[0].endswith(".ashx")
        if ext not in DOWNLOAD_EXTS and not is_sitecore_media:
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
    """Download a file and return (content, guessed_extension)."""
    r = fetch_with_retry(url, stream=True,
                         headers={"Referer": BASE_URL + "/"})
    ct = r.headers.get("Content-Type", "")
    if "html" in ct and "/-/media/" not in url:
        raise ValueError("Got HTML instead of file (likely error page)")
    content = b"".join(r.iter_content(65536))
    # Determine extension
    ext = Path(url.split("?")[0]).suffix.lower()
    if ext == ".ashx" or not ext:
        # Guess from content-type
        if "pdf" in ct:
            ext = ".pdf"
        elif "excel" in ct or "spreadsheet" in ct:
            ext = ".xlsx"
        elif "msword" in ct or "wordprocessing" in ct:
            ext = ".docx"
        elif content[:4] == b'%PDF':
            ext = ".pdf"
        elif content[:2] == b'PK':
            ext = ".xlsx"
        else:
            ext = ".pdf"  # Default for DOH media
    return content, ext


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

def group_tables_by_columns(tables):
    from collections import OrderedDict
    groups = OrderedDict()
    for rows in tables:
        if not rows:
            continue
        key = len(rows[0])
        if key not in groups:
            groups[key] = []
        groups[key].extend(rows)
    return list(groups.values())


def excel_to_csvs(data: bytes, ext: str, out_dir: Path, base_name: str) -> list[Path]:
    engine = "openpyxl" if ext in (".xlsx", ".xlsm") else "xlrd"
    try:
        xf = pd.ExcelFile(io.BytesIO(data), engine=engine)
    except Exception:
        engine = "xlrd" if engine == "openpyxl" else "openpyxl"
        xf = pd.ExcelFile(io.BytesIO(data), engine=engine)
    all_tables = []
    for sheet in xf.sheet_names:
        df = pd.read_excel(xf, sheet_name=sheet, header=None, dtype=str)
        df = df.dropna(how="all").fillna("")
        if df.empty:
            continue
        all_tables.append(df.values.tolist())
    groups = group_tables_by_columns(all_tables)
    paths = []
    for i, rows in enumerate(groups):
        suffix = f"_topic{i+1}" if len(groups) > 1 else ""
        out = out_dir / f"{base_name}{suffix}.csv"
        with open(out, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, quoting=csv.QUOTE_ALL)
            writer.writerows(rows)
        paths.append(out)
    return paths


def convert_to_csv(data: bytes, ext: str, out_dir: Path, base_name: str) -> list[Path]:
    if ext in (".xlsx", ".xls", ".xlsm"):
        return excel_to_csvs(data, ext, out_dir, base_name)
    elif ext == ".csv":
        out = out_dir / f"{base_name}.csv"
        out.write_bytes(data)
        return [out]
    elif ext in (".pdf", ".doc", ".docx", ".ppt", ".pptx"):
        log.info(f"  [SKIP] {ext} conversion deferred to post-processing: {base_name}")
        return []
    else:
        log.info(f"  [SKIP] Unsupported ext for conversion: {ext}")
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
# A) Circulars via RSS Feed
# ---------------------------------------------------------------------------

def scrape_circulars():
    folder = FOLDERS["circulars"]
    log.info("=" * 60)
    log.info("A) Circulars (RSS Feed)")
    log.info("=" * 60)

    all_circulars = []

    log.info(f"  Fetching RSS feed: {CIRCULARS_RSS_URL}")
    text = fetch_page(CIRCULARS_RSS_URL)
    if not text:
        log.error("  Could not fetch RSS feed")
        return

    # Parse RSS XML
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        # Might be HTML wrapping RSS; try to extract XML
        log.warning("  RSS parse failed, trying to extract XML from HTML")
        soup = BeautifulSoup(text, "lxml")
        # Try finding RSS content
        rss_text = soup.get_text()
        try:
            root = ET.fromstring(rss_text)
        except ET.ParseError:
            log.error("  Could not parse RSS feed at all")
            # Fallback: try scraping as HTML for links
            links = find_file_links(text, CIRCULARS_RSS_URL)
            for label, url in links:
                process_file(label, url, folder)
            return

    # Handle RSS namespaces
    ns = {}
    for event, (prefix, uri) in ET.iterparse(io.StringIO(text), events=["start-ns"]):
        if prefix:
            ns[prefix] = uri

    # Find all items in the RSS feed
    items = root.findall(".//item")
    if not items:
        # Try with channel
        items = root.findall(".//channel/item")
    if not items:
        # Try Atom entries
        items = root.findall(".//{http://www.w3.org/2005/Atom}entry")

    log.info(f"  Found {len(items)} items in RSS feed")

    for item in items:
        circular = {}

        # Extract title
        title_el = item.find("title")
        if title_el is not None and title_el.text:
            circular["title"] = title_el.text.strip()

        # Extract link/URL
        link_el = item.find("link")
        if link_el is not None and link_el.text:
            circular["link"] = link_el.text.strip()
        elif link_el is not None and link_el.get("href"):
            circular["link"] = link_el.get("href").strip()

        # Extract date
        pub_date_el = item.find("pubDate")
        if pub_date_el is not None and pub_date_el.text:
            circular["date"] = pub_date_el.text.strip()
        else:
            date_el = item.find("{http://purl.org/dc/elements/1.1/}date")
            if date_el is not None and date_el.text:
                circular["date"] = date_el.text.strip()

        # Extract description
        desc_el = item.find("description")
        if desc_el is not None and desc_el.text:
            circular["description"] = desc_el.text.strip()

        # Extract enclosure (PDF link)
        enclosure = item.find("enclosure")
        if enclosure is not None:
            circular["pdf_url"] = enclosure.get("url", "")

        # Try to find PDF URL in description HTML
        if not circular.get("pdf_url") and circular.get("description"):
            desc_soup = BeautifulSoup(circular["description"], "lxml")
            for a in desc_soup.find_all("a", href=True):
                href = a["href"]
                if "/-/media/" in href or href.endswith(".pdf") or href.endswith(".ashx"):
                    if href.startswith("/"):
                        href = BASE_URL + href
                    circular["pdf_url"] = href
                    break

        # Try to find PDF URL in link if it points to media
        if not circular.get("pdf_url") and circular.get("link"):
            link = circular["link"]
            if "/-/media/" in link or link.endswith(".ashx"):
                circular["pdf_url"] = link

        # Extract reference from title if present (e.g., "XX-2026")
        if circular.get("title"):
            ref_match = re.search(r'(\d+[-/]\d{4})', circular["title"])
            if ref_match:
                circular["reference"] = ref_match.group(1)

        if circular.get("title"):
            all_circulars.append(circular)

    # Save circulars index CSV
    if all_circulars:
        out = folder / "circulars.csv"
        fieldnames = ["reference", "title", "date", "description", "pdf_url", "link"]
        with open(out, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL,
                                    extrasaction="ignore")
            writer.writeheader()
            writer.writerows(all_circulars)
        log.info(f"  Saved {len(all_circulars)} circulars to {out.name}")

    # Download PDFs
    pdf_count = 0
    for circ in all_circulars:
        pdf_url = circ.get("pdf_url")
        if pdf_url:
            title = circ.get("title", f"circular_{pdf_count}")
            process_file(title, pdf_url, folder)
            pdf_count += 1

    if not all_circulars:
        log.warning("  No circulars found in RSS feed")
        # Fallback: treat as HTML page and look for file links
        links = find_file_links(text, CIRCULARS_RSS_URL)
        log.info(f"  Fallback: found {len(links)} file links in page")
        for label, url in links:
            process_file(label, url, folder)


# ---------------------------------------------------------------------------
# B) Policies & Manuals
# ---------------------------------------------------------------------------

def scrape_policies():
    folder = FOLDERS["policies"]
    log.info("=" * 60)
    log.info("B) Policies & Manuals")
    log.info("=" * 60)

    html = fetch_page(POLICIES_URL)
    if not html:
        log.error("  Could not fetch policies page")
        return

    soup = BeautifulSoup(html, "lxml")
    policies = []

    # Find all links to Sitecore media (/-/media/*.ashx)
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if "/-/media/" in href or href.endswith(".pdf"):
            label = a.get_text(strip=True) or Path(href.split("?")[0]).stem
            if href.startswith("/"):
                full_url = BASE_URL + href
            elif href.startswith("http"):
                full_url = href
            else:
                full_url = BASE_URL + "/" + href

            # Try to extract reference and date from surrounding context
            parent = a.find_parent(["tr", "li", "div", "p"])
            ref = ""
            date = ""
            if parent:
                text = parent.get_text(" ", strip=True)
                ref_match = re.search(r'([A-Z]{2,}/[A-Z]*/?V?\d*/?\d*)', text)
                if ref_match:
                    ref = ref_match.group(1)
                date_match = re.search(r'(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4})', text)
                if date_match:
                    date = date_match.group(1)

            policies.append({
                "title": label,
                "reference": ref,
                "date": date,
                "url": full_url,
            })

    log.info(f"  Found {len(policies)} policy/manual links")

    # Save policies index
    if policies:
        out = folder / "policies_index.csv"
        fieldnames = ["title", "reference", "date", "url"]
        with open(out, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerows(policies)
        log.info(f"  Saved policies index to {out.name}")

    # Download and convert each policy PDF
    for pol in policies:
        process_file(pol["title"], pol["url"], folder)

    # Also check for any other downloadable files on the page
    links = find_file_links(html, POLICIES_URL)
    existing_urls = {p["url"] for p in policies}
    for label, url in links:
        if url not in existing_urls:
            process_file(label, url, folder)


# ---------------------------------------------------------------------------
# C) Medications & Supplements
# ---------------------------------------------------------------------------

def scrape_medications_supplements():
    folder = FOLDERS["medications"]
    log.info("=" * 60)
    log.info("C) Medications & Supplements Awareness")
    log.info("=" * 60)

    html = fetch_page(SUPPLEMENTS_URL)
    if not html:
        log.error("  Could not fetch supplements page")
        return

    links = find_file_links(html, SUPPLEMENTS_URL)
    log.info(f"  Found {len(links)} file links")
    for label, url in links:
        process_file(label, url, folder)

    if not links:
        # Try to find links in a broader way (Sitecore media pattern)
        soup = BeautifulSoup(html, "lxml")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if "/-/media/" in href:
                label = a.get_text(strip=True) or "supplement_doc"
                full_url = BASE_URL + href if href.startswith("/") else href
                process_file(label, full_url, folder)


# ---------------------------------------------------------------------------
# D) Guidelines (JS-rendered, best effort)
# ---------------------------------------------------------------------------

def scrape_guidelines():
    folder = FOLDERS["guidelines"]
    log.info("=" * 60)
    log.info("D) Guidelines")
    log.info("=" * 60)

    html = fetch_page(GUIDELINES_URL)
    if not html:
        log.error("  Could not fetch guidelines page")
        return

    links = find_file_links(html, GUIDELINES_URL)
    log.info(f"  Found {len(links)} file links from static HTML")

    if links:
        for label, url in links:
            process_file(label, url, folder)
    else:
        log.warning("  Guidelines page uses JS-rendered tables.")
        log.warning("  No downloadable links found in static HTML.")
        log.warning("  Consider using browser automation or finding an API endpoint.")

        # Try to find any Sitecore media links anyway
        soup = BeautifulSoup(html, "lxml")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if "/-/media/" in href:
                label = a.get_text(strip=True) or "guideline_doc"
                full_url = BASE_URL + href if href.startswith("/") else href
                process_file(label, full_url, folder)
                break


# ---------------------------------------------------------------------------
# E) Standards (JS-rendered, best effort)
# ---------------------------------------------------------------------------

def scrape_standards():
    folder = FOLDERS["standards"]
    log.info("=" * 60)
    log.info("E) Standards")
    log.info("=" * 60)

    html = fetch_page(STANDARDS_URL)
    if not html:
        log.error("  Could not fetch standards page")
        return

    links = find_file_links(html, STANDARDS_URL)
    log.info(f"  Found {len(links)} file links from static HTML")

    if links:
        for label, url in links:
            process_file(label, url, folder)
    else:
        log.warning("  Standards page uses JS-rendered tables.")
        log.warning("  No downloadable links found in static HTML.")
        log.warning("  Consider using browser automation or finding an API endpoint.")

        # Try to find any Sitecore media links anyway
        soup = BeautifulSoup(html, "lxml")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if "/-/media/" in href:
                label = a.get_text(strip=True) or "standard_doc"
                full_url = BASE_URL + href if href.startswith("/") else href
                process_file(label, full_url, folder)
                break


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("=" * 60)
    log.info("DOH (Abu Dhabi Department of Health) Scraper")
    log.info("=" * 60)

    # Create folders
    for folder in FOLDERS.values():
        folder.mkdir(parents=True, exist_ok=True)

    # Initialize session
    init_session()

    # Run all scrapers
    scrape_circulars()
    scrape_policies()
    scrape_medications_supplements()
    scrape_guidelines()
    scrape_standards()

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
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from process_files import process_scraper
    log.info("Post-processing downloaded files with MiniMax...")
    process_scraper(BASE_DIR)


if __name__ == "__main__":
    main()
