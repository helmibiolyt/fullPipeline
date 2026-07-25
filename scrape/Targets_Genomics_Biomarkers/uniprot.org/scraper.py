#!/usr/bin/env python3
"""
UniProt Scraper
Scrapes pharma/clinical-relevant protein data from https://www.uniprot.org/
via its free REST API (no authentication required).

Data collected:
1. Drug Target Proteins (keyword KW-0621)
2. Disease-Associated Proteins (cancer, diabetes, alzheimer, cardiomyopathy, epilepsy)
3. Enzyme Classes (kinases, proteases, phosphatases)
4. Proto-oncogenes (keyword KW-0656)
5. Tumor Suppressors (keyword KW-0043)
"""

import csv
import io
import sys
import time
import logging
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
BASE_API = "https://rest.uniprot.org"

HEADERS = {
    "User-Agent": "PharmaDataScraper/1.0 (postlytllp@gmail.com)",
    "Accept": "text/plain",
}

REQUEST_DELAY = 1  # seconds between requests

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)

# ---------------------------------------------------------------------------
# Folder structure
# ---------------------------------------------------------------------------
FOLDERS = [
    "Drug_Targets",
    "Disease_Proteins",
    "Enzyme_Classes",
    "Oncogenes",
    "Tumor_Suppressors",
]

# ---------------------------------------------------------------------------
# Query definitions
# ---------------------------------------------------------------------------

DRUG_TARGETS = {
    "name": "drug_target_proteins",
    "folder": "Drug_Targets",
    "query": "(organism_id:9606) AND (reviewed:true) AND (keyword:KW-0621)",
    "fields": "accession,gene_names,protein_name,cc_function,cc_disease,cc_subcellular_location,xref_pdb,ec",
}

DISEASE_QUERIES = {
    "cancer": {
        "query": '(organism_id:9606) AND (reviewed:true) AND (cc_disease:"cancer")',
        "fields": "accession,gene_names,protein_name,cc_disease,cc_function",
    },
    "diabetes": {
        "query": '(organism_id:9606) AND (reviewed:true) AND (cc_disease:"diabetes")',
        "fields": "accession,gene_names,protein_name,cc_disease,cc_function",
    },
    "alzheimer": {
        "query": '(organism_id:9606) AND (reviewed:true) AND (cc_disease:"alzheimer")',
        "fields": "accession,gene_names,protein_name,cc_disease,cc_function",
    },
    "cardiomyopathy": {
        "query": '(organism_id:9606) AND (reviewed:true) AND (cc_disease:"cardiomyopathy")',
        "fields": "accession,gene_names,protein_name,cc_disease,cc_function",
    },
    "epilepsy": {
        "query": '(organism_id:9606) AND (reviewed:true) AND (cc_disease:"epilepsy")',
        "fields": "accession,gene_names,protein_name,cc_disease,cc_function",
    },
}

ENZYME_QUERIES = {
    "kinases": {
        "query": "(organism_id:9606) AND (reviewed:true) AND (ec:2.7.*)",
        "fields": "accession,gene_names,protein_name,ec,cc_function,cc_disease",
    },
    "proteases": {
        "query": "(organism_id:9606) AND (reviewed:true) AND (ec:3.4.*)",
        "fields": "accession,gene_names,protein_name,ec,cc_function,cc_disease",
    },
    "phosphatases": {
        "query": "(organism_id:9606) AND (reviewed:true) AND (ec:3.1.3.*)",
        "fields": "accession,gene_names,protein_name,ec,cc_function,cc_disease",
    },
}

ONCOGENES = {
    "name": "proto_oncogenes",
    "folder": "Oncogenes",
    "query": "(organism_id:9606) AND (reviewed:true) AND (keyword:KW-0656)",
    "fields": "accession,gene_names,protein_name,cc_function,cc_disease",
}

