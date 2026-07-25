#!/usr/bin/env python3
"""
HGNC (HUGO Gene Nomenclature Committee) Scraper
Scrapes gene nomenclature data from genenames.org

Data sources (all free, no auth required):
1. Complete gene set (TSV bulk download)
2. Gene groups (TSV bulk download)
3. REST API for pharma-relevant gene families
"""

import csv
import json
import sys
import time
import logging
from pathlib import Path

import requests
import pandas as pd

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent

COMPLETE_SET_TSV_URL = (
    "https://storage.googleapis.com/public-download-files/hgnc/tsv/tsv/hgnc_complete_set.txt"
)
COMPLETE_SET_JSON_URL = (
    "https://storage.googleapis.com/public-download-files/hgnc/json/json/hgnc_complete_set.json"
)
GENE_GROUPS_URL = "https://www.genenames.org/cgi-bin/genegroup/download-all"
REST_API_BASE = "https://rest.genenames.org"

# Pharma-relevant gene families to query via REST API
PHARMA_GENE_FAMILIES = {
    "kinases": [
        "ABL1", "ABL2", "ALK", "AXL", "BRAF", "BTK", "CDK4", "CDK6",
        "CSF1R", "EGFR", "ERBB2", "ERBB3", "ERBB4", "FGFR1", "FGFR2",
        "FGFR3", "FGFR4", "FLT3", "IGF1R", "JAK1", "JAK2", "JAK3",
        "KDR", "KIT", "MAP2K1", "MAP2K2", "MAPK1", "MAPK3", "MET",
        "MTOR", "NTRK1", "NTRK2", "NTRK3", "PDGFRA", "PDGFRB",
        "PIK3CA", "PIK3CB", "PIK3CD", "PIK3CG", "RAF1", "RET", "ROS1",
        "SRC", "SYK", "TYK2", "VEGFA",
    ],
    "gpcrs": [
        "ADORA1", "ADORA2A", "ADORA2B", "ADORA3", "ADRA1A", "ADRA1B",
        "ADRA2A", "ADRA2B", "ADRA2C", "ADRB1", "ADRB2", "ADRB3",
        "AGTR1", "AGTR2", "AVPR1A", "AVPR1B", "AVPR2", "BDKRB1",
        "BDKRB2", "C5AR1", "CALCR", "CASR", "CCKAR", "CCKBR",
        "CCR1", "CCR2", "CCR3", "CCR4", "CCR5", "CCR6", "CCR7",
        "CHRM1", "CHRM2", "CHRM3", "CHRM4", "CHRM5", "CXCR4",
        "DRD1", "DRD2", "DRD3", "DRD4", "DRD5", "EDNRA", "EDNRB",
        "GLP1R", "GLP2R", "GCGR", "GIPR", "GNRHR", "GRM1", "GRM5",
        "HRH1", "HRH2", "HRH3", "HRH4", "HTR1A", "HTR1B", "HTR2A",
        "HTR2C", "LPAR1", "LPAR2", "MC4R", "NTSR1", "OPRD1", "OPRK1",
        "OPRL1", "OPRM1", "OXTR", "PAR1", "PTGER2", "PTGER4",
        "PTH1R", "S1PR1", "S1PR2", "S1PR3", "S1PR4", "S1PR5",
        "SSTR1", "SSTR2", "SSTR3", "SSTR4", "SSTR5", "TACR1",
    ],
    "ion_channels": [
        "CACNA1A", "CACNA1B", "CACNA1C", "CACNA1D", "CACNA1S",
        "CFTR", "CLCN1", "CLCN2", "GABRA1", "GABRA2", "GABRB2",
        "GABRG2", "GRIA1", "GRIA2", "GRIN1", "GRIN2A", "GRIN2B",
        "HCN1", "HCN2", "HCN4", "KCNA1", "KCNA2", "KCNA5",
        "KCND3", "KCNH2", "KCNJ1", "KCNJ2", "KCNJ11", "KCNMA1",
        "KCNQ1", "KCNQ2", "KCNQ3", "KCNQ5", "SCN1A", "SCN2A",
        "SCN3A", "SCN5A", "SCN8A", "SCN9A", "SCN10A", "SCN11A",
        "TRPA1", "TRPM8", "TRPV1", "TRPV3", "TRPV4",
    ],
    "transporters": [
        "ABCB1", "ABCB11", "ABCC2", "ABCC8", "ABCG2",
        "SLC1A1", "SLC1A2", "SLC1A3", "SLC2A1", "SLC2A2", "SLC2A4",
        "SLC5A1", "SLC5A2", "SLC6A1", "SLC6A2", "SLC6A3", "SLC6A4",
        "SLC6A9", "SLC9A3", "SLC10A1", "SLC10A2", "SLC12A1",
        "SLC12A3", "SLC15A1", "SLC15A2", "SLC22A1", "SLC22A2",
        "SLC22A6", "SLC22A8", "SLC22A11", "SLC22A12", "SLCO1B1",
        "SLCO1B3", "SLCO2B1", "SLC47A1", "SLC47A2",
    ],
    "nuclear_receptors": [
        "AR", "ESR1", "ESR2", "NR1H2", "NR1H3", "NR1H4", "NR1I2",
        "NR1I3", "NR3C1", "NR3C2", "PGR", "PPARA", "PPARD", "PPARG",
        "RARA", "RARB", "RARG", "RXRA", "RXRB", "RXRG", "THRA",
        "THRB", "VDR",
    ],
}

