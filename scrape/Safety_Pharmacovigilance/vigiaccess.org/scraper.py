#!/usr/bin/env python3
"""
VigiAccess (WHO / Uppsala Monitoring Centre) scraper
====================================================
Source : https://vigiaccess.org/
Topic  : Safety_Pharmacovigilance

VigiAccess is an Angular single-page app that exposes WHO's global database of
reported potential side effects (individual case safety reports / ADRs). The
rendered text is DELIBERATELY OBFUSCATED with invisible unicode characters
(category Cf plus zero-width joiners, word-joiner, BOM, etc.) inserted between
letters to defeat scraping, and numbers are separated with narrow no-break
spaces (U+202F). We drive the real UI with Playwright (headful chromium under
xvfb) and strip the obfuscation before saving.

Flow per drug:
  1. Open homepage, accept the terms-and-conditions disclaimer.
  2. Click "Search database", type the drug, press Enter.
  3. Pick the first matching medicinal product from the results table.
  4. Confirm the "Drug selected" dialog (Ok).
  5. Read the "Reported potential side effects" accordion: one row per
     System Organ Class (SOC) with a percentage + ADR count. Expand each SOC
     to capture the individual adverse reactions and their counts.

Outputs (CSV only, inside BASE_DIR/VigiAccess/):
  - vigiaccess_adr.csv   : search_term, product_name, system_organ_class,
                           soc_percent, soc_total_count, adverse_reaction,
                           reaction_count, total_reports, dataset_date
  - vigiaccess_meta.csv  : search_term, product_name, total_reports, dataset_date

Contract: runnable as `python scraper.py`; resolves its own path via BASE_DIR;
writes only inside its own folder; exits non-zero if the final CSV is empty.
"""

import csv
import re
import sys
import time
import unicodedata
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# -- Configuration -----------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "VigiAccess"
OUT_DIR.mkdir(parents=True, exist_ok=True)

HOMEPAGE = "https://vigiaccess.org/"
DRUGS = ["paracetamol", "ibuprofen", "aspirin", "metformin", "atorvastatin"]

# Keep the run bounded: cap reactions captured per SOC (no "Load more" paging).
MAX_REACTIONS_PER_SOC = 25

# Invisible / zero-width characters used to obfuscate the visible text.
_ZERO_WIDTH = "​‌‍⁠﻿"
# Unicode spaces that appear inside numbers / around punctuation.
_UNICODE_SPACES = "       　᠎"


def clean(s: str) -> str:
    """Strip the invisible obfuscation chars.

    VigiAccess inserts invisible characters *between* letters to defeat scraping.
    These are not only zero-width format chars (category Cf) but also invisible
    COMBINING marks — notably U+034F COMBINING GRAPHEME JOINER (category Mn) — e.g.
    'Thrombocytop<U+034F>enia'. Drop all format/control/combining categories; the
    VigiAccess terms are ASCII English MedDRA labels, so no real accents are lost.
    """
    if not s:
        return ""
    return "".join(
        ch for ch in s
        if unicodedata.category(ch) not in ("Cf", "Cc", "Mn", "Me")
        and ch not in _ZERO_WIDTH
    ).strip()


def norm(s: str) -> str:
    """clean() + normalise weird unicode spaces to a plain space and collapse."""
    s = clean(s)
    for sp in _UNICODE_SPACES:
        s = s.replace(sp, " ")
    return re.sub(r"\s+", " ", s).strip()


def to_int(s: str):
    """Extract an integer from an obfuscated, space-separated number, or None."""
    digits = re.sub(r"[^\d]", "", clean(s))
    return int(digits) if digits else None


# Matches "System Organ Class ( 13%,  53 614 ADRs )" after norm().
_SOC_RE = re.compile(r"^(?P<name>.+?)\s*\(\s*(?P<pct>\d+)\s*%,\s*(?P<cnt>[\d ]+?)\s*ADRs?\s*\)\s*$")
# Matches "Reaction name ( 1 075 )" after norm().
_RX_RE = re.compile(r"^(?P<name>.+?)\s*\(\s*(?P<cnt>[\d ]+?)\s*\)\s*$")


def parse_soc(text: str):
    m = _SOC_RE.match(norm(text))
    if not m:
        return None
    return {
        "system_organ_class": m.group("name").strip(),
        "soc_percent": int(m.group("pct")),
        "soc_total_count": to_int(m.group("cnt")),
    }


def parse_reaction(text: str):
    t = norm(text)
    if "ADRs" in t or t.endswith("...") or "Load more" in t:
        return None
    m = _RX_RE.match(t)
    if not m:
        return None
    cnt = to_int(m.group("cnt"))
    if cnt is None:
        return None
    return {"adverse_reaction": m.group("name").strip(), "reaction_count": cnt}


def extract_dataset_date(body_text: str) -> str:
    m = re.search(r"data set date is\s*([\d]{1,2}/[\d]{1,2}/[\d]{4})", body_text, re.I)
    return m.group(1) if m else ""


def extract_total_reports(body_text: str):
    m = re.search(r"There are\s+([\d ]+?)\s+reports", body_text)
    return to_int(m.group(1)) if m else None


