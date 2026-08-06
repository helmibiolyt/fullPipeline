#!/usr/bin/env python3
"""CRIS (Korea) - Clinical Research Information Service, to one CSV.

    python cris_downloader.py                 all pages
    python cris_downloader.py --limit 3       first 3 pages, for testing

CRIS serves its search results as XML from a single POST endpoint, so this
needs no HTML parsing, no browser and no proxy:

    POST /cris/search/selectBasic.do   page, pageSize   ->  <items><item>...

which is the easiest shape a registry has taken in this pipeline. The site was
restructured at some point - every /cris/index.do style path now 404s - and
the endpoint above is what the current search page calls.

The English fields are the ones taken. CRIS stores most values twice, Korean
and English (`research_title_kr` / `research_title_en`), and a few as one
string carrying both - "중재연구(Interventional Study)". Those are written
through unchanged: the graph's norm_study_type and norm_status already read
the English out of prose, and keeping the registry's own wording is the rule
everywhere else in this pipeline.

Writes only inside this folder.
"""
from __future__ import annotations

import argparse
import csv
import html
import re
import sys
import time
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "cris_trials"
OUT_CSV = OUT_DIR / "cris_trials.csv"

URL = "https://cris.nih.go.kr/cris/search/selectBasic.do"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
}
PAGE_SIZE = 100
DELAY = 1.0

#: Written in this order. system_number FIRST because it is the trial id -
#: research_number is the sponsor's own protocol code and is not unique.
COLUMNS = [
    "system_number", "research_number", "research_title_en", "research_title_kr",
    "clinical_step", "research_kind", "research_step", "intervention_type",
    "arm_desc_kr", "cp_contents", "diss_cd", "my_code_en",
    "resrc_spp_en", "resrc_ref_en", "resrc_spp", "resrc_ref",
    "study_start_date", "study_complete_date", "outcome_en",
    "target_in_en", "target_out_en", "target_in_sex",
    "target_in_start_age", "target_in_end_age",
    "irb_status", "results_yn", "ins_date", "udt_date",
]

_ITEM = re.compile(r"<item>(.*?)</item>", re.S)
_TAG = re.compile(r"<([a-zA-Z_][a-zA-Z0-9_]*)>(.*?)</\1>", re.S)
_TOTAL = re.compile(r"<totalDataCnt>(\d+)</totalDataCnt>")


def fetch(session: requests.Session, page: int, size: int,
          retries: int = 4) -> str:
    data = {"page": page, "pageSize": size, "lang": "en", "searchFlg": "Y"}
    for attempt in range(1, retries + 1):
        try:
            r = session.post(URL, data=data, headers=HEADERS, timeout=60)
            r.raise_for_status()
            r.encoding = "utf-8"
            return r.text
        except Exception as e:                            # noqa: BLE001
            wait = min(2 ** attempt, 30)
            print(f"  page {page}: {type(e).__name__}, retry in {wait}s",
                  flush=True)
            time.sleep(wait)
    raise SystemExit(f"page {page} failed after {retries} attempts")


def parse(xml_text: str) -> list[dict]:
    """One dict per <item>, values unescaped and whitespace-collapsed.

    CRIS double-escapes: the payload carries "&#xd;" INSIDE already-escaped
    text, so a single unescape leaves it visible. Unescaped twice, then the
    carriage returns it decodes to are collapsed - a value spanning lines
    would otherwise break the CSV row for every downstream reader.
    """
    out = []
    for chunk in _ITEM.findall(xml_text):
        rec = {}
        for tag, raw in _TAG.findall(chunk):
            v = html.unescape(html.unescape(raw))
            v = " ".join(v.split())
            if v:
                rec[tag] = v
        if rec.get("system_number"):
            out.append(rec)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="stop after N pages (testing)")
    ap.add_argument("--page-size", type=int, default=PAGE_SIZE)
    ap.add_argument("--delay", type=float, default=DELAY)
    a = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()

    first = fetch(session, 1, a.page_size)
    m = _TOTAL.search(first)
    total = int(m.group(1)) if m else 0
    if not total:
        raise SystemExit("could not read totalDataCnt - endpoint changed?")

    # The server IGNORES pageSize and returns its own number - 20, whatever is
    # asked for. Trusting the requested size is not a cosmetic error: at
    # pageSize=100 the page count comes out five times too small, the crawl
    # stops early, and a fifth of the registry publishes as if it were all of
    # it. So the real per-page count is measured from page 1 and used.
    per_page = len(parse(first))
    if not per_page:
        raise SystemExit("page 1 parsed to nothing - endpoint changed?")
    if per_page != a.page_size:
        print(f"note: asked for {a.page_size}/page, server gives {per_page}",
              flush=True)
    pages = (total + per_page - 1) // per_page
    if a.limit:
        pages = min(pages, a.limit)
    print(f"CRIS: {total:,} trials, {pages:,} pages of {per_page}", flush=True)

    seen: set[str] = set()
    rows: list[dict] = []
    for page in range(1, pages + 1):
        xml_text = first if page == 1 else fetch(session, page, a.page_size)
        got = parse(xml_text)
        # The endpoint repeats a trial across pages when a record has several
        # sub-studies, so dedupe on the id rather than trusting pagination.
        fresh = [r for r in got if r["system_number"] not in seen]
        seen.update(r["system_number"] for r in fresh)
        rows.extend(fresh)
        if page % 10 == 0 or page == pages:
            print(f"  page {page}/{pages}  {len(rows):,} trials", flush=True)
        if not got:
            print(f"  page {page} returned nothing - stopping", flush=True)
            break
        if page < pages:
            time.sleep(a.delay)

    if not rows:
        raise SystemExit("no trials parsed - refusing to write an empty CSV")

    # Pages overlap - a trial with sub-studies comes back on more than one -
    # so the unique count lands slightly under totalDataCnt and that is
    # expected. A LARGE shortfall means the crawl stopped early, and writing
    # that over a good CSV is how a registry silently shrinks. Fail instead,
    # unless --limit says a partial run was the point.
    if not a.limit:
        got = len(rows) / total
        print(f"collected {len(rows):,} of {total:,} ({got:.1%})")
        if got < 0.9:
            raise SystemExit(
                f"only {got:.1%} of the registry - refusing to publish a "
                f"partial dataset over the previous one")

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {OUT_CSV}  ({len(rows):,} trials, "
          f"{OUT_CSV.stat().st_size / 1048576:.1f} MB)")


if __name__ == "__main__":
    sys.exit(main())
