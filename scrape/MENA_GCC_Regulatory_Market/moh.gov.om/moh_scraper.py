#!/usr/bin/env python3
"""
MOH Oman Drug Safety Center — Data Scraper
===========================================
Scrapes the 10 resource categories from:
  https://www.moh.gov.om/en/hospitals-directorates/directorates-and-centers-at-hq/drug-safety-center/

For each category:
  - Discovers all available files (PDF / Excel)
  - Downloads all files (clean full scrape each run)
  - Extracts every table into separate CSV files

Usage:
  python moh_scraper.py                    # scrape all categories
  python moh_scraper.py --category "Banned_Adulterated_Products"  # single category
"""

import argparse
import csv
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


try:
    import openpyxl
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL    = "https://www.moh.gov.om"
PAGE_URL    = BASE_URL + "/en/hospitals-directorates/directorates-and-centers-at-hq/drug-safety-center/"
OUTPUT_DIR  = Path(__file__).parent / "moh_data"
REQUEST_DELAY = 1.5
TODAY       = datetime.now().strftime("%Y-%m-%d")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Category mapping: data-description substring → folder name ────────────────
CATEGORIES: Dict[str, str] = {
    "registered pharmaceutical products list with prices":
        "Registered_Pharmaceutical_Products_List_with_Prices",
    "list of banned /adulterated products":
        "Banned_Adulterated_Products",
    "list of registered pharmaceutical manufactures":
        "List_of_Registered_Pharmaceutical_Manufacturers_and_Products",
    "list of herbal companies":
        "List_of_Registered_Herbal_Companies_and_Products",
    "list of registered health products":
        "List_of_Registered_Health_Products",
    "list of registered medicated medical devices":
        "List_of_Registered_Medicated_Medical_Devices",
    "list of licensed medical stores":
        "List_of_Licensed_Medical_Stores",
    "list of bioequivalence centers approved locally":
        "List_of_Bioequivalence_Centers_Approved_Locally_GCC",
    "moh hs code list 2026":
        "MOH_HS_Code_List_2026",
    "medical device supplier approved list":
        "Medical_Device_Supplier_Approved_List",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
}


# ── Utilities ─────────────────────────────────────────────────────────────────

def safe_name(s: str) -> str:
    """Convert a string to a safe filename."""
    s = re.sub(r"[^\w\s-]", "", s).strip()
    s = re.sub(r"[\s]+", "_", s)
    return s[:80]


