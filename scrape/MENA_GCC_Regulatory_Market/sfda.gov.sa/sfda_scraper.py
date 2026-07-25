#!/usr/bin/env python3
"""
SFDA (Saudi Food and Drug Authority) Data Scraper
==================================================
Automatically discovers and extracts all datasets from:
  • Drugs section          https://www.sfda.gov.sa/en/lists-categories?keys=&tags=2
  • Clinical Trials section https://www.sfda.gov.sa/en/lists-categories?keys=&tags=3

For each dataset the script:
  1. Discovers the dataset URL from the listing pages (walks all pagination pages).
  2. Detects the table schema dynamically (skips Drupal modal/action columns).
  3. Walks all pagination pages to collect every record.
  4. Falls back to download links (Excel / CSV) when pages use JS rendering.
  5. Writes one CSV per dataset with source URL and extraction date as metadata.

Requirements
------------
    pip install requests beautifulsoup4 lxml openpyxl
    # For JS-rendered datasets (--playwright flag):
    pip install playwright && playwright install chromium

Usage
-----
    python sfda_scraper.py                          # scrape everything (static pages)
    python sfda_scraper.py --playwright             # also scrape JS-rendered datasets
    python sfda_scraper.py -o my_data               # custom output folder
    python sfda_scraper.py -d 2.0                   # slower crawl (polite)
    python sfda_scraper.py --dataset safety-alert   # single dataset by URL slug
"""

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from io import BytesIO, StringIO
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

try:
    import openpyxl
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# ─── Constants ─────────────────────────────────────────────────────────────────
BASE_URL        = "https://www.sfda.gov.sa"
EXTRACTION_DATE = datetime.now().strftime("%Y-%m-%d")

DEFAULT_OUTPUT_DIR = "sfda_data"
DEFAULT_DELAY      = 1.2   # seconds between HTTP requests — be polite
REQUEST_TIMEOUT    = 45
MAX_RETRIES        = 3


# Drugs List modal: abbreviated data-* key → human-readable label.
# Keys absent from this map (tn, sn, st, pfn, price) are excluded because
# they duplicate columns already present in the main table.
_DRUG_LIST_KEY_MAP = {
    "rn":           "Register Number",
    "ra":           "Route of Administration",
    "s":            "Size",
    "sizeunit":     "Size Unit",
    "pt":           "Package Type",
    "ps":           "Package Size",
    "ls":           "Legal Status",
    "pc":           "Product Control",
    "dsu":          "Distribution Site",
    "shlf":         "Shelf Life",
    "atc1":         "ATC Code1",
    "atc2":         "ATC Code2",
    "desc":         "Description Code",
    "auth":         "Authorization Status",
    "comp":         "Marketing Company",
    "compc":        "Country of Marketing Company",
    "agents":       "Agents",
    "manufacturers":"Manufacturers",
    "storage":      "Storage Conditions",
}

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

# Each section is either:
#   {"listing": "/en/lists-categories?..."} – auto-discover all cards on that page
#   {"fixed": [{"title": ..., "url": ...}]}  – use this exact list, no discovery
SOURCE_SECTIONS = {
    "drugs": {
        "listing": "/en/lists-categories?keys=&tags=2",
    },
    "clinical_trials": {
        "fixed": [
            {"title": "Clinical Trials List", "url": "/en/clinical-trials-list"},
        ],
    },
}

# Datasets that require JavaScript rendering and cannot be scraped with plain HTTP.
# Hospitals Side Effects is excluded — genuinely empty ("No results found").
JS_DATASETS = [
    {
        "title":   "Drug Companies List",
        "url":     "/en/drug-companies",
    },
    {
        "title":   "Incentive Project List",
        "url":     "/en/unregistered-pharmaceuticals-list",
    },
    {
        "title":   "Released Vaccines",
        "url":     "/en/released-vaccines",
    },
    {
        "title":   "Drugs Under Studying List",
        "url":     "/en/underStudyingList",
    },
    {
        "title":   "List of Registered Human/Herbal/Veterinary Drugs",
        "url":     "/en/drugs-list",
    },
]

# URL prefixes that are navigation pages, not datasets
_NAV_PREFIXES = (
    "/en/lists-categories",
    "/en/regulations",
    "/en/guidelines",
    "/en/circulars",
    "/en/faq",
    "/en/home",
    "/en/about",
    "/en/contact",
    "/en/news",
    "/en/media",
    "/en/open-data",
    "/en/rss",
    "/en/search",
    "/en/sitemap",
    "/en/eservices",
    "/en/overview",
    "/en/authority",
    "/en/career",
    "/en/annual-report",
    "/en/vision",
    "/en/sfda-",
    "/en/sector-list",
    "/en/scientific-reports",
    "/en/HajjAndOmrah",
    "/en/board",
    "/en/drugs-circulars",
    "/en/medical-devices-studies",
)

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("sfda")