TUMOR_SUPPRESSORS = {
    "name": "tumor_suppressors",
    "folder": "Tumor_Suppressors",
    "query": "(organism_id:9606) AND (reviewed:true) AND (keyword:KW-0043)",
    "fields": "accession,gene_names,protein_name,cc_function,cc_disease",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def stream_tsv(query: str, fields: str) -> str:
    """Fetch a TSV result set from the UniProt REST API (search endpoint with pagination)."""
    url = f"{BASE_API}/uniprotkb/search"
    page_size = 500
    params = {
        "query": query,
        "format": "tsv",
        "fields": fields,
        "size": page_size,
    }
    log.info("Requesting: %s  query=%s", url, query[:80])

    all_lines = []
    header = None
    page = 0

    while url:
        if page == 0:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=300)
        else:
            # Subsequent pages use the full URL from the Link header
            resp = requests.get(url, headers=HEADERS, timeout=300)
        resp.raise_for_status()

        lines = resp.text.rstrip("\n").split("\n")
        if page == 0:
            header = lines[0]
            all_lines.append(header)
            lines = lines[1:]
        else:
            # Skip header on subsequent pages
            if lines and lines[0] == header:
                lines = lines[1:]

        all_lines.extend(lines)
        page += 1

        # Check for next page via Link header
        url = None
        link = resp.headers.get("Link", "")
        if 'rel="next"' in link:
            # Parse: <URL>; rel="next"
            url = link.split(">")[0].lstrip("<")

    content = "\n".join(all_lines) + "\n"
    log.info("Received %d bytes (%d data rows) across %d page(s)",
             len(content), len(all_lines) - 1, page)
    return content


def tsv_to_csv(tsv_text: str, csv_path: Path) -> int:
    """Convert TSV text to a CSV file. Returns row count (excluding header)."""
    reader = csv.reader(io.StringIO(tsv_text), delimiter="\t")
    rows_written = 0
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for row in reader:
            writer.writerow(row)
            rows_written += 1
    data_rows = max(0, rows_written - 1)  # exclude header
    log.info("Wrote %d data rows to %s", data_rows, csv_path.name)
    return data_rows


# ---------------------------------------------------------------------------
# Scrape functions
# ---------------------------------------------------------------------------

def scrape_drug_targets() -> None:
    """Scrape drug target proteins (keyword KW-0621)."""
    folder = BASE_DIR / DRUG_TARGETS["folder"]
    folder.mkdir(parents=True, exist_ok=True)

    tsv = stream_tsv(DRUG_TARGETS["query"], DRUG_TARGETS["fields"])
    tsv_to_csv(tsv, folder / "drug_target_proteins.csv")
    time.sleep(REQUEST_DELAY)


def scrape_disease_proteins() -> None:
    """Scrape disease-associated proteins for key disease areas."""
    folder = BASE_DIR / "Disease_Proteins"
    folder.mkdir(parents=True, exist_ok=True)

    for disease, cfg in DISEASE_QUERIES.items():
        log.info("Scraping disease proteins: %s", disease)
        tsv = stream_tsv(cfg["query"], cfg["fields"])
        tsv_to_csv(tsv, folder / f"{disease}_proteins.csv")
        time.sleep(REQUEST_DELAY)



def scrape_enzyme_classes() -> None:
    """Scrape enzyme classes relevant as drug targets."""
    folder = BASE_DIR / "Enzyme_Classes"
    folder.mkdir(parents=True, exist_ok=True)

    for enzyme_class, cfg in ENZYME_QUERIES.items():
        log.info("Scraping enzyme class: %s", enzyme_class)
        tsv = stream_tsv(cfg["query"], cfg["fields"])
        tsv_to_csv(tsv, folder / f"{enzyme_class}.csv")
        time.sleep(REQUEST_DELAY)



def scrape_oncogenes() -> None:
    """Scrape proto-oncogenes (keyword KW-0656)."""
    folder = BASE_DIR / ONCOGENES["folder"]
    folder.mkdir(parents=True, exist_ok=True)

    tsv = stream_tsv(ONCOGENES["query"], ONCOGENES["fields"])
    tsv_to_csv(tsv, folder / "proto_oncogenes.csv")
    time.sleep(REQUEST_DELAY)


def scrape_tumor_suppressors() -> None:
    """Scrape tumor suppressor proteins (keyword KW-0043)."""
    folder = BASE_DIR / TUMOR_SUPPRESSORS["folder"]
    folder.mkdir(parents=True, exist_ok=True)

    tsv = stream_tsv(TUMOR_SUPPRESSORS["query"], TUMOR_SUPPRESSORS["fields"])
    tsv_to_csv(tsv, folder / "tumor_suppressors.csv")
    time.sleep(REQUEST_DELAY)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("=" * 60)
    log.info("UniProt Scraper — starting")
    log.info("=" * 60)

    # Create all folders
    for folder_name in FOLDERS:
        (BASE_DIR / folder_name).mkdir(parents=True, exist_ok=True)

    scrape_drug_targets()
    scrape_disease_proteins()
    scrape_enzyme_classes()
    scrape_oncogenes()
    scrape_tumor_suppressors()

    # Post-processing
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from process_files import process_scraper
    log.info("Post-processing downloaded files with MiniMax...")
    process_scraper(BASE_DIR)

    log.info("=" * 60)
    log.info("UniProt Scraper — complete")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