def scrape_drug(page, term: str):
    """Return (rows, meta) for a single drug. Raises on hard failure."""
    print(f"\n[{term}] starting")
    page.goto(HOMEPAGE, wait_until="domcontentloaded", timeout=60_000)
    time.sleep(3)

    # 1. Accept the terms-and-conditions disclaimer (checkbox is custom-styled;
    #    the label carries the click handler). Harmless if already accepted.
    try:
        page.locator("label[for=accept-terms-and-conditions]").click(force=True, timeout=8_000)
        time.sleep(1)
    except Exception as e:
        print(f"[{term}] terms label not clickable (may be already accepted): {e}")

    # 2. Open the search UI.
    page.get_by_text("Search database", exact=False).first.click(timeout=15_000)
    time.sleep(3)

    # 3. Type the drug and search.
    inp = page.locator("input").first
    inp.fill(term)
    time.sleep(2)
    inp.press("Enter")
    time.sleep(5)

    # 4. Pick the first matching product from the results table.
    rows = page.locator("table.is-hoverable tbody tr")
    if rows.count() == 0:
        print(f"[{term}] no product results")
        return [], None
    # product name = first (top) line of the row (brand / active ingredient).
    # VigiAccess renders the name twice in the row (a visible + a duplicate node);
    # once the invisible separators are stripped the two copies merge into e.g.
    # "ParacetamolParacetamol", so collapse an exact full-string duplication.
    raw_first = clean(rows.nth(0).inner_text())
    product_name = norm(raw_first.split("\n")[0]) if raw_first else term
    _n = len(product_name)
    if _n and _n % 2 == 0 and product_name[: _n // 2] == product_name[_n // 2:]:
        product_name = product_name[: _n // 2]
    rows.nth(0).click()
    time.sleep(3)

    # 5. Confirm the "Drug selected" dialog.
    try:
        page.get_by_text("Ok", exact=True).first.click(timeout=10_000)
    except Exception as e:
        print(f"[{term}] no 'Ok' confirm dialog: {e}")
    time.sleep(7)

    body = clean(page.inner_text("body"))
    dataset_date = extract_dataset_date(body)
    total_reports = extract_total_reports(body)
    print(f"[{term}] product={product_name!r} total_reports={total_reports} dataset_date={dataset_date}")

    socs = page.locator('span[dtid="dashboard-socrow"]')
    nsoc = socs.count()
    print(f"[{term}] {nsoc} System Organ Classes")
    if nsoc == 0:
        return [], {"search_term": term, "product_name": product_name,
                    "total_reports": total_reports, "dataset_date": dataset_date}

    out_rows = []
    for i in range(nsoc):
        try:
            soc_text = socs.nth(i).inner_text()
        except Exception:
            continue
        soc = parse_soc(soc_text)
        if not soc:
            continue

        reactions = []
        try:
            socs.nth(i).click(timeout=5_000)          # expand
            time.sleep(1.2)
            pt = page.locator(f'[dtid="dashboard-ptrow-{i}"]')
            npt = pt.count()
            for j in range(min(npt, MAX_REACTIONS_PER_SOC)):
                rx = parse_reaction(pt.nth(j).inner_text())
                if rx:
                    reactions.append(rx)
            socs.nth(i).click(timeout=5_000)          # collapse to keep DOM small
            time.sleep(0.4)
        except Exception as e:
            print(f"[{term}] SOC #{i} '{soc['system_organ_class']}' expand issue: {e}")

        if reactions:
            for rx in reactions:
                out_rows.append({
                    "search_term": term,
                    "product_name": product_name,
                    "system_organ_class": soc["system_organ_class"],
                    "soc_percent": soc["soc_percent"],
                    "soc_total_count": soc["soc_total_count"],
                    "adverse_reaction": rx["adverse_reaction"],
                    "reaction_count": rx["reaction_count"],
                    "total_reports": total_reports,
                    "dataset_date": dataset_date,
                })
        else:
            # Fallback: at minimum record the SOC-level total.
            out_rows.append({
                "search_term": term,
                "product_name": product_name,
                "system_organ_class": soc["system_organ_class"],
                "soc_percent": soc["soc_percent"],
                "soc_total_count": soc["soc_total_count"],
                "adverse_reaction": "",
                "reaction_count": "",
                "total_reports": total_reports,
                "dataset_date": dataset_date,
            })

    print(f"[{term}] collected {len(out_rows)} rows")
    meta = {"search_term": term, "product_name": product_name,
            "total_reports": total_reports, "dataset_date": dataset_date}
    return out_rows, meta


def write_csv(path: Path, rows, fieldnames):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def main():
    all_rows = []
    meta_rows = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)   # headful -> run under xvfb-run
        context = browser.new_context(viewport={"width": 1400, "height": 1000})
        page = context.new_page()

        for term in DRUGS:
            try:
                rows, meta = scrape_drug(page, term)
                all_rows.extend(rows)
                if meta:
                    meta_rows.append(meta)
            except PlaywrightTimeout as e:
                print(f"[{term}] TIMEOUT: {e}")
            except Exception as e:
                print(f"[{term}] FAILED: {e}")

        browser.close()

    if not all_rows:
        sys.exit("VigiAccess: no ADR data scraped from any drug -> failing")

    adr_fields = ["search_term", "product_name", "system_organ_class",
                  "soc_percent", "soc_total_count", "adverse_reaction",
                  "reaction_count", "total_reports", "dataset_date"]
    write_csv(OUT_DIR / "vigiaccess_adr.csv", all_rows, adr_fields)

    if meta_rows:
        write_csv(OUT_DIR / "vigiaccess_meta.csv", meta_rows,
                  ["search_term", "product_name", "total_reports", "dataset_date"])

    print(f"\nDONE: {len(all_rows)} ADR rows across {len(meta_rows)} products "
          f"-> {OUT_DIR / 'vigiaccess_adr.csv'}")


if __name__ == "__main__":
    main()