HEADERS_BROWSER = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
}

REST_API_HEADERS = {
    "Accept": "application/json",
    "User-Agent": HEADERS_BROWSER["User-Agent"],
}

REQUEST_DELAY = 0.15  # max ~6.6 req/sec, well under 10 req/sec limit
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
log = logging.getLogger("genenames")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
session = requests.Session()
session.headers.update(HEADERS_BROWSER)


def fetch(url, headers=None, **kwargs):
    """GET with retry + exponential backoff."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            time.sleep(REQUEST_DELAY)
            r = session.get(url, timeout=120, headers=headers, **kwargs)
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            wait = 2 ** attempt
            log.warning("Attempt %d/%d failed for %s: %s", attempt, MAX_RETRIES, url, exc)
            if attempt == MAX_RETRIES:
                log.error("All retries exhausted for %s", url)
                raise
            time.sleep(wait)


def ensure_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# 1. Download complete gene set (TSV) and convert to CSV
# ---------------------------------------------------------------------------
def scrape_complete_gene_set():
    """Download the HGNC complete gene set TSV and save as CSV."""
    data_dir = ensure_dir(BASE_DIR / "data" / "complete_set")
    log.info("Downloading complete gene set TSV...")

    r = fetch(COMPLETE_SET_TSV_URL)
    tsv_path = data_dir / "hgnc_complete_set.txt"
    tsv_path.write_bytes(r.content)
    log.info("Saved raw TSV: %s (%d bytes)", tsv_path, len(r.content))

    # Convert TSV to CSV
    log.info("Converting TSV to CSV...")
    df = pd.read_csv(tsv_path, sep="\t", dtype=str, low_memory=False)
    csv_path = data_dir / "hgnc_complete_set.csv"
    df.to_csv(csv_path, index=False, quoting=csv.QUOTE_ALL)
    log.info("Saved CSV: %s (%d rows, %d columns)", csv_path, len(df), len(df.columns))


# ---------------------------------------------------------------------------
# 2. Download gene groups (TSV) and convert to CSV
# ---------------------------------------------------------------------------
def scrape_gene_groups():
    """Download gene groups TSV and save as CSV."""
    data_dir = ensure_dir(BASE_DIR / "data" / "gene_groups")
    log.info("Downloading gene groups TSV...")

    r = fetch(GENE_GROUPS_URL)
    tsv_path = data_dir / "gene_groups_all.txt"
    tsv_path.write_bytes(r.content)
    log.info("Saved raw TSV: %s (%d bytes)", tsv_path, len(r.content))

    # Convert TSV to CSV
    log.info("Converting TSV to CSV...")
    df = pd.read_csv(tsv_path, sep="\t", dtype=str, low_memory=False)
    csv_path = data_dir / "gene_groups_all.csv"
    df.to_csv(csv_path, index=False, quoting=csv.QUOTE_ALL)
    log.info("Saved CSV: %s (%d rows, %d columns)", csv_path, len(df), len(df.columns))


# ---------------------------------------------------------------------------
# 3. REST API: Fetch pharma-relevant gene families
# ---------------------------------------------------------------------------
def fetch_gene_details(symbol):
    """Fetch detailed record for a single gene symbol via REST API."""
    url = f"{REST_API_BASE}/fetch/symbol/{symbol}"
    try:
        r = fetch(url, headers=REST_API_HEADERS)
        data = r.json()
        docs = data.get("response", {}).get("docs", [])
        if docs:
            return docs[0]
        else:
            log.warning("No data returned for symbol: %s", symbol)
            return None
    except Exception as exc:
        log.error("Failed to fetch %s: %s", symbol, exc)
        return None


def scrape_pharma_gene_family(family_name, symbols):
    """Fetch all genes in a pharma-relevant family and save as CSV."""
    data_dir = ensure_dir(BASE_DIR / "data" / "pharma_families")
    log.info("Fetching gene family: %s (%d symbols)", family_name, len(symbols))

    records = []
    for i, symbol in enumerate(symbols, 1):
        log.info("  [%d/%d] Fetching %s...", i, len(symbols), symbol)
        rec = fetch_gene_details(symbol)
        if rec:
            records.append(rec)

    if not records:
        log.warning("No records retrieved for family: %s", family_name)
        return

    # Save raw JSON
    json_path = data_dir / f"{family_name}.json"
    json_path.write_text(json.dumps(records, indent=2, default=str), encoding="utf-8")
    log.info("Saved JSON: %s (%d records)", json_path, len(records))

    # Flatten and save as CSV
    df = pd.json_normalize(records, sep="_")
    # Convert list columns to pipe-delimited strings
    for col in df.columns:
        if df[col].apply(lambda x: isinstance(x, list)).any():
            df[col] = df[col].apply(
                lambda x: "|".join(str(v) for v in x) if isinstance(x, list) else x
            )

    csv_path = data_dir / f"{family_name}.csv"
    df.to_csv(csv_path, index=False, quoting=csv.QUOTE_ALL)
    log.info("Saved CSV: %s (%d rows, %d columns)", csv_path, len(df), len(df.columns))


def scrape_all_pharma_families():
    """Iterate over all pharma-relevant gene families."""
    for family_name, symbols in PHARMA_GENE_FAMILIES.items():
        scrape_pharma_gene_family(family_name, symbols)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    log.info("=" * 60)
    log.info("HGNC (genenames.org) scraper starting")
    log.info("=" * 60)

    # 1. Complete gene set
    try:
        scrape_complete_gene_set()
    except Exception as exc:
        log.error("Failed to scrape complete gene set: %s", exc)

    # 2. Gene groups
    try:
        scrape_gene_groups()
    except Exception as exc:
        log.error("Failed to scrape gene groups: %s", exc)

    # 3. Pharma-relevant gene families via REST API
    try:
        scrape_all_pharma_families()
    except Exception as exc:
        log.error("Failed to scrape pharma gene families: %s", exc)

    # Post-processing
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from process_files import process_scraper
    log.info("Post-processing downloaded files with MiniMax...")
    process_scraper(BASE_DIR)

    log.info("=" * 60)
    log.info("HGNC scraper finished")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
