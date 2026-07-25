#!/usr/bin/env python3
"""
MedDRA (meddra.org) scraper — Safety & Pharmacovigilance.

MedDRA (Medical Dictionary for Regulatory Activities) is maintained by the MSSO.
The FULL terminology (SOC / HLGT / HLT / PT / LLT hierarchy) is NOT publicly
downloadable: it requires a (free) MSSO subscription account and is delivered as
ASCII/MedDRA-format files behind a login. That part is intentionally out of scope
here (see README.md).

What IS public and scraped here comes from the official MedDRA site backend, a
Drupal JSON API that powers the Angular front-end at https://www.meddra.org/ :

    https://admin.meddra.org/api/nodes      -> all published content nodes
    https://admin.meddra.org/api/timelines  -> the "Evolving MedDRA" milestone timeline

From those we build two structured CSVs of public reference data:

    MedDRA/meddra_versions.csv  -> MedDRA release history (version, release_date, ...)
    MedDRA/meddra_timeline.csv  -> "Evolving MedDRA" milestones (date, title, ...)

No authentication is required for any of the above. No secrets are hardcoded; if
the full-dictionary download is added later, read credentials from the environment
(MEDDRA_USERNAME / MEDDRA_PASSWORD) — never hardcode them.

Runnable as: python scraper.py
Exits non-zero if no data could be scraped.
"""

import csv
import logging
import re
import sys
import time
from pathlib import Path

import requests

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - bs4 is expected to be present
    BeautifulSoup = None

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "MedDRA"

API_BASE = "https://admin.meddra.org/api"
SITE_BASE = "https://www.meddra.org"
PAGE_SIZE = 200          # server default page size for /nodes
MAX_OFFSET = 5000        # safety cap on pagination
REQUEST_DELAY = 0.4
MAX_RETRIES = 4
TIMEOUT = 60

MONTHS = (
    "January|February|March|April|May|June|July|August|"
    "September|October|November|December"
)
VERSION_RE = re.compile(r"MedDRA\s+Version\s+(\d+\.\d+)", re.IGNORECASE)
MONTH_YEAR_RE = re.compile(rf"({MONTHS})\s+(\d{{4}})")

log_fmt = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=log_fmt,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(BASE_DIR / "scraper.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("meddra")

session = requests.Session()
session.headers.update(
    {
        "User-Agent": "Mozilla/5.0 (compatible; pipeline-scraper/1.0)",
        "Accept": "application/json",
    }
)


def api_get(path: str, params: dict | None = None):
    """GET a MedDRA API endpoint with retries. Returns parsed JSON or None."""
    url = f"{API_BASE}/{path.lstrip('/')}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, params=params, timeout=TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001 - be robust to any network error
            log.warning(
                "GET %s (params=%s) attempt %d/%d failed: %s",
                path, params, attempt, MAX_RETRIES, exc,
            )
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
    log.error("Giving up on %s after %d attempts", path, MAX_RETRIES)
    return None


def html_to_text(html: str) -> str:
    """Collapse an HTML fragment to a short plain-text summary."""
    if not html:
        return ""
    if BeautifulSoup is not None:
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    else:  # pragma: no cover - fallback if bs4 missing
        text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:500]


def full_url(alias: str | None) -> str:
    if not alias:
        return ""
    if alias.startswith("http"):
        return alias
    return SITE_BASE + alias


def fetch_all_nodes() -> list[dict]:
    """Paginate /api/nodes via the `offset` parameter until exhausted."""
    nodes: list[dict] = []
    offset = 0
    expected = None
    while offset <= MAX_OFFSET:
        data = api_get("nodes", params={"offset": offset})
        if not data:
            break
        items = data.get("items", []) if isinstance(data, dict) else []
        if expected is None and isinstance(data, dict):
            expected = (data.get("meta") or {}).get("maxcount")
        if not items:
            break
        nodes.extend(items)
        log.info("Fetched nodes offset=%d (+%d, total=%d, maxcount=%s)",
                 offset, len(items), len(nodes), expected)
        if len(items) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        time.sleep(REQUEST_DELAY)
    return nodes


