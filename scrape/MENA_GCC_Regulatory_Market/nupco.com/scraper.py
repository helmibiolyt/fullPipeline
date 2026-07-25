#!/usr/bin/env python3
"""
NUPCO (National Unified Procurement Company) Scraper
Scrapes catalogues, tenders, and news from nupco.com
"""

import os
import re
import csv
import json
import time
import logging
from pathlib import Path
from collections import OrderedDict
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
import pandas as pd

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
CATALOGUE_URL = "https://www.nupco.com/suppliers/unified-catalogue/"
TENDERS_LIST_URL = "https://www.nupco.com/tenders/tenders-list/"
TENDERS_PLAN_URL = "https://www.nupco.com/tenders/tenders-plan/"
WP_API_POSTS = "https://www.nupco.com/wp-json/wp/v2/posts"
HOMEPAGE = "https://www.nupco.com/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
}

REQUEST_DELAY = 0.5
MAX_RETRIES = 3

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log_fmt = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=log_fmt,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(BASE_DIR / "scraper.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("nupco")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
session = requests.Session()
session.headers.update(HEADERS)


def init_session():
    """Visit homepage to pick up cookies."""
    try:
        r = session.get(HOMEPAGE, timeout=30)
        log.info("Homepage visited – status %s, cookies: %s", r.status_code, len(session.cookies))
    except Exception as e:
        log.warning("Could not visit homepage: %s", e)


def fetch(url, **kwargs):
    """GET with retry + exponential backoff."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            time.sleep(REQUEST_DELAY)
            r = session.get(url, timeout=60, **kwargs)
            r.raise_for_status()
            return r
        except Exception as e:
            wait = 2 ** attempt
            log.warning("Attempt %d/%d failed for %s: %s – retrying in %ds",
                        attempt, MAX_RETRIES, url, e, wait)
            if attempt == MAX_RETRIES:
                log.error("All retries exhausted for %s", url)
                raise
            time.sleep(wait)


def ensure_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    return path


def group_tables_by_columns(tables):
    """Group table row-lists by column count for merging."""
    groups = OrderedDict()
    for rows in tables:
        if not rows:
            continue
        key = len(rows[0])
        if key not in groups:
            groups[key] = []
        groups[key].extend(rows)
    return list(groups.values())


# ---------------------------------------------------------------------------
# A) Unified Catalogue
# ---------------------------------------------------------------------------
def scrape_catalogue():
    out_dir = ensure_dir(BASE_DIR / "Unified Catalogue")
    log.info("Scraping Unified Catalogue page: %s", CATALOGUE_URL)

    r = fetch(CATALOGUE_URL)
    soup = BeautifulSoup(r.text, "lxml")

    # Find all .xlsx links on the page
    xlsx_links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().endswith(".xlsx"):
            full = urljoin(CATALOGUE_URL, href)
            # Fix staging URLs → production
            full = full.replace("://stg.nupco.com/", "://www.nupco.com/")
            xlsx_links.add(full)

    if not xlsx_links:
        log.warning("No .xlsx links found on catalogue page")
        return

    log.info("Found %d Excel catalogue files", len(xlsx_links))

    for url in sorted(xlsx_links):
        fname = url.split("/")[-1]
        xlsx_path = out_dir / fname
        log.info("Downloading %s", fname)

        try:
            resp = fetch(url)
            xlsx_path.write_bytes(resp.content)
            log.info("Saved %s (%d bytes)", xlsx_path.name, len(resp.content))
        except Exception as e:
            log.error("Failed to download %s: %s", url, e)
            continue

        # Convert to CSV
        try:
            convert_xlsx_to_csv(xlsx_path, out_dir)
        except Exception as e:
            log.error("Failed to convert %s: %s", fname, e)

    log.info("Catalogue section complete")


def convert_xlsx_to_csv(xlsx_path, out_dir):
    """Read all sheets, group by column count, write CSVs."""
    xls = pd.ExcelFile(xlsx_path, engine="openpyxl")
    base = xlsx_path.stem

    tables = []
    for sheet in xls.sheet_names:
        df = pd.read_excel(xls, sheet_name=sheet, header=None)
        if df.empty:
            continue
        rows = df.values.tolist()
        tables.append(rows)

    if not tables:
        log.warning("No data in %s", xlsx_path.name)
        return

    groups = group_tables_by_columns(tables)

    if len(groups) == 1:
        csv_path = out_dir / f"{base}.csv"
        write_csv(csv_path, groups[0])
        log.info("Wrote %s (%d rows)", csv_path.name, len(groups[0]))
    else:
        for i, grp in enumerate(groups, 1):
            csv_path = out_dir / f"{base}_topic{i}.csv"
            write_csv(csv_path, grp)
            log.info("Wrote %s (%d rows)", csv_path.name, len(grp))


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# B) Tenders List
# ---------------------------------------------------------------------------
def scrape_tenders_list():
    out_dir = ensure_dir(BASE_DIR / "Tenders")
    log.info("Scraping Tenders List: %s", TENDERS_LIST_URL)

    r = fetch(TENDERS_LIST_URL)
    soup = BeautifulSoup(r.text, "lxml")

    tenders = []
    html = r.text

    # The page uses Elementor div-based layout with elementor-heading-title spans.
    # Each tender is inside a container whose class contains "tender-status-" or
    # a repeated Elementor section. We parse by finding all "Tender ID" labels
    # and then extracting sibling data.

    # Strategy: find all elementor-heading-title elements, walk through them
    # to reconstruct tender records by looking for "Tender ID" markers.
    headings = soup.find_all(class_="elementor-heading-title")
    heading_texts = [(h, h.get_text(strip=True)) for h in headings]

    i = 0
    while i < len(heading_texts):
        el, txt = heading_texts[i]
        if txt == "Tender ID" and i + 1 < len(heading_texts):
            tender = {
                "tender_id": "",
                "title": "",
                "opening_date": "",
                "submission_deadline": "",
                "status": "",
            }
            # Next heading should be the actual ID
            i += 1
            tender["tender_id"] = heading_texts[i][1] if i < len(heading_texts) else ""

            # Scan forward to collect fields until next "Tender ID" or end
            i += 1
            while i < len(heading_texts) and heading_texts[i][1] != "Tender ID":
                _, t = heading_texts[i]
                if t in ("Available", "Updated", "Cancelled", "Direct Purchase",
                         "Results", "Under Study", "Available New", "Closed"):
                    tender["status"] = t
                elif t.startswith("Opening Date"):
                    pass  # label, value follows or is combined
                elif t.startswith("Submission Deadline"):
                    pass
                elif t.startswith("Bid Opening"):
                    pass
                elif t == "Discover More":
                    pass
                elif re.match(r"^[A-Z][a-z]{2},\s*\d{2}/\d{2}/\d{4}", t):
                    # Date value
                    if not tender["opening_date"]:
                        tender["opening_date"] = t
                    elif not tender["submission_deadline"]:
                        tender["submission_deadline"] = t
                elif len(t) > 5 and t not in ("Opening Date:", "Submission Deadline:",
                                                "Bid Opening:", "Discover More",
                                                "Opening Date :", "Submission Deadline :",
                                                "Bid Opening :"):
                    # Likely the title (Arabic or English)
                    if not tender["title"]:
                        tender["title"] = t
                    elif not tender["title"].strip():
                        tender["title"] = t
                i += 1

            if tender["tender_id"]:
                tenders.append(tender)
        else:
            i += 1

    csv_path = out_dir / "tenders_list.csv"
    if tenders:
        df = pd.DataFrame(tenders)
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        log.info("Saved %d tenders to %s", len(tenders), csv_path.name)
    else:
        log.warning("No tenders found on tenders list page")
        # Save empty CSV with headers
        pd.DataFrame(columns=["tender_id", "title", "opening_date", "submission_deadline", "status"]).to_csv(
            csv_path, index=False, encoding="utf-8-sig"
        )



# ---------------------------------------------------------------------------
# C) Tenders Plan
# ---------------------------------------------------------------------------
def scrape_tenders_plan():
    out_dir = ensure_dir(BASE_DIR / "Tenders")
    log.info("Scraping Tenders Plan: %s", TENDERS_PLAN_URL)

    r = fetch(TENDERS_PLAN_URL)
    soup = BeautifulSoup(r.text, "lxml")

    all_entries = []

    # wpDataTables renders as standard HTML tables with internal columns like
    # wdt_ID, wdt_created_by, etc. The meaningful columns are:
    # الشهر (Month), م (Number), title, opening date, final date
    # We need to find the right column indices by inspecting headers.

    tables = soup.find_all("table")

    # Find category names from headings or table captions
    # Look for h2/h3/h4 elements that precede each table
    all_headings = soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])

    categories = ["Medical & Lab Supplies", "Pharmaceuticals", "Medical Devices"]

    for idx, table in enumerate(tables):
        # Determine category
        category = categories[idx] if idx < len(categories) else f"Category_{idx+1}"

        rows = table.find_all("tr")
        if not rows:
            continue

        # Parse header to find relevant column indices
        header_row = rows[0]
        header_cells = header_row.find_all(["td", "th"])
        headers = [c.get_text(strip=True) for c in header_cells]

        # Find indices for meaningful columns (skip wdt_ internal columns)
        # We want: number (م), title, opening date, final date
        # These are typically the last 4-5 meaningful columns
        col_map = {}
        for ci, h in enumerate(headers):
            hl = h.lower()
            if h == "م" or hl == "number":
                col_map["number"] = ci
            elif "منافس" in h or "competition" in hl or "title" in hl:
                col_map["title"] = ci
            elif "فتح" in h or "opening" in hl:
                col_map["tender_opening_date"] = ci
            elif "نتائج" in h or "نهائي" in h or "final" in hl or "announcement" in hl:
                col_map["final_announcement_date"] = ci
            elif h == "الشهر" or "month" in hl:
                col_map["month"] = ci

        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) < 3:
                continue
            values = [c.get_text(strip=True) for c in cells]

            entry = {
                "category": category,
                "number": values[col_map["number"]] if "number" in col_map and col_map["number"] < len(values) else "",
                "title": values[col_map["title"]] if "title" in col_map and col_map["title"] < len(values) else "",
                "tender_opening_date": values[col_map["tender_opening_date"]] if "tender_opening_date" in col_map and col_map["tender_opening_date"] < len(values) else "",
                "final_announcement_date": values[col_map["final_announcement_date"]] if "final_announcement_date" in col_map and col_map["final_announcement_date"] < len(values) else "",
            }
            # Skip rows that look empty or are just metadata
            if entry["title"] or entry["number"]:
                all_entries.append(entry)

    csv_path = out_dir / "tenders_plan.csv"
    if all_entries:
        df = pd.DataFrame(all_entries)
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        log.info("Saved %d tender plan entries to %s", len(all_entries), csv_path.name)
    else:
        log.warning("No tender plan entries found")
        pd.DataFrame(columns=["category", "number", "title", "tender_opening_date", "final_announcement_date"]).to_csv(
            csv_path, index=False, encoding="utf-8-sig"
        )



# ---------------------------------------------------------------------------
# D) News / Posts via WP REST API
# ---------------------------------------------------------------------------
def scrape_news():
    out_dir = ensure_dir(BASE_DIR / "News")
    log.info("Scraping News via WP REST API")

    all_posts = []
    api_works = True
    page = 1

    while api_works:
        url = f"{WP_API_POSTS}?per_page=100&page={page}"
        log.info("Fetching posts page %d via WP REST API", page)

        try:
            r = fetch(url)
        except Exception as e:
            log.warning("WP REST API failed (page %d): %s – falling back to HTML scraping", page, e)
            api_works = False
            break

        try:
            posts = r.json()
        except Exception:
            log.warning("Invalid JSON from API, falling back to HTML")
            api_works = False
            break

        if not posts:
            break

        for p in posts:
            title_text = p.get("title", {}).get("rendered", "") if isinstance(p.get("title"), dict) else str(p.get("title", ""))
            excerpt_text = p.get("excerpt", {}).get("rendered", "") if isinstance(p.get("excerpt"), dict) else str(p.get("excerpt", ""))
            excerpt_clean = BeautifulSoup(excerpt_text, "lxml").get_text(strip=True) if excerpt_text else ""
            title_clean = BeautifulSoup(title_text, "lxml").get_text(strip=True) if title_text else ""

            all_posts.append({
                "id": p.get("id", ""),
                "title": title_clean,
                "date": p.get("date", ""),
                "link": p.get("link", ""),
                "excerpt": excerpt_clean,
            })

        total_pages = int(r.headers.get("X-WP-TotalPages", 1))
        log.info("Page %d/%d – got %d posts", page, total_pages, len(posts))

        if page >= total_pages:
            break
        page += 1

    # Fallback: scrape news from HTML pages if API is blocked
    if not all_posts:
        log.info("Trying HTML fallback for news...")
        for news_url in ["https://www.nupco.com/category/news/",
                         "https://www.nupco.com/media-center/news/",
                         "https://www.nupco.com/news/",
                         "https://www.nupco.com/media-center/"]:
            try:
                r = fetch(news_url)
                soup = BeautifulSoup(r.text, "lxml")
                # Look for article/post elements
                articles = soup.find_all("article")
                if not articles:
                    articles = soup.select(".post, .news-item, [class*='post'], .elementor-post")
                for art in articles:
                    title_el = art.find(["h2", "h3", "h4", "a"])
                    title = title_el.get_text(strip=True) if title_el else ""
                    link = ""
                    a_tag = art.find("a", href=True)
                    if a_tag:
                        link = urljoin(news_url, a_tag["href"])
                    excerpt_el = art.find("p")
                    excerpt = excerpt_el.get_text(strip=True) if excerpt_el else ""
                    date_el = art.find(class_=re.compile(r"date|time", re.I))
                    date_str = date_el.get_text(strip=True) if date_el else ""
                    if title:
                        all_posts.append({
                            "id": "",
                            "title": title,
                            "date": date_str,
                            "link": link,
                            "excerpt": excerpt,
                        })
                if all_posts:
                    log.info("Found %d posts from %s", len(all_posts), news_url)
                    break
            except Exception as e:
                log.warning("HTML fallback failed for %s: %s", news_url, e)

    csv_path = out_dir / "news.csv"
    if all_posts:
        df = pd.DataFrame(all_posts)
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        log.info("Saved %d news posts to %s", len(all_posts), csv_path.name)
    else:
        log.warning("No news posts found")
        pd.DataFrame(columns=["id", "title", "date", "link", "excerpt"]).to_csv(
            csv_path, index=False, encoding="utf-8-sig"
        )



# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    log.info("=" * 60)
    log.info("NUPCO Scraper starting")
    log.info("=" * 60)

    init_session()

    scrape_catalogue()
    scrape_tenders_list()
    scrape_tenders_plan()
    scrape_news()

    log.info("=" * 60)
    log.info("NUPCO Scraper finished")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
