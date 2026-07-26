#!/usr/bin/env python3
"""
NHRA Bahrain scraper — downloads and converts all available lists to CSV.

PPR page uses JS tabs (AJAX-loaded), so files are sourced from OpenData mirror pages.
"""

import os
import re
import time
import csv
import requests
from pathlib import Path
from bs4 import BeautifulSoup
import pandas as pd

BASE_DIR = Path(__file__).parent
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

def init_session():
    """Visit homepage to get session cookies."""
    try:
        SESSION.get("https://www.nhra.bh/", timeout=15)
    except Exception:
        pass

init_session()

# ---------------------------------------------------------------------------
# Folder definitions
# ---------------------------------------------------------------------------
PPR = BASE_DIR / "Pharmacy & Pharmaceutical Products Regulation"
CT  = BASE_DIR / "Clinical Trials & CPD Regulation Section"

FOLDERS = {
    "medicines":       PPR / "Medicines",
    "health_products": PPR / "Health Products",
    "alt_comp":        PPR / "Alternative & Complementary",
    "pharma_fac":      PPR / "Pharmaceutical Facilities",
    "approved_ct":     CT  / "List of Approved CT",
}

for f in FOLDERS.values():
    f.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# OpenData source pages — each yields file links for the mapped folder
# ---------------------------------------------------------------------------
OPEN_DATA_SOURCES = [
    ("https://www.nhra.bh/OpenData/Licensed%20Medicines/",              "medicines"),
    ("https://www.nhra.bh/OpenData/Licensed%20Health%20Products/",      "health_products"),
    ("https://www.nhra.bh/OpenData/Alternative%20and%20Complementary/", "alt_comp"),
    ("https://www.nhra.bh/OpenData/Licensed%20Pharmacies/",             "pharma_fac"),
    ("https://www.nhra.bh/OpenData/Approved%20Clinical%20Trials/",      "approved_ct"),
]

# Fallback: directly-known URLs if OpenData pages are missing/empty
FALLBACK_FILES = {
    "medicines": [
        ("Registered Medicine Price List 20260614",
         "https://www.nhra.bh/MediaHandler/GenericHandler/documents/departments/PPR/Medicines/List/PPR_LISTS_Registered Medicine Price List_20260614.xlsx"),
        ("Temporary Importation Medicines List 20250119",
         "https://www.nhra.bh/MediaHandler/GenericHandler/documents/departments/PPR/Medicines/List/PPR_LISTS_Temporary Importation Medicines List_20250119.xlsx"),
        ("Innovator Price List 18 Feb 2024",
         "https://www.nhra.bh/MediaHandler/GenericHandler/documents/departments/PPR/Medicines/List/Innovator Price list - Implementation Date 18 Feb 2024.xlsx"),
        ("Generic Price List 18 Feb 2024",
         "https://www.nhra.bh/MediaHandler/GenericHandler/documents/departments/PPR/Medicines/List/Generics Price List - Implementation Date 18 Feb 2024.xlsx"),
        ("Cancelled Medicine List 20240720",
         "https://www.nhra.bh/MediaHandler/GenericHandler/documents/departments/PPR/Medicines/List/PPR_LIST_Cancelled Medicines List_20240720.xlsx"),
        ("General Sale Medicine List 20190613",
         "https://www.nhra.bh/MediaHandler/GenericHandler/documents/departments/PPR/Medicines/List/PPR_LISTS_General Sale Medicines List_20190613.pdf"),
        ("List of Approved BEQ Centres by GCC 20250406",
         "https://www.nhra.bh/MediaHandler/GenericHandler/documents/departments/PPR/Medicines/List/PPR_List_List of BEQ Centres by GCC_20250406.pdf"),
    ],
    "health_products": [
        ("Registered Health Product List - 14 June 2026",
         "https://www.nhra.bh/MediaHandler/GenericHandler/documents/departments/PPR/2026/PPR_LIST_Registered%20Health%20Product%20List_20260614.xlsx"),
        ("Cancelled Health Product List By Companies Agent - 14 June 2026",
         "https://www.nhra.bh/MediaHandler/GenericHandler/documents/departments/PPR/Health Products/PPR_LIST_Cancelled Health Products List_20230313.xlsx"),
        ("General Sale Health Products List",
         "https://www.nhra.bh/MediaHandler/GenericHandler/documents/departments/PPR/Health Products/PPR_List_General Sale Health Products List_20201110.pdf"),
    ],
    "alt_comp": [
        ("Approved Alternative and Complementary Medicine List - 14 June 2026",
         "https://www.nhra.bh/MediaHandler/GenericHandler/documents/departments/PPR/Alternative Complementary/PPR_LISTS_Approved Alternative and Complementary Medicine List_20260210.xlsx"),
    ],
    "pharma_fac": [
        ("Licensed Warehouses - 14 June 2026",
         "https://www.nhra.bh/MediaHandler/GenericHandler/documents/departments/PPR/2026/PPR_LIST_Licensed%20Warehouses_20260614.xlsx"),
    ],
    "approved_ct": [
        ("List of Approved Clinical Trials 2021-2022",
         "https://www.nhra.bh/MediaHandler/GenericHandler/documents/departments/CT/Statistics/Approved CT Report 21-22.pdf"),
        ("List of Approved Clinical Trials 2020-2016",
         "https://www.nhra.bh/MediaHandler/GenericHandler/documents/departments/CT/Statistics/Ct in Year 2016, 2017 AND 2018.pptx"),
    ],
}

