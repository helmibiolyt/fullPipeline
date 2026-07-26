#!/usr/bin/env python3
"""Scraper for COSMIC cancer mutation data via NIH Clinical Table Search API
and COSMIC Mutational Signatures public downloads."""

import csv
import json
import logging
import sys
import time
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

NIH_API = "https://clinicaltables.nlm.nih.gov/api/cosmic/v4/search"
SIGNATURES_URL = "https://cancer.sanger.ac.uk/signatures/downloads/"

EXTRA_FIELDS = "GeneName,MutationAA,MutationCDS,MutationDescription,PrimarySite,PrimaryHistology,PubmedPMID,MutationGenomePosition,MutationID"
EXTRA_FIELDS_LIST = EXTRA_FIELDS.split(",")

CSV_COLUMNS = [
    "MutationID", "GeneName", "MutationAA", "MutationCDS",
    "MutationDescription", "PrimarySite", "PrimaryHistology",
    "PubmedPMID", "MutationGenomePosition",
]

GENES = [
    "BRAF", "KRAS", "TP53", "EGFR", "PIK3CA", "BRCA1", "BRCA2", "ALK",
    "ERBB2", "PTEN", "MYC", "RB1", "APC", "VHL", "NRAS", "IDH1", "IDH2",
    "FGFR2", "FGFR3", "RET", "MET", "KIT", "PDGFRA", "ABL1", "JAK2",
    "NPM1", "FLT3", "DNMT3A", "TET2", "SF3B1",
]

CANCER_SITES = [
    "lung", "breast", "liver", "colon", "skin", "brain", "kidney",
    "pancreas", "ovary", "prostate", "stomach", "thyroid", "bladder",
]

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "COSMIC-Scraper/1.0"})


def _query_nih(terms: str, max_list: int = 500) -> list[dict]:
    """Query NIH Clinical Table Search API and return parsed records."""
    params = {
        "terms": terms,
        "maxList": max_list,
        "ef": EXTRA_FIELDS,
    }
    resp = SESSION.get(NIH_API, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    # Response: [total_count, code_list, extra_fields_dict, display_strings]
    if not data or len(data) < 3:
        return []
    total = data[0]
    extra = data[2]  # dict of field_name -> list of values
    if not extra or not isinstance(extra, dict):
        return []
    # Determine how many records
    first_key = next(iter(extra), None)
    if not first_key or not extra[first_key]:
        return []
    n = len(extra[first_key])
    records = []
    for i in range(n):
        row = {}
        for field in EXTRA_FIELDS_LIST:
            vals = extra.get(field, [])
            row[field] = vals[i] if i < len(vals) else ""
        records.append(row)
    log.info("  Query '%s': %d total matches, %d returned", terms, total, n)
    return records


def _write_csv(path: Path, records: list[dict]):
    """Write records to CSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    log.info("  Saved %d records to %s", len(records), path.name)


def scrape_gene_mutations():
    """Scrape mutations for key cancer genes."""
    out_dir = BASE_DIR / "Gene_Mutations"
    out_dir.mkdir(parents=True, exist_ok=True)
    for gene in GENES:
        csv_path = out_dir / f"{gene}_mutations.csv"
        try:
            records = _query_nih(gene)
            if records:
                _write_csv(csv_path, records)
            else:
                log.warning("  No results for gene %s", gene)
        except Exception as e:
            log.error("  Error querying gene %s: %s", gene, e)
        time.sleep(0.3)
    log.info("Gene_Mutations complete.")


def scrape_cancer_site_mutations():
    """Scrape mutations by cancer site."""
    out_dir = BASE_DIR / "Cancer_Site_Mutations"
    out_dir.mkdir(parents=True, exist_ok=True)
    for site in CANCER_SITES:
        csv_path = out_dir / f"{site}_mutations.csv"
        try:
            records = _query_nih(site)
            if records:
                _write_csv(csv_path, records)
            else:
                log.warning("  No results for site %s", site)
        except Exception as e:
            log.error("  Error querying site %s: %s", site, e)
        time.sleep(0.3)
    log.info("Cancer_Site_Mutations complete.")


def scrape_mutational_signatures():
    """Download public mutational signature files from COSMIC."""
    out_dir = BASE_DIR / "Mutational_Signatures"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        resp = SESSION.get(SIGNATURES_URL, timeout=30)
        resp.raise_for_status()
        html = resp.text
        # Extract links to TSV/CSV files
        import re
        links = re.findall(r'href="([^"]*\.(?:tsv|csv|txt)[^"]*)"', html, re.IGNORECASE)
        # Also check for absolute URLs
        abs_links = re.findall(r'href="(https?://[^"]*\.(?:tsv|csv|txt)[^"]*)"', html, re.IGNORECASE)
        # Combine and deduplicate
        all_links = list(set(links + abs_links))
        if not all_links:
            # Try broader pattern for download links
            all_links = re.findall(r'href="([^"]*(?:download|signature)[^"]*)"', html, re.IGNORECASE)
        log.info("Found %d potential signature files to download.", len(all_links))
        for link in all_links:
            if link.startswith("/"):
                url = f"https://cancer.sanger.ac.uk{link}"
            elif not link.startswith("http"):
                url = f"https://cancer.sanger.ac.uk/signatures/downloads/{link}"
            else:
                url = link
            fname = Path(link.split("?")[0].split("/")[-1]).name
            if not fname:
                continue
            fpath = out_dir / fname
            try:
                log.info("  Downloading %s ...", fname)
                dl = SESSION.get(url, timeout=60)
                dl.raise_for_status()
                fpath.write_bytes(dl.content)
                log.info("  Saved %s (%d bytes)", fname, len(dl.content))
            except Exception as e:
                log.warning("  Failed to download %s: %s", fname, e)
            time.sleep(0.3)
    except Exception as e:
        log.error("Error fetching signatures page: %s", e)
    log.info("Mutational_Signatures complete.")


def main():
    log.info("Starting COSMIC scraper...")
    scrape_gene_mutations()
    scrape_cancer_site_mutations()
    scrape_mutational_signatures()
    log.info("All scraping complete.")

    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
if __name__ == "__main__":
    main()