# ─── Helpers ───────────────────────────────────────────────────────────────────

def safe_filename(title: str) -> str:
    name = re.sub(r"[^\w\s-]", "", title).strip()
    name = re.sub(r"[-\s]+", "_", name)
    return f"{name[:80]}.csv"


def is_dataset_url(href: str) -> bool:
    """True when href looks like a dataset page rather than nav/info page."""
    if not href or not href.startswith("/en/"):
        return False
    return not any(href.startswith(p) for p in _NAV_PREFIXES)


def append_page(url: str, page: int) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}page={page}"


# ─── Main scraper ──────────────────────────────────────────────────────────────

class SFDAScraper:
    def __init__(self, output_dir: str = DEFAULT_OUTPUT_DIR,
                 delay: float = DEFAULT_DELAY):
        self.output_dir = output_dir
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update(HTTP_HEADERS)
        os.makedirs(output_dir, exist_ok=True)

    # ── HTTP ─────────────────────────────────────────────────────────────────

    def fetch(self, url: str) -> Optional[requests.Response]:
        """GET with retry and rate limiting."""
        time.sleep(self.delay)
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                return resp
            except requests.RequestException as exc:
                log.warning("  attempt %d/%d – %s", attempt, MAX_RETRIES, exc)
                if attempt < MAX_RETRIES:
                    time.sleep(2 ** attempt)
        log.error("  gave up: %s", url)
        return None

    # ── Category discovery ───────────────────────────────────────────────────

    def discover_categories(self, source_path: str) -> List[Dict[str, str]]:
        """
        Walk the SFDA listing page at source_path and return every dataset card
        as {"title": ..., "url": ...}.

        Cards are <article class="warning-item"> elements. The title is in
        <span class="m-c-title"> and the URL is in <a class="download-doc-link">.
        Links may be relative (/en/...) or absolute (https://www.sfda.gov.sa/en/...).
        """
        full_url = urljoin(BASE_URL, source_path)
        categories: List[Dict[str, str]] = []
        seen: set = set()
        page = 0

        while True:
            url  = append_page(full_url, page) if page > 0 else full_url
            resp = self.fetch(url)
            if not resp:
                break

            soup = BeautifulSoup(resp.text, "lxml")
            found = 0

            for article in soup.select("article.warning-item"):
                # Title lives in <span class="m-c-title">
                title_span = article.select_one("span.m-c-title")
                title = title_span.get_text(" ", strip=True) if title_span else None

                # URL lives in <a class="download-doc-link">
                link_a = article.select_one("a.download-doc-link[href]")
                if not link_a:
                    continue
                href = link_a["href"]

                # Normalise absolute → relative path
                if href.startswith(BASE_URL):
                    href = href[len(BASE_URL):]

                # Skip navigation / non-dataset pages
                if not is_dataset_url(href) or href in seen:
                    continue

                if not title:
                    title = href.rstrip("/").split("/")[-1].replace("-", " ").title()

                # Strip "Open configuration options" Drupal prefix if present
                title = re.sub(r"^Open configuration options\s+", "", title).strip()

                categories.append({"title": title, "url": href})
                seen.add(href)
                found += 1

            log.info("  page %d → %d categories (total: %d)",
                     page + 1, found, len(categories))

            # Follow next page in the pager
            next_a = soup.select_one(
                "a[rel='next'], .pager__item--next a, li.pager-next a"
            )
            if next_a and found > 0:
                page += 1
            else:
                break

        return categories

    # ── Table parsing ─────────────────────────────────────────────────────────

    @staticmethod
    def _cell_text(td: Tag, tr: Tag) -> str:
        """
        Extract meaningful text from a <td>.

        If the cell holds a Drupal modal trigger (data-toggle='modal'),
        pull the actual content from the matching modal in the same row.
        Otherwise return plain text; if there is a hyperlink, append it.
        """
        modal_link = td.select_one("a[data-toggle='modal'][data-target]")
        if modal_link:
            target_class = modal_link.get("data-target", "").lstrip(".")
            if target_class:
                # The modal lives inside a td.views-field-nothing in the same row
                modal_node = tr.select_one(
                    f"td.views-field-nothing .{target_class}, "
                    f"td .{target_class}"
                )
                if modal_node:
                    body = modal_node.select_one(".modal-body")
                    content = (body or modal_node)

                    # Parse 2-column key/value modal tables as JSON dict
                    _modal_tbl = content.select_one("table")
                    if _modal_tbl:
                        kv = {}
                        for _tr in _modal_tbl.select("tr"):
                            _tds = _tr.select("td")
                            if len(_tds) < 2:
                                continue
                            _k = _tds[0].get_text(" ", strip=True)
                            if not _k:
                                continue
                            _links = [
                                urljoin(BASE_URL, a["href"])
                                for a in _tds[1].select("a[href]")
                                if a.get("href") and not a["href"].startswith(
                                    ("#", "javascript:"))
                            ]
                            _v = (" | ".join(_links)
                                  if _links
                                  else _tds[1].get_text(" ", strip=True))
                            kv[_k] = _v
                        if kv:
                            # Reference Warehouses: exclude Warehouse Name
                            if "Warehouse Distribution Area" in kv:
                                return json.dumps(
                                    {k: v for k, v in kv.items()
                                     if k != "Warehouse Name" and v},
                                    ensure_ascii=False,
                                )
                            # International Standards
                            if "Organization Number" in kv:
                                return json.dumps(kv, ensure_ascii=False)

                    return content.get_text(" ", strip=True)

                # No HTML modal — JS pages embed data inside data-* attributes.
                attrs = modal_link.attrs

                # ── Under Studying List ──────────────────────────────────────
                # Groups activesubstanceN / quantityN / unitN triplets into a
                # JSON list: [{"Active Substance":…,"Quantity":…,"Unit":…},…]
                if "data-activesubstance0" in attrs:
                    substances = []
                    i = 0
                    while f"data-activesubstance{i}" in attrs:
                        substances.append({
                            "Active Substance": attrs.get(
                                f"data-activesubstance{i}", ""),
                            "Quantity":         attrs.get(
                                f"data-quantity{i}", ""),
                            "Unit":             attrs.get(
                                f"data-unit{i}", ""),
                        })
                        i += 1
                    return json.dumps(substances, ensure_ascii=False)

                # ── List of Registered Drugs ────────────────────────────────
                # Converts abbreviated data-* keys to human-readable labels
                # and returns a JSON dict; redundant columns are excluded.
                if "data-rn" in attrs:
                    result = {}
                    for short_k, label in _DRUG_LIST_KEY_MAP.items():
                        v = attrs.get(f"data-{short_k}", "")
                        if not v:
                            continue
                        sv = str(v)
                        if "<" in sv:
                            sv = BeautifulSoup(sv, "lxml").get_text(
                                " ", strip=True)
                        result[label] = sv.strip()
                    return json.dumps(result, ensure_ascii=False)

                # ── Generic fallback ────────────────────────────────────────
                _skip = {"href", "id", "class", "data-toggle", "data-target",
                         "data-number"}
                pairs = []
                for k, v in attrs.items():
                    if k.startswith("data-") and k not in _skip and v:
                        sv = str(v)
                        cleaned = (
                            BeautifulSoup(sv, "lxml").get_text(" ", strip=True)
                            if "<" in sv else sv.strip()
                        )
                        pairs.append(f"{k[5:]}: {cleaned}")
                if pairs:
                    return " | ".join(pairs)

        # Regular: text + any real hyperlinks (skip # / javascript:)
        links = [
            urljoin(BASE_URL, a["href"])
            for a in td.select("a[href]")
            if a["href"] and not a["href"].startswith(("#", "javascript:"))
        ]
        text = td.get_text(" ", strip=True)
        if links:
            return f"{text} [LINK: {' | '.join(links)}]" if text else " | ".join(links)
        return text

    def parse_table(self, soup: BeautifulSoup) -> Tuple[List[str], List[List[str]]]:
        """
        Extract (headers, rows) from the first Drupal Views table on the page.

        Uses recursive=False throughout so that modals/nested tables embedded
        inside cells are never mistaken for data rows or columns.
        Skips 'views-field-nothing' columns (they hold modal popup containers).
        """
        table = soup.select_one(
            ".view-content table, main table, .views-table, table"
        )
        if not table:
            return [], []

        # ── Headers ──
        headers: List[str] = []
        thead_tr = None

        # Find the <thead> that is a direct child of this table (not a nested one)
        thead = None
        for child in table.children:
            if getattr(child, "name", None) == "thead":
                thead = child
                break

        if thead:
            thead_tr = thead.find("tr")

        if thead_tr:
            # find_all recursive=False: only direct <th> children of the <tr>
            for th in thead_tr.find_all("th", recursive=False):
                if "views-field-nothing" in th.get("class", []):
                    continue
                headers.append(th.get_text(" ", strip=True))
        else:
            # No <thead>: use first row's <th> elements as headers
            first_tr = table.find("tr")
            if first_tr:
                ths = first_tr.find_all("th", recursive=False)
                if ths:
                    headers = [t.get_text(" ", strip=True) for t in ths]

        # ── Rows ──
        rows: List[List[str]] = []

        # Find <tbody> that is a direct child of this table
        tbody = None
        for child in table.children:
            if getattr(child, "name", None) == "tbody":
                tbody = child
                break
        if tbody is None:
            tbody = table

        # find_all recursive=False: only direct <tr> children of <tbody>
        for tr in tbody.find_all("tr", recursive=False):
            # find_all recursive=False: only direct <td> children of <tr>
            tds = tr.find_all("td", recursive=False)
            if not tds:
                continue

            row_data: List[str] = [
                self._cell_text(td, tr)
                for td in tds
                if "views-field-nothing" not in td.get("class", [])
            ]

            if any(c.strip() for c in row_data):
                rows.append(row_data)

        return headers, rows

    # ── Pagination ────────────────────────────────────────────────────────────

    @staticmethod
    def last_page_number(soup: BeautifulSoup) -> Optional[int]:
        """
        Return the 0-indexed last page number from the Drupal pager, or None
        if the last-page link is not present.
        """
        last_a = soup.select_one(
            "a[rel='last'], .pager__item--last a, li.pager-last a"
        )
        if last_a:
            m = re.search(r"page=(\d+)", last_a.get("href", ""))
            if m:
                return int(m.group(1))

        # Fallback: highest page= value in all pager links
        nums = []
        for a in soup.select(".pager a, .pager__items a"):
            m = re.search(r"page=(\d+)", a.get("href", ""))
            if m:
                nums.append(int(m.group(1)))
        return max(nums) if nums else None

    @staticmethod
    def has_next_page(soup: BeautifulSoup) -> bool:
        return bool(soup.select_one("a[rel='next'], .pager__item--next a"))

    # ── Download-link fallback ────────────────────────────────────────────────

    def try_download(self, soup: BeautifulSoup,
                     source_url: str) -> Tuple[List[str], List[List[str]]]:
        """
        Scan the page for a download link (Excel/CSV/PHP) and fetch it.
        Returns (headers, rows) or ([], []).
        """
        download_url: Optional[str] = None
        for a in soup.select("a[href]"):
            text = a.get_text(strip=True).lower()
            href = a.get("href", "")
            if (any(kw in text for kw in ("download", "click here", "excel"))
                    or href.lower().endswith((".xlsx", ".xls", ".csv", ".php"))):
                download_url = urljoin(BASE_URL, href)
                break

        if not download_url:
            return [], []

        log.info("  → download fallback: %s", download_url)
        resp = self.fetch(download_url)
        if not resp or not resp.content:
            return [], []

        ct = resp.headers.get("Content-Type", "").lower()

        if "spreadsheet" in ct or "excel" in ct or download_url.endswith((".xlsx", ".xls")):
            return self._parse_excel(resp.content)

        if "csv" in ct or "text/plain" in ct:
            reader = csv.reader(StringIO(resp.text))
            all_rows = list(reader)
            return (all_rows[0], all_rows[1:]) if all_rows else ([], [])

        # Try as HTML
        inner = BeautifulSoup(resp.text, "lxml")
        h, r  = self.parse_table(inner)
        if r:
            return h, r

        # Last resort: Excel bytes
        return self._parse_excel(resp.content)

    def _parse_excel(self, content: bytes) -> Tuple[List[str], List[List[str]]]:
        if not HAS_OPENPYXL:
            log.warning("  openpyxl not installed — cannot parse Excel")
            return [], []
        try:
            wb = openpyxl.load_workbook(BytesIO(content), data_only=True)
            ws = wb.active
            all_rows = [
                [str(c) if c is not None else "" for c in row]
                for row in ws.iter_rows(values_only=True)
            ]
            return (all_rows[0], all_rows[1:]) if all_rows else ([], [])
        except Exception as exc:
            log.warning("  Excel parse error: %s", exc)
            return [], []

    # ── RMM detail-page enrichment ────────────────────────────────────────────

    @staticmethod
    def _parse_rmm_detail(
        soup: BeautifulSoup,
    ) -> Tuple[str, str]:
        """
        Extract risk description and materials from one RMM detail page.

        Page structure (single <table>):
          Scientific Name  | <name>          ← skip, already in main table
          Brand Name       | <name>          ← skip
          Risk             | <text>          ← risk string
          <Material Name>  | <file links>    ← one or more material rows

        Returns (risk_str, materials_json_str).
        """
        risk = ""
        materials: List[Dict] = []

        table = soup.select_one("table")
        if not table:
            return risk, json.dumps(materials, ensure_ascii=False)

        _skip_th = {"scientific name", "brand name"}

        for row in table.select("tr"):
            th = row.select_one("th")
            td = row.select_one("td")
            if not th or not td:
                continue

            th_text = th.get_text(" ", strip=True)

            if th_text.lower() in _skip_th:
                continue

            if th_text.lower() == "risk":
                risk = td.get_text(" ", strip=True)
                continue

            # Material row — collect attached file links
            files = [
                urljoin(BASE_URL, a["href"])
                for a in td.select("a[href]")
                if a.get("href")
            ]
            if files or td.get_text(strip=True):
                materials.append({"name": th_text, "files": files})

        return risk, json.dumps(materials, ensure_ascii=False)

    def _enrich_rmm_rows(
        self,
        headers: List[str],
        rows: List[List[str]],
    ) -> Tuple[List[str], List[List[str]]]:
        """
        Replace the RMM 'Details' column (a link) with two new columns:
          risk      – string description from the detail page
          materials – JSON list of {"name": str, "files": [str, …]}
        Fetches each detail page individually (one request per row).
        """
        details_idx = next(
            (i for i, h in enumerate(headers) if h.lower() == "details"),
            None,
        )
        if details_idx is None:
            return headers, rows

        new_headers = (
            headers[:details_idx]
            + ["risk", "materials"]
            + headers[details_idx + 1:]
        )

        new_rows: List[List[str]] = []
        total = len(rows)

        for idx, row in enumerate(rows):
            prefix = row[:details_idx]
            suffix = row[details_idx + 1:]
            detail_cell = row[details_idx] if details_idx < len(row) else ""

            link_m = re.search(r"\[LINK:\s*(https?://[^\]]+)\]", detail_cell)
            if not link_m:
                new_rows.append(prefix + ["", json.dumps([])] + suffix)
                continue

            detail_url = link_m.group(1).strip()
            log.info("  RMM %d/%d: %s", idx + 1, total, detail_url)

            resp = self.fetch(detail_url)
            if not resp:
                new_rows.append(prefix + ["", json.dumps([])] + suffix)
                continue

            detail_soup = BeautifulSoup(resp.text, "lxml")
            risk, materials_json = self._parse_rmm_detail(detail_soup)
            new_rows.append(prefix + [risk, materials_json] + suffix)

        return new_headers, new_rows

    # ── CSV output ─────────────────────────────────────────────────────────

    def write_csv(self, title: str, headers: List[str],
                  rows: List[List[str]], source_url: str) -> str:
        """
        Write one CSV with the scraped data. Overwrites the file on each run.
        """
        path = os.path.join(self.output_dir, safe_filename(title))

        full_headers = (headers if headers
                        else [f"col_{i}" for i in range(
                            max(len(r) for r in rows) if rows else 0
                        )])

        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(full_headers)
            for row in rows:
                w.writerow(row)

        log.info("  ✓ %d rows → %s", len(rows), path)
        return path

    # ── Dataset scraper ───────────────────────────────────────────────────────

    def scrape_dataset(self, title: str, url: str) -> bool:
        """
        Scrape one dataset through all pages. Returns True on success.

        Strategy:
        1. Fetch page 0 and parse the table.
        2. If "Displaying 0 - 0 of 0" or no rows → try download-link fallback.
        3. Otherwise detect total pages via last-page pager link; if absent,
           iterate page-by-page until no 'next' link remains.
        """
        full_url = urljoin(BASE_URL, url)
        log.info("─" * 58)
        log.info("Dataset : %s", title)
        log.info("URL     : %s", full_url)

        resp = self.fetch(full_url)
        if not resp:
            log.error("  No response — skipped")
            return False

        soup    = BeautifulSoup(resp.text, "lxml")
        pg_text = soup.get_text(" ")

        # Detect empty JavaScript-rendered pages
        is_empty = bool(re.search(
            r"[Dd]isplaying\s+0\s*[-–]\s*0\s+of\s+0", pg_text
        ))

        if is_empty:
            log.warning("  Page reports 0 records — trying download fallback")
            h, r = self.try_download(soup, full_url)
            if r:
                self.write_csv(title, h, r, full_url)
                return True
            log.warning("  No downloadable data — writing empty CSV.")
            headers, _ = self.parse_table(soup)
            self.write_csv(title, headers, [], full_url)
            return True

        headers, rows = self.parse_table(soup)
        if not rows:
            log.warning("  No table on page 1 — trying download fallback")
            h, r = self.try_download(soup, full_url)
            if r:
                self.write_csv(title, h, r, full_url)
                return True
            log.warning("  No data found — writing empty CSV.")
            self.write_csv(title, headers, [], full_url)
            return True

        log.info("  Schema  : %s", headers)
        all_rows = list(rows)

        # ── Pagination ──────────────────────────────────────────────────────
        last_pg = self.last_page_number(soup)

        if last_pg is not None:
            # We know exactly how many pages there are
            log.info("  Pages   : %d", last_pg + 1)
            for page in range(1, last_pg + 1):
                r = self._fetch_page_rows(full_url, page)
                if r is None:
                    break
                all_rows.extend(r)
                log.info("  page %d/%d → %d rows (total: %d)",
                         page + 1, last_pg + 1, len(r), len(all_rows))
        else:
            # Walk pages until no "next" link
            page = 1
            while self.has_next_page(soup):
                r = self._fetch_page_rows_with_soup(full_url, page)
                if r is None:
                    break
                rows_got, soup = r
                if not rows_got:
                    break
                all_rows.extend(rows_got)
                log.info("  page %d → %d rows (total: %d)",
                         page + 1, len(rows_got), len(all_rows))
                page += 1

        if not all_rows:
            log.warning("  No rows collected — writing empty CSV.")
            self.write_csv(title, headers, [], full_url)
            return True

        # Enrich RMM rows: fetch each detail page to replace link with risk+materials
        if "/RMM" in full_url or "/rmm" in full_url.lower():
            log.info("  Enriching RMM rows with detail pages …")
            headers, all_rows = self._enrich_rmm_rows(headers, all_rows)

        self.write_csv(title, headers, all_rows, full_url)
        return True

    def _fetch_page_rows(self, base_url: str,
                         page: int) -> Optional[List[List[str]]]:
        """Fetch one pagination page and return its rows (or None on failure)."""
        resp = self.fetch(append_page(base_url, page))
        if not resp:
            return None
        _, rows = self.parse_table(BeautifulSoup(resp.text, "lxml"))
        return rows if rows else None

    def _fetch_page_rows_with_soup(
            self, base_url: str, page: int
    ) -> Optional[Tuple[List[List[str]], BeautifulSoup]]:
        """Like _fetch_page_rows but also returns the parsed soup (for pager check)."""
        resp = self.fetch(append_page(base_url, page))
        if not resp:
            return None
        soup = BeautifulSoup(resp.text, "lxml")
        _, rows = self.parse_table(soup)
        return rows, soup

    # ── Playwright (JS-rendered pages) ───────────────────────────────────────

    @staticmethod
    def _next_playwright_url(soup: BeautifulSoup, current_url: str) -> Optional[str]:
        """
        Derive the next-page URL from the rendered pager.

        Priority:
          1. <a rel='next' href=...>
          2. .pager__item--next a[href]
          3. Numbered pager using ?pg= or ?page= param — increment current page
        """
        def _active_pg(soup: BeautifulSoup) -> Optional[int]:
            """Return current page number from the pager's active item."""
            for sel in ("#pgnumber .is-active", ".pager__items .is-active",
                        ".pager__item.is-active"):
                el = soup.select_one(sel)
                if el:
                    try:
                        return int(el.get_text(strip=True))
                    except (ValueError, TypeError):
                        pass
            return None

        def _pg_template(soup: BeautifulSoup) -> Optional[Tuple[str, str]]:
            """Return (base_path_without_pg, param_name) from any pager link."""
            for a in soup.select(".pager a[href]"):
                m = re.search(r"([?&]?)(pg|page)=(\d+)", a["href"])
                if m:
                    param = m.group(2)
                    base = re.sub(rf"[?&]?{param}=\d+", "", a["href"]).rstrip("&?")
                    return base, param
            return None

        # 1. Standard rel=next — but verify it actually advances the page.
        #    SFDA bug: on page 1 (no ?pg= in URL), rel=next wrongly points to ?pg=1.
        a = soup.select_one("a[rel='next'][href]")
        if a and a.get("href"):
            next_url = urljoin(BASE_URL, a["href"])
            m_next = re.search(r"[?&](pg|page)=(\d+)", next_url)
            if m_next:
                next_pg = int(m_next.group(2))
                m_cur = re.search(rf"[?&]{m_next.group(1)}=(\d+)", current_url)
                cur_pg = int(m_cur.group(1)) if m_cur else (_active_pg(soup) or 1)
                if next_pg <= cur_pg:
                    # rel=next is stale; advance to cur_pg + 1
                    param = m_next.group(1)
                    next_url = re.sub(rf"{param}=\d+", f"{param}={cur_pg + 1}", next_url)
            return next_url

        # 2. Explicit .pager__item--next with a valid href
        a = soup.select_one(".pager__item--next a[href]")
        if a and a.get("href"):
            return urljoin(BASE_URL, a["href"])

        # 3. Custom #pgnumber pager (SFDA): use active item + any link as template
        tpl = _pg_template(soup)
        cur_pg = _active_pg(soup)
        if tpl and cur_pg is not None:
            base, param = tpl
            sep = "&" if "?" in base else "?"
            return urljoin(BASE_URL, f"{base}{sep}{param}={cur_pg + 1}")

        # 4. Numbered pager with explicit last-page link
        last_a = soup.select_one(".pager__item--last a[href]")
        if last_a:
            last_href = last_a["href"]
            m = re.search(r"[?&](pg|page)=(\d+)", last_href)
            if m:
                param, last_pg = m.group(1), int(m.group(2))
                cur = re.search(rf"[?&]{param}=(\d+)", current_url)
                current_pg = int(cur.group(1)) if cur else 0
                if current_pg < last_pg:
                    base = re.sub(rf"([?&]){param}=\d+", "", last_href).rstrip("&?")
                    sep = "&" if "?" in base else "?"
                    return urljoin(BASE_URL, f"{base}{sep}{param}={current_pg + 1}")

        return None

    def scrape_playwright_datasets(
        self, target_slug: Optional[str] = None
    ) -> Tuple[int, int]:
        """
        Scrape JavaScript-rendered datasets using Playwright headless Chromium.

        Uses URL-based pagination (?page=N) rather than clicking — more reliable
        and avoids re-rendering the full page layout on each click.

        Returns (attempted, saved).
        """
        if not HAS_PLAYWRIGHT:
            log.error(
                "Playwright not installed. "
                "Run: pip install playwright && playwright install chromium"
            )
            return 0, 0

        # Filter by target_slug if provided
        datasets = JS_DATASETS
        if target_slug:
            datasets = [
                d for d in datasets
                if target_slug.lower() in (
                    d["url"].rstrip("/").split("/")[-1].lower(),
                    d["title"].lower(),
                )
            ]

        if not datasets:
            return 0, 0

        attempted = len(datasets)
        saved = 0

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()

            for ds in datasets:
                full_url = urljoin(BASE_URL, ds["url"])
                log.info("─" * 58)
                log.info("Dataset (JS) : %s", ds["title"])
                log.info("URL          : %s", full_url)

                all_headers: List[str] = []
                all_rows: List[List[str]] = []
                pg_num = 0

                pg_url = full_url
                prev_page_hash: Optional[str] = None

                while True:
                    # Navigate and wait for the data table to appear
                    try:
                        page.goto(
                            pg_url,
                            wait_until="domcontentloaded",
                            timeout=30_000,
                        )
                        page.wait_for_selector(
                            "table tbody tr",
                            timeout=25_000,
                        )
                    except PWTimeoutError:
                        log.info(
                            "  page %d: no table appeared — stopping", pg_num + 1
                        )
                        break
                    except Exception as exc:
                        log.warning("  page %d error: %s", pg_num + 1, exc)
                        break

                    time.sleep(0.8)  # let any trailing JS settle

                    soup = BeautifulSoup(page.content(), "lxml")
                    headers, rows = self.parse_table(soup)

                    if not rows:
                        log.info("  page %d: 0 rows — stopping", pg_num + 1)
                        break

                    # Detect pagination loop (same page served regardless of URL)
                    page_hash = hashlib.md5(
                        "|".join("".join(r) for r in rows).encode()
                    ).hexdigest()
                    if page_hash == prev_page_hash:
                        log.info(
                            "  page %d: same content as previous — loop detected, stopping",
                            pg_num + 1,
                        )
                        break
                    prev_page_hash = page_hash

                    if not all_headers:
                        all_headers = headers
                        log.info("  Schema : %s", headers)

                    all_rows.extend(rows)
                    log.info(
                        "  page %d → %d rows  (cumulative: %d)",
                        pg_num + 1, len(rows), len(all_rows),
                    )

                    # Resolve next-page URL from the rendered pager
                    next_url = self._next_playwright_url(soup, pg_url)
                    if not next_url:
                        break

                    pg_url = next_url
                    pg_num += 1
                    time.sleep(self.delay)

                if all_rows:
                    self.write_csv(ds["title"], all_headers, all_rows, full_url)
                else:
                    log.warning("  No rows collected — writing empty CSV.")
                    self.write_csv(ds["title"], all_headers, [], full_url)
                saved += 1

            browser.close()

        return attempted, saved

    # ── Main ─────────────────────────────────────────────────────────────────

    def run(
        self,
        target_slug: Optional[str] = None,
        use_playwright: bool = False,
    ) -> None:
        log.info("=" * 58)
        log.info("SFDA Data Extractor  —  %s", EXTRACTION_DATE)
        log.info("Output: %s", os.path.abspath(self.output_dir))
        if not HAS_OPENPYXL:
            log.warning("openpyxl not found — Excel downloads disabled")
        if use_playwright and not HAS_PLAYWRIGHT:
            log.warning(
                "playwright not installed — JS datasets will be skipped. "
                "Run: pip install playwright && playwright install chromium"
            )
        log.info("=" * 58)

        total_found = total_saved = 0

        for section_name, section_cfg in SOURCE_SECTIONS.items():
            log.info("\n▶  Section: %s", section_name.upper().replace("_", " "))

            if "fixed" in section_cfg:
                categories = section_cfg["fixed"]
                log.info("   Fixed datasets: %d\n", len(categories))
            else:
                listing = section_cfg["listing"]
                log.info("   Listing: %s%s", BASE_URL, listing)
                categories = self.discover_categories(listing)
                log.info("   → %d datasets discovered\n", len(categories))

            js_paths = {ds["url"].rstrip("/") for ds in JS_DATASETS}
            for cat in categories:
                slug = cat["url"].rstrip("/").split("/")[-1]
                if target_slug and target_slug.lower() not in (
                    slug.lower(), cat["title"].lower()
                ):
                    continue
                # Skip URLs handled by the Playwright pass to avoid empty duplicates
                if cat["url"].rstrip("/") in js_paths and use_playwright and HAS_PLAYWRIGHT:
                    log.info("  Skipping %s — handled by Playwright pass", cat["title"])
                    continue
                total_found += 1
                if self.scrape_dataset(cat["title"], cat["url"]):
                    total_saved += 1

        # ── Playwright pass (JS-rendered datasets) ──────────────────────────
        if use_playwright and HAS_PLAYWRIGHT:
            log.info("\n▶  Section: JS-RENDERED DATASETS (Playwright)")
            attempted, saved = self.scrape_playwright_datasets(target_slug)
            total_found += attempted
            total_saved += saved

        log.info("\n" + "=" * 58)
        log.info("Done — %d/%d datasets saved to: %s",
                 total_saved, total_found, os.path.abspath(self.output_dir))
        log.info("=" * 58)


# ─── CLI ───────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Extract drug and clinical-trial datasets from sfda.gov.sa",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "-o", "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help="Directory where CSV files are written",
    )
    p.add_argument(
        "-d", "--delay",
        type=float, default=DEFAULT_DELAY,
        help="Pause between HTTP requests (seconds)",
    )
    p.add_argument(
        "--dataset",
        default=None, metavar="SLUG",
        help=(
            "Scrape only the dataset whose URL slug matches SLUG "
            "(e.g. 'new-sfda-drug-approvals', 'clinical-trials-list', "
            "'released-vaccines', 'drugs-list')"
        ),
    )
    p.add_argument(
        "--playwright",
        action="store_true",
        default=False,
        help=(
            "Also scrape the 3 JavaScript-rendered datasets that standard HTTP "
            "cannot reach (Released Vaccines, Drugs Under Studying List, "
            "List of Registered Drugs). Requires: pip install playwright && "
            "playwright install chromium"
        ),
    )
    return p


def main() -> None:
    args = _build_parser().parse_args()
    SFDAScraper(output_dir=args.output_dir, delay=args.delay).run(
        target_slug=args.dataset,
        use_playwright=args.playwright,
    )


if __name__ == "__main__":
    main()