def build_versions(nodes: list[dict]) -> list[dict]:
    """Derive MedDRA release history from content-node titles.

    Every version has English release/guidance nodes titled e.g.
    "MedDRA Version 29.0 March 2026" and an announcement article
    "English MedDRA Version 29.0 is now available for download". We key on the
    version number and enrich with a scraped "Month YYYY" release date and the
    announcement article's URL/date where present.
    """
    versions: dict[str, dict] = {}
    for node in nodes:
        title = (node.get("title") or "").strip()
        m = VERSION_RE.search(title)
        if not m:
            continue
        ver = m.group(1)
        rec = versions.setdefault(
            ver,
            {
                "version": ver,
                "release_date": "",
                "announcement_date": "",
                "source_title": "",
                "source_url": "",
            },
        )
        my = MONTH_YEAR_RE.search(title)
        if my and not rec["release_date"]:
            rec["release_date"] = f"{my.group(1)} {my.group(2)}"
        low = title.lower()
        if "available for download" in low and "english" in low:
            rec["announcement_date"] = node.get("created", "") or ""
            rec["source_title"] = title
            rec["source_url"] = full_url(node.get("alias"))
        # Fall back to any English node as the source reference.
        if not rec["source_title"] and title.isascii():
            rec["source_title"] = title
            rec["source_url"] = full_url(node.get("alias"))

    rows = sorted(versions.values(), key=lambda r: _ver_key(r["version"]))
    return rows


def _ver_key(v: str) -> tuple[int, int]:
    try:
        major, minor = v.split(".")
        return int(major), int(minor)
    except Exception:  # noqa: BLE001
        return (0, 0)


def build_timeline(timeline_json) -> list[dict]:
    """Flatten the 'Evolving MedDRA' milestone timeline into rows."""
    if isinstance(timeline_json, dict):
        items = timeline_json.get("items", [])
    elif isinstance(timeline_json, list):
        items = timeline_json
    else:
        items = []
    rows = []
    for it in items:
        body = it.get("body") or {}
        summary = ""
        if isinstance(body, dict):
            summary = html_to_text(body.get("summary") or body.get("text") or "")
        rows.append(
            {
                "date": it.get("evolvingDate") or "",
                "title": (it.get("title") or "").strip(),
                "url": full_url(it.get("alias")),
                "summary": summary,
            }
        )
    rows = [r for r in rows if r["title"]]
    rows.sort(key=lambda r: r["date"] or "", reverse=True)
    return rows


def write_csv(rows: list[dict], path: Path, fields: list[str]) -> int:
    if not rows:
        log.warning("No rows to write for %s", path.name)
        return 0
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    log.info("Saved %d rows -> %s", len(rows), path)
    return len(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    total_rows = 0

    # 1) MedDRA release/version history (public, from content nodes).
    try:
        nodes = fetch_all_nodes()
        log.info("Total content nodes fetched: %d", len(nodes))
        version_rows = build_versions(nodes)
        total_rows += write_csv(
            version_rows,
            OUT_DIR / "meddra_versions.csv",
            ["version", "release_date", "announcement_date",
             "source_title", "source_url"],
        )
    except Exception:  # noqa: BLE001
        log.exception("Failed to build MedDRA version history")

    # 2) "Evolving MedDRA" milestone timeline (public).
    try:
        timeline_json = api_get("timelines")
        timeline_rows = build_timeline(timeline_json) if timeline_json else []
        total_rows += write_csv(
            timeline_rows,
            OUT_DIR / "meddra_timeline.csv",
            ["date", "title", "url", "summary"],
        )
    except Exception:  # noqa: BLE001
        log.exception("Failed to build MedDRA timeline")

    if total_rows == 0:
        log.error("No public MedDRA data could be scraped — failing.")
        sys.exit(1)

    log.info("Done. Wrote %d total rows across CSV outputs.", total_rows)


if __name__ == "__main__":
    main()
