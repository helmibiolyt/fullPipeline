#!/usr/bin/env python3
"""
adrreports.eu (EudraVigilance) Scraper — Safety & Pharmacovigilance

Scrapes the public A-Z index of centrally authorised medicinal products and
active substances exposed by the EudraVigilance European database of suspected
adverse drug reaction reports (https://www.adrreports.eu/).

The A-Z browse page (search_subst.html) renders its tables client-side via
Scripts/dashboard-api.js, which XHR-loads static HTML fragments from:
    https://www.adrreports.eu/tables/substance/{letter}.html
    https://www.adrreports.eu/tables/product/{letter}.html
where {letter} is a-z or "0-9". Each row is an <a> whose text is the
substance/product name and whose href points at the EudraVigilance line-listing
report (dap.ema.europa.eu). The href embeds the EudraVigilance "High Level Code"
as the P3 parameter (P3=1+<code>).

These fragments are plain static HTML, so no browser/JS execution is required.

NOTE: The actual adverse-reaction line-listing figures live behind an
interactive EudraVigilance / Power BI style dashboard on dap.ema.europa.eu and
are intentionally OUT OF SCOPE for this scraper. We capture only the public
index of substance/product names and their report URLs.

No authentication and no secrets required.
"""

import csv
import logging
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "EudraVigilance"

BASE_URL = "https://www.adrreports.eu"
TABLE_ROOT = f"{BASE_URL}/tables"
# a-z plus the "0-9" bucket, exactly as the site's A-Z navigation exposes.
LETTERS = [chr(c) for c in range(ord("a"), ord("z") + 1)] + ["0-9"]

REQUEST_DELAY = 0.5
MAX_RETRIES = 3
TIMEOUT = 60

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

log_fmt = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=log_fmt,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(BASE_DIR / "scraper.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("adrreports")

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})


def fetch(url: str) -> str | None:
    """GET a URL with retries. Returns text or None on persistent failure."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=TIMEOUT)
            if resp.status_code == 404:
                log.warning("404 Not Found: %s", url)
                return None
            resp.raise_for_status()
            return resp.text
        except Exception as exc:  # noqa: BLE001 - be robust per request
            log.warning(
                "Request %s attempt %d/%d failed: %s",
                url, attempt, MAX_RETRIES, exc,
            )
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
    log.error("Failed after %d attempts: %s", MAX_RETRIES, url)
    return None


def extract_code(report_url: str) -> str:
    """Extract the EudraVigilance High Level Code from the report URL.

    The relevant query param is P3, e.g. 'P3=1+18853' -> '18853'.
    """
    try:
        qs = parse_qs(urlparse(report_url).query)
        p3 = qs.get("P3", [""])[0]
        # Value looks like '1+18853' (URL-decoded from '1%2B18853' / '1+18853').
        m = re.search(r"(\d+)\s*$", p3)
        return m.group(1) if m else ""
    except Exception:  # noqa: BLE001
        return ""


def parse_table(html: str, letter: str) -> list[dict]:
    """Parse one A-Z fragment into a list of {name, code, letter, report_url}."""
    rows: list[dict] = []
    soup = BeautifulSoup(html, "lxml")
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        name = anchor.get_text(strip=True)
        if not href or not name:
            continue
        # Only keep the EudraVigilance report links (skip any nav/other links).
        if "dap.ema.europa.eu" not in href:
            continue
        rows.append(
            {
                "name": name,
                "code": extract_code(href),
                "letter": letter,
                "report_url": href,
            }
        )
    return rows


def scrape_category(category: str) -> list[dict]:
    """Scrape all A-Z fragments for a category ('substance' or 'product')."""
    all_rows: list[dict] = []
    for letter in LETTERS:
        url = f"{TABLE_ROOT}/{category}/{letter}.html"
        try:
            html = fetch(url)
            if not html:
                continue
            rows = parse_table(html, letter)
            log.info("[%s] letter '%s': %d rows", category, letter, len(rows))
            all_rows.extend(rows)
        except Exception as exc:  # noqa: BLE001 - never let one letter kill the run
            log.warning("[%s] letter '%s' failed: %s", category, letter, exc)
        finally:
            time.sleep(REQUEST_DELAY)
    return all_rows


def write_csv(path: Path, rows: list[dict], name_col: str, code_col: str) -> None:
    """Write rows to CSV, de-duplicated on (name, code), sorted by name."""
    seen = set()
    unique = []
    for r in rows:
        key = (r["name"], r["code"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)
    unique.sort(key=lambda r: (r["name"].lower(), r["code"]))

    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([name_col, code_col, "letter", "report_url"])
        for r in unique:
            writer.writerow([r["name"], r["code"], r["letter"], r["report_url"]])
    log.info("Wrote %d unique rows -> %s", len(unique), path)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    total = 0

    # Primary deliverable: active substances.
    substances = scrape_category("substance")
    if substances:
        write_csv(
            OUT_DIR / "adrreports_substances.csv",
            substances,
            name_col="active_substance",
            code_col="substance_code",
        )
        total += len(substances)

    # Same public index, product view — cheap extra using the same mechanism.
    products = scrape_category("product")
    if products:
        write_csv(
            OUT_DIR / "adrreports_products.csv",
            products,
            name_col="medicinal_product",
            code_col="product_code",
        )
        total += len(products)

    if total == 0:
        log.error("No data scraped from adrreports.eu — failing.")
        sys.exit(1)

    log.info("Done. Total rows scraped (pre-dedup): %d", total)


if __name__ == "__main__":
    main()