def download_file(url: str, dest: Path) -> bool:
    """Download a file. Returns True on success."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=120, stream=True)
        r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
        log.info("  Downloaded: %s (%d KB)", dest.name, dest.stat().st_size // 1024)
        return True
    except Exception as e:
        log.error("  Download failed for %s: %s", url, e)
        return False


# ── Table extraction ──────────────────────────────────────────────────────────

def clean_cell(val) -> str:
    if val is None:
        return ""
    return str(val).strip().replace("\n", " ").replace("\r", "")


def extract_tables_pdf(pdf_path: Path) -> List[Tuple[str, List[List[str]]]]:
    """
    Extract all tables from a PDF.
    Skipped here — will be processed by post-processor.
    """
    log.info("  Skipping PDF conversion for %s, will be processed by post-processor", pdf_path.name)
    return []


# ── Table post-processing ────────────────────────────────────────────────────

def _trim_empty_cols(rows: List[List[str]]) -> List[List[str]]:
    """Remove trailing columns where every row value is empty."""
    if not rows:
        return rows
    max_cols = max(len(r) for r in rows)
    last_meaningful = -1
    for col_i in range(max_cols):
        for row in rows:
            if col_i < len(row) and row[col_i].strip():
                last_meaningful = col_i
                break
    if last_meaningful < 0:
        return rows
    return [r[:last_meaningful + 1] for r in rows]


def _is_title_row(row: List[str]) -> bool:
    """Title row: only col 0 has content, rest empty, and row has >2 cols."""
    if len(row) <= 2:
        return False
    non_empty = [c for c in row if c.strip()]
    return len(non_empty) == 1 and bool(row[0].strip())


def _is_overflow_fragment(rows: List[List[str]]) -> bool:
    """Single-column table with no structured-data keywords → PDF text overflow artifact."""
    if not rows or len(rows[0]) > 2:
        return False
    # Check ALL header cells so we don't miss keywords in col 1 when col 0 is empty
    header_text = " ".join(c.strip() for c in rows[0] if c)
    structured = re.compile(
        r'\b(No\.|Name|Date|Code|Price|Regn|Trade|Type|Rate|Address|'
        r'Company|Approval|Expiry|Duty|Product|Group|Agent|Status|'
        r'Establishment|Manufacturer|Country|License|Registration|Centre|Research)\b',
        re.I,
    )
    return not structured.search(header_text)


def _clean_bilingual_header(headers: List[str]) -> List[str]:
    """Strip Arabic text from bilingual header cells, keeping only the English part."""
    cleaned = []
    for cell in headers:
        text = cell.strip()
        if not text:
            cleaned.append(text)
            continue
        if "/" in text:
            english = text.split("/")[0].strip()
            english = re.sub(r"[؀-ۿ]+", "", english).strip()
            english = re.sub(r"\s+", " ", english).strip()
            cleaned.append(english if english else text)
        else:
            english = re.sub(r"[؀-ۿ]+", "", text).strip()
            english = re.sub(r"\s+", " ", english).strip()
            cleaned.append(english if english else text)
    return cleaned


def _is_merged_bilingual_header(row: List[str]) -> bool:
    """Row where col 0 has very long bilingual text and all other cells are empty."""
    if len(row) < 3:
        return False
    non_empty = [c for c in row if c.strip()]
    if len(non_empty) != 1:
        return False
    text = row[0]
    return len(text) > 30 and bool(re.search(r"[؀-ۿ]", text))


def _process_hs_code_tables(
    tables: List[Tuple[str, List[List[str]]]]
) -> List[Tuple[str, List[List[str]]]]:
    """Keep only 10-col HS Code data tables, merge description cols, produce one CSV."""
    HS_HEADER = ["Duty Rate", "Product Type", "Description (EN)",
                 "Description (AR)", "HS Code", "Heading"]
    data_rows: List[List[str]] = []

    for _label, rows in tables:
        if not rows or len(rows[0]) != 10:
            continue
        first_text = " ".join(rows[0]).upper()
        if "DUTY RATE" in first_text or "H.S CODE" in first_text or "DESCRIPTION" in first_text:
            rows = rows[1:]  # skip the PDF's own header row
        for row in rows:
            duty     = row[0].strip()
            ptype    = row[1].strip()
            desc_en  = " ".join(c.strip() for c in row[2:5] if c.strip())
            desc_ar  = " ".join(c.strip() for c in row[5:8] if c.strip())
            hs_code  = row[8].strip()
            heading  = row[9].strip()
            if any([duty, ptype, desc_en, desc_ar, hs_code, heading]):
                data_rows.append([duty, ptype, desc_en, desc_ar, hs_code, heading])

    if not data_rows:
        return []
    return [("table_1", [HS_HEADER] + data_rows)]


def _process_medicated_devices(
    tables: List[Tuple[str, List[List[str]]]]
) -> List[Tuple[str, List[List[str]]]]:
    """Merge fragment tables from Medicated Medical Devices PDF into one clean table."""
    HEADER = ["No.", "Regn. No.", "Trade Name", "Pack Size",
              "Local Agent", "Marketing Company", "Main Group"]
    all_data: List[List[str]] = []

    for _label, rows in tables:
        if not rows:
            continue
        first = rows[0]
        # Skip merged header row (single cell with all column names)
        if _is_merged_bilingual_header(first) or (
            len(first) >= 2 and
            any(kw in first[0] for kw in ("Regn. No.", "Trade Name", "Sl."))
        ):
            rows = rows[1:]
        for row in rows:
            if len(row) == 6:
                row = [""] + row   # no "No." column in this fragment
            elif len(row) < 7:
                row = row + [""] * (7 - len(row))
            all_data.append(row[:7])

    if not all_data:
        return []
    return [("table_1", [HEADER] + all_data)]


def _process_bioequivalence(
    tables: List[Tuple[str, List[List[str]]]]
) -> List[Tuple[str, List[List[str]]]]:
    """
    Fix Bioequivalence Centers tables:
    - GCC 5/6-col table: extract No.(col 0) and Centre (last non-empty col)
    - Local-centers 5/6-col table: merge Name cols, keep Address/Dates
    - 7-col table (second format): Name(col 0), Address(col 4), Dates(5,6)
    - 4-col with dates: keep as local-centers continuation
    """
    result = []
    date_like = re.compile(r"\d{1,2}/\d{1,2}/\d{4}|\d{4}/\d{1,2}")

    for label, rows in tables:
        if not rows:
            continue
        ncols = len(rows[0])
        hdr = rows[0]
        meaningful = sum(1 for c in hdr if c.strip())

        # ── 5 or 6-col tables ───────────────────────────────────────────────
        if ncols >= 5:
            # GCC detection: "No." in col 1 OR "Bioequivalence" anywhere in header
            gcc_detected = (
                (len(hdr) > 1 and hdr[1].strip() == "No.") or
                any("Bioequivalence" in c for c in hdr)
            )
            if gcc_detected:
                new_hdr = ["No.", "Bioequivalence Centre"]
                new_rows = [new_hdr]
                for row in rows[1:]:
                    no = row[0].strip()
                    # Centre text in last non-empty col, prefer col 3 or 4
                    centre = ""
                    for ci in (3, 4, 5, 1, 2):
                        if ci < len(row) and row[ci].strip():
                            centre = row[ci].strip()
                            break
                    if no or centre:
                        new_rows.append([no, centre])
                if len(new_rows) > 1:
                    result.append((label, new_rows))
                continue

            # Local-centers pattern: Name(cols 0..n-3), Address(-3), Approval(-2), Expiry(-1)
            new_hdr = ["Name", "Address", "Date of Approval", "Date of Expiry"]
            new_rows = [new_hdr]
            name_end = max(1, ncols - 3)
            for row in rows[1:]:
                padded = row + [""] * max(0, ncols - len(row))
                name     = " ".join(c.strip() for c in padded[:name_end] if c.strip())
                address  = padded[name_end].strip() if name_end < len(padded) else ""
                approval = padded[name_end + 1].strip() if name_end + 1 < len(padded) else ""
                expiry   = padded[name_end + 2].strip() if name_end + 2 < len(padded) else ""
                if name or address:
                    new_rows.append([name, address, approval, expiry])
            if len(new_rows) > 1:
                result.append((label, new_rows))
            continue

        # ── 7-col tables (second local-centers format) ───────────────────────
        if ncols == 7 and meaningful >= 2:
            new_hdr = ["Name", "Address", "Date of Approval", "Date of Expiry"]
            new_rows = [new_hdr]
            for row in rows:
                padded = row + [""] * max(0, 7 - len(row))
                name     = padded[0].strip()
                address  = padded[4].strip()
                approval = padded[5].strip()
                expiry   = padded[6].strip()
                if name or address:
                    new_rows.append([name, address, approval, expiry])
            if len(new_rows) > 1:
                result.append((label, new_rows))
            continue

        # ── 4-col tables ─────────────────────────────────────────────────────
        if ncols == 4 and meaningful >= 2:
            has_dates = (len(rows) > 1 and
                         any(date_like.search(c) for c in rows[1][2:4] if c))
            looks_like_hdr = any(kw in hdr[0] for kw in ("Name", "No.", "Sr.", "Sl."))
            if has_dates or looks_like_hdr:
                if not looks_like_hdr:
                    rows = [["Name", "Address", "Date of Approval", "Date of Expiry"]] + rows
                result.append((label, rows))
            continue

        # ── 2-col tables ─────────────────────────────────────────────────────
        if ncols == 2 and meaningful >= 1:
            # Overflow fragment: col 0 empty, col 1 has address continuation → skip
            if not hdr[0].strip() and hdr[1].strip():
                continue
            # Data-first table (no header row): first cell is a number → add header
            if re.match(r"^\d+\.?", hdr[0].strip()):
                rows = [["No.", "Bioequivalence Centre"]] + rows
            if any(c.strip() for row in rows[1:] for c in row):
                result.append((label, rows))

    return result


def post_process_tables(
    tables: List[Tuple[str, List[List[str]]]],
    category_name: str,
    base_csv_name: str,
) -> List[Tuple[str, List[List[str]]]]:
    """Apply general cleanup and dataset-specific fixes to extracted tables."""

    # ── General cleanup ──────────────────────────────────────────────────────
    cleaned: List[Tuple[str, List[List[str]]]] = []
    for label, rows in tables:
        rows = [r for r in rows if any(c.strip() for c in r)]
        if not rows:
            continue
        max_w = max(len(r) for r in rows)
        rows = [r + [""] * (max_w - len(r)) for r in rows]
        rows = _trim_empty_cols(rows)
        while rows and _is_title_row(rows[0]):
            rows = rows[1:]
        if not rows:
            continue
        if _is_overflow_fragment(rows):
            continue
        cleaned.append((label, rows))

    if not cleaned:
        return []

    cat = category_name

    # ── HS Code: merge all 10-col data tables into one clean table ───────────
    if cat == "MOH_HS_Code_List_2026":
        return _process_hs_code_tables(cleaned)

    # ── Pharma Price List: skip "FOR SEARCH PRESS F3" instruction row ────────
    if cat == "Registered_Pharmaceutical_Products_List_with_Prices":
        result = []
        for label, rows in cleaned:
            if rows and "FOR SEARCH" in rows[0][0].upper():
                rows = rows[1:]
            if rows:
                result.append((label, rows))
        return result

    # ── Health Products: prepend standard header (bilingual row was stripped) ──
    if cat == "List_of_Registered_Health_Products":
        HEALTH_HEADER = ["S. No.", "Regn. No.", "Trade Name", "Pack Size",
                         "Local Agent", "Marketing Company", "Main Group"]
        result = []
        for label, rows in cleaned:
            if not rows:
                continue
            # If first row looks like data (starts with a number), insert header
            try:
                int(rows[0][0].strip())
                ncols = len(rows[0])
                hdr = HEALTH_HEADER[:ncols] + [""] * max(0, ncols - len(HEALTH_HEADER))
                rows = [hdr] + rows
            except (ValueError, IndexError):
                pass
            result.append((label, rows))
        return result

    # ── Medicated Medical Devices: merge 3 fragment tables into one ──────────
    if cat == "List_of_Registered_Medicated_Medical_Devices":
        return _process_medicated_devices(cleaned)

    # ── Bioequivalence Centers: fix structure ─────────────────────────────────
    if cat == "List_of_Bioequivalence_Centers_Approved_Locally_GCC":
        return _process_bioequivalence(cleaned)

    # ── Medical Device Supplier: remove empty cols, fix header ────────────────
    if cat == "Medical_Device_Supplier_Approved_List":
        result = []
        for label, rows in cleaned:
            if not rows:
                continue
            ncols = max(len(r) for r in rows)
            non_empty_cols = [
                i for i in range(ncols)
                if any(i < len(r) and r[i].strip() for r in rows)
            ]
            if not non_empty_cols:
                continue
            trimmed = [[r[i] if i < len(r) else "" for i in non_empty_cols] for r in rows]
            if trimmed and not trimmed[0][0].strip():
                trimmed[0][0] = "No."
            result.append((label, trimmed))
        return result

    # ── Herbal / Pharma files: clean bilingual headers in Excel extracts ─────
    if cat in (
        "List_of_Registered_Herbal_Companies_and_Products",
        "List_of_Registered_Pharmaceutical_Manufacturers_and_Products",
    ):
        result = []
        for label, rows in cleaned:
            if rows and any(re.search(r"[؀-ۿ]", c) for c in rows[0]):
                rows = [_clean_bilingual_header(rows[0])] + rows[1:]
            result.append((label, rows))
        return result

    return cleaned


def extract_tables_excel(xlsx_path: Path) -> List[Tuple[str, List[List[str]]]]:
    """Extract all sheets from an Excel file as tables."""
    if not HAS_OPENPYXL:
        log.error("openpyxl not installed")
        return []
    results = []
    try:
        wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            rows = []
            for row in ws.iter_rows(values_only=True):
                cleaned = [clean_cell(c) for c in row]
                if any(cleaned):
                    rows.append(cleaned)
            if rows:
                label = safe_name(sheet_name) or f"sheet_{len(results)+1}"
                results.append((label, rows))
    except Exception as e:
        log.error("  Excel extraction error for %s: %s", xlsx_path.name, e)
    return results


def tables_to_csv(tables: List[Tuple[str, List[List[str]]]], out_dir: Path,
                  base_name: str) -> List[Path]:
    """Write each table to a CSV file. Returns list of written paths."""
    written = []
    out_dir.mkdir(parents=True, exist_ok=True)
    for label, rows in tables:
        if not rows:
            continue
        # Pad all rows to same width
        max_cols = max(len(r) for r in rows)
        rows = [r + [""] * (max_cols - len(r)) for r in rows]

        fname = f"{base_name}__{label}.csv" if len(tables) > 1 else f"{base_name}.csv"
        path = out_dir / fname
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerows(rows)
        log.info("    CSV: %s (%d rows)", fname, len(rows) - 1)
        written.append(path)
    return written


# ── Discovery ─────────────────────────────────────────────────────────────────

def discover_resources() -> Dict[str, List[Dict]]:
    """
    Fetch the MOH page and return a dict:
      {folder_name: [{"filename": ..., "url": ..., "ext": ...}, ...]}
    """
    log.info("Fetching MOH Drug Safety Center page...")
    r = requests.get(PAGE_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    result: Dict[str, List[Dict]] = {}

    for item in soup.select(".resource-item"):
        desc = item.get("data-description", "").lower().strip()

        folder = None
        for key, val in CATEGORIES.items():
            if key in desc:
                folder = val
                break
        if not folder:
            continue

        files = []
        for span in item.select("span.fileURL[data-fileurl]"):
            fname = span.get("data-filename", "").strip()
            furl  = urljoin(BASE_URL, span["data-fileurl"])
            ext   = furl.rsplit(".", 1)[-1].lower() if "." in furl else "bin"
            files.append({"filename": fname, "url": furl, "ext": ext})

        if files:
            if folder not in result:
                result[folder] = []
            result[folder].extend(files)

    log.info("Discovered %d categories with files", len(result))
    return result


# ── Processing ────────────────────────────────────────────────────────────────

def process_file(file_info: Dict, category_dir: Path) -> bool:
    """
    Download and extract one file.
    Returns True if CSV(s) were written.
    """
    fname   = file_info["filename"]
    url     = file_info["url"]
    ext     = file_info["ext"]

    log.info("  Processing: %s", fname)

    # ── Download ─────────────────────────────────────────────────────────────
    # Store downloads in a _downloads subfolder to keep csvs clean
    dl_dir  = category_dir / "_downloads"
    dl_dir.mkdir(parents=True, exist_ok=True)
    dl_path = dl_dir / f"{safe_name(fname)}.{ext}"

    if not download_file(url, dl_path):
        return False

    time.sleep(REQUEST_DELAY)

    # ── Extract tables ────────────────────────────────────────────────────────
    base_csv_name = safe_name(fname)
    cat_name = category_dir.name  # folder name = category key
    if ext == "pdf":
        tables = extract_tables_pdf(dl_path)
    elif ext in ("xlsx", "xls"):
        tables = extract_tables_excel(dl_path)
    else:
        log.warning("  Unsupported file type: %s — skipping extraction", ext)
        tables = []

    tables = post_process_tables(tables, cat_name, base_csv_name)

    if not tables:
        log.warning("  No tables extracted from %s", fname)
        return False

    tables_to_csv(tables, category_dir, base_csv_name)
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="MOH Oman Drug Safety Center scraper")
    parser.add_argument("--category", help="Process only this folder name (e.g. Banned_Adulterated_Products)")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    resources = discover_resources()

    total_files  = 0
    total_updated = 0

    for folder, files in resources.items():
        if args.category and folder != args.category:
            continue

        log.info("")
        log.info("=" * 60)
        log.info("Category: %s  (%d file(s))", folder, len(files))
        log.info("=" * 60)

        cat_dir = OUTPUT_DIR / folder
        cat_dir.mkdir(parents=True, exist_ok=True)

        for finfo in files:
            total_files += 1
            log.info("  File: %s", finfo["filename"])
            updated = process_file(finfo, cat_dir)
            if updated:
                total_updated += 1

    log.info("")
    log.info("=" * 60)
    log.info("Done — %d files checked, %d updated/new", total_files, total_updated)
    log.info("Output: %s", OUTPUT_DIR)
    log.info("=" * 60)

    # Post-process: extract CSVs from PDF/Word/PPT files using MiniMax
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from process_files import process_scraper
    log.info("Post-processing downloaded files with MiniMax...")
    process_scraper(Path(__file__).parent)


if __name__ == "__main__":
    main()