DOWNLOAD_EXTS = {".xlsx", ".xls", ".xlsm", ".csv", ".pdf", ".doc", ".docx", ".dotx", ".pptx", ".ppt"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe_name(text: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', '', text).strip().rstrip('.')
    return name or "file"


def fetch_page(url: str):
    try:
        r = SESSION.get(url, timeout=30)
        r.raise_for_status()
        ct = r.headers.get("Content-Type", "")
        if "html" not in ct:
            return None
        return r.text
    except Exception as e:
        print(f"  [WARN] Could not fetch {url}: {e}")
        return None


def find_file_links(html: str, base_url: str):
    from urllib.parse import urlparse
    soup = BeautifulSoup(html, "lxml")
    results = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        ext = Path(href.split("?")[0]).suffix.lower()
        if ext not in DOWNLOAD_EXTS:
            continue
        label = a.get_text(strip=True) or Path(href).stem
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


def download_file(url: str) -> tuple:
    from urllib.parse import urlparse, quote, urlunparse
    # Pre-encode spaces before urlparse (spaces break urlparse path splitting)
    url_preencoded = url.replace(" ", "%20")
    p = urlparse(url_preencoded)
    encoded_path = quote(p.path, safe="/:@!$&'()*+,;=%")
    url = urlunparse(p._replace(path=encoded_path))
    headers = {"Referer": "https://www.nhra.bh/Departments/PPR/"}
    r = SESSION.get(url, timeout=120, stream=True, headers=headers)
    r.raise_for_status()
    ct = r.headers.get("Content-Type", "")
    if "html" in ct:
        raise ValueError("Got HTML (server error page)")
    content = b"".join(r.iter_content(65536))
    ext = Path(url.split("?")[0]).suffix.lower()
    return content, ext


# ---------------------------------------------------------------------------
# Conversion functions
# ---------------------------------------------------------------------------

def group_tables_by_columns(tables: list) -> list:
    """
    Group a list of row-lists by their column count (same col count = same topic).
    Returns list of (group_key, merged_rows).
    Preserves order of first appearance.
    """
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


def excel_to_csvs(data: bytes, ext: str, out_dir: Path, base_name: str) -> list:
    import io
    engine = "openpyxl" if ext in (".xlsx", ".xlsm", ".dotx") else "xlrd"
    try:
        xf = pd.ExcelFile(io.BytesIO(data), engine=engine)
    except Exception:
        engine = "xlrd" if engine == "openpyxl" else "openpyxl"
        xf = pd.ExcelFile(io.BytesIO(data), engine=engine)
    # Collect all sheets as row lists
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


def convert_to_csv(data: bytes, ext: str, out_dir: Path, base_name: str) -> list:
    if ext in (".xlsx", ".xls", ".xlsm"):
        return excel_to_csvs(data, ext, out_dir, base_name)
    elif ext == ".csv":
        out = out_dir / f"{base_name}.csv"
        out.write_bytes(data)
        return [out]
    elif ext in (".pdf", ".doc", ".docx", ".dotx", ".ppt", ".pptx"):
        print(f"    [SKIP] {ext} conversion deferred to post-processing (MiniMax LLM)")
        return []
    else:
        print(f"    [SKIP] Unsupported extension: {ext}")
        return []


# ---------------------------------------------------------------------------
# Process a single file
# ---------------------------------------------------------------------------

def process_file(label: str, url: str, folder: Path):
    name = safe_name(label)
    print(f"  -> {label}")
    try:
        data, ext = download_file(url)
    except Exception as e:
        print(f"     [ERROR] Download: {e}")
        return
    print(f"     {len(data):,} bytes  ext={ext}")
    # Save raw file to disk (needed for deferred conversion types)
    raw_path = folder / f"{name}{ext}"
    raw_path.write_bytes(data)
    try:
        csvs = convert_to_csv(data, ext, folder, name)
    except Exception as e:
        print(f"     [ERROR] Convert: {e}")
        return
    if csvs:
        for c in csvs:
            print(f"     -> {c.name}")
    elif ext in (".pdf", ".doc", ".docx", ".dotx", ".ppt", ".pptx"):
        print(f"     -> saved {raw_path.name} (deferred)")
    else:
        print(f"     [WARN] No CSV output")
    time.sleep(0.5)


# ---------------------------------------------------------------------------
# Scrape page for file links
# ---------------------------------------------------------------------------

def scrape_page(url: str, folder: Path) -> int:
    print(f"\n[Page] {url}")
    html = fetch_page(url)
    if not html:
        return 0
    links = find_file_links(html, url)
    print(f"  Found {len(links)} file link(s)")
    for label, file_url in links:
        process_file(label, file_url, folder)
    return len(links)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# AJAX tab data-ids for PPR department page
# Each entry: (article_id, folder_key)
AJAX_TABS = [
    (219, "health_products"),   # Health Products > Lists
    (223, "alt_comp"),          # Alternative & Complementary > Lists
    # Medicines and Pharmaceutical Facilities are covered by OpenData pages
]

def scrape_ajax_tab(article_id: int, folder: Path) -> int:
    import json, random
    url = f"https://www.nhra.bh/Handlers/DepartmentArticleGrabber.ashx?id={article_id}&r={random.random()}"
    print(f"\n[AJAX tab {article_id}] {folder.name}")
    try:
        r = SESSION.get(url, timeout=20, headers={"Referer": "https://www.nhra.bh/Departments/PPR/"})
        r.raise_for_status()
        data = r.json()
        html = data.get("html", "")
    except Exception as e:
        print(f"  [WARN] {e}")
        return 0
    links = find_file_links(html, "https://www.nhra.bh/")
    print(f"  Found {len(links)} file(s)")
    for label, file_url in links:
        process_file(label, file_url, folder)
    return len(links)


def main():
    print("=" * 60)
    print("NHRA Bahrain Scraper")
    print("=" * 60)

    found_counts = {}

    # Scrape OpenData pages
    for page_url, folder_key in OPEN_DATA_SOURCES:
        folder = FOLDERS[folder_key]
        count = scrape_page(page_url, folder)
        found_counts[folder_key] = found_counts.get(folder_key, 0) + count

    # Scrape AJAX tab content for JS-driven sections
    for article_id, folder_key in AJAX_TABS:
        folder = FOLDERS[folder_key]
        count = scrape_ajax_tab(article_id, folder)
        found_counts[folder_key] = found_counts.get(folder_key, 0) + count

    # Also try the HCP department page for CT links
    scrape_page("https://nhra.bh/Departments/HCP/", FOLDERS["approved_ct"])

    # Apply fallbacks — always run (done markers prevent re-downloading)
    for folder_key, file_list in FALLBACK_FILES.items():
        folder = FOLDERS[folder_key]
        print(f"\n[Fallback] {folder_key}")
        for label, url in file_list:
            process_file(label, url, folder)

    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    for key, folder in FOLDERS.items():
        csvs = list(folder.glob("*.csv"))
        print(f"  {folder.relative_to(BASE_DIR)}: {len(csvs)} CSV file(s)")

    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
if __name__ == "__main__":
    main()
