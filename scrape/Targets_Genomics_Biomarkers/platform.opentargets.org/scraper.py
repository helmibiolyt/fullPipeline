#!/usr/bin/env python3
"""
Open Targets Platform Scraper
Scrapes drug, disease-target association, and target tractability data
via the public GraphQL API.
"""

import csv
import json
import logging
import sys
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
API_URL = "https://api.platform.opentargets.org/api/v4/graphql"
PAGE_SIZE = 100
REQUEST_DELAY = 0.5
MAX_RETRIES = 3

DISEASE_IDS = {
    "Cancer": "MONDO_0004992",
    "Cardiovascular": "MONDO_0004995",
    "Diabetes": "MONDO_0005015",
    "Respiratory": "MONDO_0005087",
    "Alzheimer": "MONDO_0004975",
    "Infectious_Disease": "MONDO_0005550",
}

DRUG_SEARCH_TERMS = [
    "cancer", "diabetes", "cardiovascular", "antibiotics", "antiviral",
    "immunotherapy", "kinase inhibitor", "monoclonal antibody",
]

FOLDERS = ["Drugs", "Disease_Associations", "Target_Tractability"]

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
log = logging.getLogger("opentargets")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
session = requests.Session()
session.headers.update({"Content-Type": "application/json"})


def graphql(query: str, variables: dict | None = None) -> dict:
    """Execute a GraphQL query with retries."""
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.post(API_URL, json=payload, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                log.warning("GraphQL errors: %s", data["errors"])
            return data.get("data", {})
        except Exception as exc:
            log.warning("Request failed (attempt %d/%d): %s", attempt, MAX_RETRIES, exc)
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
            else:
                raise
    return {}


def ensure_dirs():
    for folder in FOLDERS:
        (BASE_DIR / folder).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 1. Drugs (via search + individual drug detail)
# ---------------------------------------------------------------------------
SEARCH_DRUGS_QUERY = """
query SearchDrugs($queryString: String!, $page: Pagination) {
  search(queryString: $queryString, page: $page, entityNames: ["drug"]) {
    total
    hits {
      id
      entity
      name
      description
    }
  }
}
"""

DRUG_DETAIL_QUERY = """
query DrugInfo($chemblId: String!) {
  drug(chemblId: $chemblId) {
    id
    name
    drugType
    maximumClinicalStage
    mechanismsOfAction {
      rows {
        mechanismOfAction
        targets {
          approvedSymbol
        }
      }
    }
    indications {
      rows {
        disease {
          id
          name
        }
      }
    }
    drugWarnings {
      toxicityClass
      description
      year
      country
      references {
        source
      }
    }
  }
}
"""


def scrape_drugs():
    folder = "Drugs"
    log.info("Scraping drugs via search API...")

    # Step 1: Collect unique drug IDs via search
    drug_hits = {}  # id -> {name, description}
    for term in DRUG_SEARCH_TERMS:
        log.info("  Searching for '%s'...", term)
        page_index = 0
        while True:
            data = graphql(SEARCH_DRUGS_QUERY, {
                "queryString": term,
                "page": {"index": page_index, "size": PAGE_SIZE},
            })
            search_data = data.get("search", {})
            total = search_data.get("total", 0)
            hits = search_data.get("hits", [])
            if not hits:
                break

            for hit in hits:
                if hit.get("entity") == "drug" and hit.get("id"):
                    drug_hits[hit["id"]] = {
                        "name": hit.get("name", ""),
                        "description": hit.get("description", ""),
                    }

            fetched = (page_index + 1) * PAGE_SIZE
            log.info("    Page %d: %d hits (total %d)", page_index, len(hits), total)
            if fetched >= total or len(hits) < PAGE_SIZE:
                break
            page_index += 1
            time.sleep(REQUEST_DELAY)

    log.info("Found %d unique drugs from search. Fetching details...", len(drug_hits))

    # Step 2: Get details for each drug
    drug_csv_path = BASE_DIR / folder / "known_drugs.csv"
    drug_fields = [
        "drug_id", "name", "type", "max_phase",
        "mechanism", "target_symbol", "indication_id", "indication_name",
    ]

    warnings_csv_path = BASE_DIR / folder / "drug_warnings.csv"
    warning_fields = [
        "drug_id", "drug_name", "toxicity_class",
        "description", "country", "year", "reference_source",
    ]

    all_drug_rows = []
    all_warning_rows = []

    for i, (chembl_id, meta) in enumerate(sorted(drug_hits.items())):
        if i > 0 and i % 50 == 0:
            log.info("  Drug detail progress: %d/%d", i, len(drug_hits))
        try:
            data = graphql(DRUG_DETAIL_QUERY, {"chemblId": chembl_id})
            drug = data.get("drug")
            if not drug:
                continue

            base = {
                "drug_id": drug.get("id", ""),
                "name": drug.get("name", ""),
                "type": drug.get("drugType", ""),
                "max_phase": drug.get("maximumClinicalStage", ""),
            }

            mechanisms = drug.get("mechanismsOfAction", {}).get("rows", []) or [{}]
            indications = drug.get("indications", {}).get("rows", []) or [{}]

            for mech in mechanisms:
                mech_name = mech.get("mechanismOfAction", "")
                targets = mech.get("targets", []) or [{}]
                for tgt in targets:
                    symbol = tgt.get("approvedSymbol", "")
                    for ind in indications:
                        disease = ind.get("disease", {}) or {}
                        row = {
                            **base,
                            "mechanism": mech_name,
                            "target_symbol": symbol,
                            "indication_id": disease.get("id", ""),
                            "indication_name": disease.get("name", ""),
                        }
                        all_drug_rows.append(row)

            # Drug warnings
            warnings = drug.get("drugWarnings", []) or []
            for w in warnings:
                refs = w.get("references", []) or [{}]
                for ref in refs:
                    all_warning_rows.append({
                        "drug_id": drug.get("id", ""),
                        "drug_name": drug.get("name", ""),
                        "toxicity_class": w.get("toxicityClass", ""),
                        "description": w.get("description", ""),
                        "country": w.get("country", ""),
                        "year": w.get("year", ""),
                        "reference_source": ref.get("source", ""),
                    })

        except Exception as exc:
            log.warning("Failed to get details for %s: %s", chembl_id, exc)
        time.sleep(REQUEST_DELAY)

    with open(drug_csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=drug_fields)
        writer.writeheader()
        writer.writerows(all_drug_rows)
    log.info("Saved %d drug rows to %s", len(all_drug_rows), drug_csv_path)

    with open(warnings_csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=warning_fields)
        writer.writeheader()
        writer.writerows(all_warning_rows)
    log.info("Saved %d warning rows to %s", len(all_warning_rows), warnings_csv_path)


# ---------------------------------------------------------------------------
# 2. Disease-Target Associations
# ---------------------------------------------------------------------------
DISEASE_ASSOC_QUERY = """
query DiseaseAssoc($efoId: String!, $page: Pagination!) {
  disease(efoId: $efoId) {
    id
    name
    associatedTargets(page: $page) {
      count
      rows {
        target {
          id
          approvedSymbol
          approvedName
        }
        score
        datatypeScores {
          id
          score
        }
      }
    }
  }
}
"""


def scrape_disease_associations():
    folder = "Disease_Associations"
    log.info("Scraping disease-target associations...")

    for disease_label, efo_id in DISEASE_IDS.items():
        log.info("  Fetching associations for %s (%s)...", disease_label, efo_id)
        csv_path = BASE_DIR / folder / f"{disease_label}_targets.csv"
        fields = [
            "disease_id", "disease_name", "target_id", "target_symbol",
            "target_name", "overall_score", "datatype", "datatype_score",
        ]
        all_rows = []
        page_index = 0
        total = None

        while True:
            data = graphql(DISEASE_ASSOC_QUERY, {"efoId": efo_id, "page": {"index": page_index, "size": PAGE_SIZE}})
            disease_data = data.get("disease", {})
            if not disease_data:
                log.warning("  No data for %s", efo_id)
                break

            assoc = disease_data.get("associatedTargets", {})
            if total is None:
                total = assoc.get("count", 0)
                log.info("  Total targets for %s: %d", disease_label, total)

            rows = assoc.get("rows", [])
            if not rows:
                break

            d_id = disease_data.get("id", "")
            d_name = disease_data.get("name", "")

            for row in rows:
                target = row.get("target", {})
                score = row.get("score", 0)
                dt_scores = row.get("datatypeScores", []) or [{"id": "", "score": ""}]
                for dt in dt_scores:
                    all_rows.append({
                        "disease_id": d_id,
                        "disease_name": d_name,
                        "target_id": target.get("id", ""),
                        "target_symbol": target.get("approvedSymbol", ""),
                        "target_name": target.get("approvedName", ""),
                        "overall_score": score,
                        "datatype": dt.get("id", ""),
                        "datatype_score": dt.get("score", ""),
                    })

            fetched = (page_index + 1) * PAGE_SIZE
            log.info("  Page %d (%d/%s)", page_index, min(fetched, total or fetched), total)
            if fetched >= (total or 0) or len(rows) < PAGE_SIZE:
                break
            page_index += 1
            time.sleep(REQUEST_DELAY)

        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(all_rows)

        log.info("  Saved %d rows for %s", len(all_rows), disease_label)


# ---------------------------------------------------------------------------
# 3. Target Tractability
# ---------------------------------------------------------------------------
TARGET_TRACTABILITY_QUERY = """
query TargetTractability($ensemblId: String!) {
  target(ensemblId: $ensemblId) {
    id
    approvedSymbol
    approvedName
    tractability {
      modality
      value
    }
  }
}
"""


def scrape_target_tractability():
    folder = "Target_Tractability"
    log.info("Scraping target tractability for top disease-associated targets...")

    # Collect unique target IDs with their best score from disease association CSVs
    target_scores = {}
    assoc_dir = BASE_DIR / "Disease_Associations"
    for csv_file in assoc_dir.glob("*_targets.csv"):
        try:
            with open(csv_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    tid = row.get("target_id", "").strip()
                    score = float(row.get("overall_score", 0) or 0)
                    if tid:
                        target_scores[tid] = max(target_scores.get(tid, 0), score)
        except Exception as exc:
            log.warning("Could not read %s: %s", csv_file, exc)

    if not target_scores:
        log.warning("No target IDs found. Run disease associations first.")
        return

    # Limit to top 1000 targets by score to keep runtime reasonable
    target_list = sorted(target_scores, key=lambda t: target_scores[t], reverse=True)[:1000]
    log.info("Fetching tractability for %d unique targets...", len(target_list))

    csv_path = BASE_DIR / folder / "target_tractability.csv"
    fields = [
        "target_id", "target_symbol", "target_name",
        "modality", "value",
    ]
    all_rows = []

    for i, tid in enumerate(target_list):
        if i > 0 and i % 100 == 0:
            log.info("  Tractability progress: %d/%d", i, len(target_list))
        try:
            data = graphql(TARGET_TRACTABILITY_QUERY, {"ensemblId": tid})
            tgt = data.get("target", {})
            if not tgt:
                continue
            tracts = tgt.get("tractability", [])
            if not tracts:
                continue
            for t in tracts:
                all_rows.append({
                    "target_id": tgt.get("id", ""),
                    "target_symbol": tgt.get("approvedSymbol", ""),
                    "target_name": tgt.get("approvedName", ""),
                    "modality": t.get("modality", ""),
                    "value": t.get("value", ""),
                })
        except Exception as exc:
            log.warning("Failed tractability for %s: %s", tid, exc)
        time.sleep(REQUEST_DELAY)

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(all_rows)

    log.info("Saved %d tractability rows to %s", len(all_rows), csv_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    log.info("=" * 60)
    log.info("Open Targets Platform Scraper - Starting")
    log.info("=" * 60)

    ensure_dirs()

    scrape_drugs()
    scrape_disease_associations()
    scrape_target_tractability()

    log.info("All scraping complete.")

    # Post-processing
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from process_files import process_scraper
    log.info("Post-processing downloaded files with MiniMax...")
    process_scraper(BASE_DIR)


if __name__ == "__main__":
    main()
