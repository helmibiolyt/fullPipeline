#!/usr/bin/env python3
"""
cBioPortal scraper — downloads cancer types, studies, clinical data,
mutation data, and treatment data via the cBioPortal REST API.
All endpoints are free and require no authentication.
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
BASE_DIR = Path(__file__).parent
API_BASE = "https://www.cbioportal.org/api"

FOLDERS = {
    "cancer_types": BASE_DIR / "Cancer_Types",
    "studies":      BASE_DIR / "Studies",
    "clinical":     BASE_DIR / "Clinical_Data",
    "mutations":    BASE_DIR / "Mutations",
    "treatments":   BASE_DIR / "Treatments",
}

KEY_STUDIES = [
    "brca_tcga",   # Breast Cancer
    "luad_tcga",   # Lung Adenocarcinoma
    "coadread_tcga",  # Colorectal
    "prad_tcga",   # Prostate
    "paad_tcga",   # Pancreatic
    "laml_tcga",   # Leukemia
    "lihc_tcga",   # Liver
    "ov_tcga",     # Ovarian
    "stad_tcga",   # Stomach
    "skcm_tcga",   # Melanoma
]

KEY_GENES = [
    "BRAF", "KRAS", "TP53", "EGFR", "PIK3CA",
    "BRCA1", "BRCA2", "ALK", "ERBB2", "PTEN",
]

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

REQUEST_DELAY = 0.5
MAX_RETRIES = 3
RETRY_BACKOFF = 2

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log = logging.getLogger("cbioportal_scraper")
log.setLevel(logging.DEBUG)
fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.INFO)
ch.setFormatter(fmt)
log.addHandler(ch)

fh = logging.FileHandler(BASE_DIR / "scraper.log", mode="w", encoding="utf-8")
fh.setLevel(logging.DEBUG)
fh.setFormatter(fmt)
log.addHandler(fh)

# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def api_get(endpoint, params=None):
    """GET request with retries."""
    url = f"{API_BASE}{endpoint}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = SESSION.get(url, params=params, timeout=120)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.warning(f"GET {endpoint} attempt {attempt} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
            else:
                log.error(f"GET {endpoint} failed after {MAX_RETRIES} attempts")
                return None
    return None


def api_post(endpoint, json_body):
    """POST request with retries."""
    url = f"{API_BASE}{endpoint}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = SESSION.post(url, json=json_body, timeout=120)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.warning(f"POST {endpoint} attempt {attempt} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
            else:
                log.error(f"POST {endpoint} failed after {MAX_RETRIES} attempts")
                return None
    return None


def save_csv(rows, filepath, fieldnames=None):
    """Save a list of dicts as CSV."""
    if not rows:
        log.warning(f"No data to save for {filepath}")
        return
    if fieldnames is None:
        fieldnames = list(rows[0].keys())
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    log.info(f"Saved {len(rows)} rows -> {filepath}")


def flatten(obj, prefix=""):
    """Flatten nested dict/list to a single-level dict."""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}{k}" if not prefix else f"{prefix}_{k}"
            if isinstance(v, (dict, list)):
                out.update(flatten(v, key))
            else:
                out[key] = v
    elif isinstance(obj, list):
        out[prefix] = json.dumps(v) if isinstance(v, (dict, list)) else str(obj)
    else:
        out[prefix] = obj
    return out


# ---------------------------------------------------------------------------
# 1. Cancer Types
# ---------------------------------------------------------------------------
def scrape_cancer_types():
    folder = FOLDERS["cancer_types"]
    folder.mkdir(parents=True, exist_ok=True)
    log.info("Fetching cancer types...")
    data = api_get("/cancer-types")
    if not data:
        return

    rows = []
    for item in data:
        rows.append({
            "cancer_type_id": item.get("cancerTypeId", ""),
            "name": item.get("name", ""),
            "short_name": item.get("shortName", ""),
            "parent": item.get("parent", ""),
            "dedicated_color": item.get("dedicatedColor", ""),
        })

    save_csv(rows, folder / "cancer_types.csv",
             ["cancer_type_id", "name", "short_name", "parent", "dedicated_color"])
    time.sleep(REQUEST_DELAY)


# ---------------------------------------------------------------------------
# 2. All Studies
# ---------------------------------------------------------------------------
def scrape_studies():
    folder = FOLDERS["studies"]
    folder.mkdir(parents=True, exist_ok=True)
    log.info("Fetching all studies...")
    data = api_get("/studies")
    if not data:
        return

    fields = [
        "studyId", "name", "description", "cancerTypeId", "pmid",
        "citation", "groups", "status", "importDate", "allSampleCount",
    ]
    rows = []
    for item in data:
        row = {}
        for f in fields:
            val = item.get(f, "")
            if isinstance(val, list):
                val = "; ".join(str(v) for v in val)
            row[f] = val
        # Also grab cancerType nested info
        ct = item.get("cancerType", {})
        if ct:
            row["cancerTypeName"] = ct.get("name", "")
        rows.append(row)

    all_fields = fields + ["cancerTypeName"]
    save_csv(rows, folder / "all_studies.csv", all_fields)
    time.sleep(REQUEST_DELAY)


# ---------------------------------------------------------------------------
# 3. Clinical Data per study
# ---------------------------------------------------------------------------
def scrape_clinical_data():
    folder = FOLDERS["clinical"]
    folder.mkdir(parents=True, exist_ok=True)

    for study_id in KEY_STUDIES:
        log.info(f"Fetching clinical data for {study_id}...")

        # Fetch patient clinical data
        data = api_get(
            f"/studies/{study_id}/clinical-data",
            params={"clinicalDataType": "PATIENT", "projection": "DETAILED"},
        )
        if not data:
            log.warning(f"No clinical data returned for {study_id}")
            time.sleep(REQUEST_DELAY)
            continue

        # Pivot: each row is one attribute for one patient.
        # We pivot so each patient is one row with attribute columns.
        patients = {}
        attr_names = set()
        for item in data:
            pid = item.get("patientId", "")
            attr_id = item.get("clinicalAttributeId", "")
            value = item.get("value", "")
            attr_names.add(attr_id)
            if pid not in patients:
                patients[pid] = {"patientId": pid, "studyId": study_id}
            patients[pid][attr_id] = value

        rows = list(patients.values())
        if rows:
            all_cols = ["patientId", "studyId"] + sorted(attr_names)
            # Fill missing attrs with empty string
            for row in rows:
                for col in all_cols:
                    row.setdefault(col, "")
            save_csv(rows, folder / f"{study_id}_clinical.csv", all_cols)

        time.sleep(REQUEST_DELAY)


# ---------------------------------------------------------------------------
# 4. Mutations for key genes
# ---------------------------------------------------------------------------
def scrape_mutations():
    folder = FOLDERS["mutations"]
    folder.mkdir(parents=True, exist_ok=True)

    # First, resolve gene entrezGeneIds
    log.info("Fetching gene metadata...")
    gene_map = {}  # symbol -> entrezGeneId
    for gene in KEY_GENES:
        data = api_get(f"/genes/{gene}")
        if data and "entrezGeneId" in data:
            gene_map[gene] = data["entrezGeneId"]
            log.debug(f"Gene {gene} -> entrezGeneId {data['entrezGeneId']}")
        time.sleep(REQUEST_DELAY)

    if not gene_map:
        log.error("Could not resolve any gene IDs, skipping mutations.")
        return

    entrez_ids = list(gene_map.values())

    for study_id in KEY_STUDIES:
        log.info(f"Fetching molecular profiles for {study_id}...")
        profiles = api_get(f"/studies/{study_id}/molecular-profiles")
        if not profiles:
            time.sleep(REQUEST_DELAY)
            continue

        # Find mutation profile (molecularAlterationType == MUTATION_EXTENDED)
        mut_profile_id = None
        for p in profiles:
            if p.get("molecularAlterationType") == "MUTATION_EXTENDED":
                mut_profile_id = p.get("molecularProfileId")
                break

        if not mut_profile_id:
            log.warning(f"No mutation profile found for {study_id}")
            time.sleep(REQUEST_DELAY)
            continue

        # Get all sample IDs for this study
        log.info(f"Fetching sample list for {study_id}...")
        samples = api_get(f"/studies/{study_id}/samples", params={"projection": "ID"})
        if not samples:
            time.sleep(REQUEST_DELAY)
            continue

        sample_ids = [s.get("sampleId") for s in samples if s.get("sampleId")]
        time.sleep(REQUEST_DELAY)

        # Fetch mutations in batches (API may limit)
        log.info(f"Fetching mutations for {study_id} ({mut_profile_id}), "
                 f"{len(sample_ids)} samples, {len(entrez_ids)} genes...")

        # Use the POST endpoint scoped to the molecular profile
        body = {
            "sampleListId": f"{study_id}_all",
            "entrezGeneIds": entrez_ids,
        }

        # The correct endpoint includes the molecularProfileId in the URL path
        data = api_post(
            f"/molecular-profiles/{mut_profile_id}/mutations/fetch?projection=DETAILED",
            body,
        )

        if data is None:
            # Fallback: use sampleIds instead of sampleListId
            body = {
                "sampleIds": sample_ids[:5000],
                "entrezGeneIds": entrez_ids,
            }
            data = api_post(
                f"/molecular-profiles/{mut_profile_id}/mutations/fetch?projection=DETAILED",
                body,
            )

        if not data:
            log.warning(f"No mutation data for {study_id}")
            time.sleep(REQUEST_DELAY)
            continue

        # Flatten mutation records
        fields = [
            "sampleId", "patientId", "studyId", "molecularProfileId",
            "entrezGeneId", "gene_hugoGeneSymbol",
            "proteinChange", "mutationType", "functionalImpactScore",
            "fisValue", "linkXvar", "linkPdb", "linkMsa",
            "ncbiBuild", "chr", "startPosition", "endPosition",
            "referenceAllele", "variantAllele", "variantType",
            "keyword", "mutationStatus", "validationStatus",
            "center", "tumorAltCount", "tumorRefCount",
            "normalAltCount", "normalRefCount",
        ]
        rows = []
        for item in data:
            row = {}
            gene_info = item.get("gene", {})
            for f in fields:
                if f.startswith("gene_"):
                    row[f] = gene_info.get(f.replace("gene_", ""), "")
                else:
                    row[f] = item.get(f, "")
            rows.append(row)

        save_csv(rows, folder / f"{study_id}_mutations.csv", fields)
        time.sleep(REQUEST_DELAY)


# ---------------------------------------------------------------------------
# 5. Treatment Data
# ---------------------------------------------------------------------------
def scrape_treatments():
    folder = FOLDERS["treatments"]
    folder.mkdir(parents=True, exist_ok=True)
    log.info("Fetching treatment data for key studies...")

    # POST /api/treatments/patient
    body = {
        "studyIds": KEY_STUDIES,
    }
    data = api_post("/treatments/patient", body)

    if not data:
        # Try alternative: fetch per study via clinical attributes
        log.info("Treatment endpoint returned no data, trying clinical data approach...")
        all_treatment_rows = []
        for study_id in KEY_STUDIES:
            log.info(f"Fetching sample clinical data for treatments in {study_id}...")
            cdata = api_get(
                f"/studies/{study_id}/clinical-data",
                params={"clinicalDataType": "SAMPLE", "projection": "DETAILED"},
            )
            if cdata:
                for item in cdata:
                    attr_id = item.get("clinicalAttributeId", "").upper()
                    if any(kw in attr_id for kw in [
                        "TREATMENT", "DRUG", "THERAPY", "CHEMO", "REGIMEN",
                        "AGENT", "RADIATION"
                    ]):
                        all_treatment_rows.append({
                            "studyId": study_id,
                            "patientId": item.get("patientId", ""),
                            "sampleId": item.get("sampleId", ""),
                            "attributeId": item.get("clinicalAttributeId", ""),
                            "attributeName": item.get("clinicalAttribute", {}).get(
                                "displayName", item.get("clinicalAttributeId", "")
                            ) if isinstance(item.get("clinicalAttribute"), dict)
                            else item.get("clinicalAttributeId", ""),
                            "value": item.get("value", ""),
                        })
            time.sleep(REQUEST_DELAY)

        if all_treatment_rows:
            save_csv(
                all_treatment_rows,
                folder / "treatments_from_clinical.csv",
                ["studyId", "patientId", "sampleId", "attributeId",
                 "attributeName", "value"],
            )
        else:
            log.warning("No treatment data found via clinical attributes either.")
        return

    # If the endpoint returned data directly
    rows = []
    for item in data:
        row = {}
        if isinstance(item, dict):
            row = {
                "treatment": item.get("treatment", ""),
                "studyId": item.get("studyId", ""),
                "patientId": item.get("patientId", ""),
                "start": item.get("start", ""),
                "stop": item.get("stop", ""),
                "count": item.get("count", ""),
            }
            # Flatten any extra keys
            for k, v in item.items():
                if k not in row:
                    row[k] = json.dumps(v) if isinstance(v, (dict, list)) else v
        rows.append(row)

    if rows:
        all_keys = list(rows[0].keys())
        save_csv(rows, folder / "treatments.csv", all_keys)
    time.sleep(REQUEST_DELAY)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    log.info("=" * 60)
    log.info("cBioPortal Scraper")
    log.info("=" * 60)

    # Create folders
    for folder in FOLDERS.values():
        folder.mkdir(parents=True, exist_ok=True)

    # Run scrapers
    scrape_cancer_types()
    scrape_studies()
    scrape_clinical_data()
    scrape_mutations()
    scrape_treatments()

    # Summary
    log.info("")
    log.info("=" * 60)
    log.info("Summary")
    log.info("=" * 60)
    for key, folder in FOLDERS.items():
        csvs = list(folder.glob("*.csv"))
        log.info(f"  {folder.name}: {len(csvs)} CSV files")

    # Post-process
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
if __name__ == "__main__":
    main()
