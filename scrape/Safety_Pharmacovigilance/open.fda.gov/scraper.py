#!/usr/bin/env python3
"""
OpenFDA Scraper — Safety & Pharmacovigilance
Scrapes drug adverse events (FAERS), drug labels, recalls/enforcement,
NDC directory, and FDA drug approvals via the openFDA public API.

API docs: https://open.fda.gov/apis/
No authentication required. Rate limit: 240 requests/minute without API key.
"""

import csv
import json
import logging
import sys
import time
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
API_BASE = "https://api.fda.gov"
REQUEST_DELAY = 1.5
MAX_RETRIES = 3
PAGE_SIZE = 100
MAX_SKIP = 25000

FOLDERS = [
    "Adverse_Events",
    "Adverse_Events_Counts",
    "Drug_Labels",
    "Drug_Recalls",
    "Drug_Approvals",
    "NDC_Directory",
]

log_fmt = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=log_fmt,
                    handlers=[logging.StreamHandler(),
                              logging.FileHandler(BASE_DIR / "scraper.log", encoding="utf-8")])
log = logging.getLogger("openfda")

session = requests.Session()


def api_get(endpoint: str, params: dict | None = None) -> dict:
    url = f"{API_BASE}{endpoint}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, params=params, timeout=60)
            if resp.status_code == 404:
                return {"results": []}
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            log.warning("Request %s attempt %d/%d failed: %s", endpoint, attempt, MAX_RETRIES, exc)
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
            else:
                log.error("Failed after %d attempts: %s", MAX_RETRIES, endpoint)
                return {"results": []}
    return {"results": []}


def write_csv(rows: list[dict], path: Path, fields: list[str] | None = None):
    if not rows:
        log.warning("No data to write for %s", path.name)
        return
    if not fields:
        fields = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    log.info("Saved %d rows -> %s", len(rows), path)


def paginate(endpoint: str, search: str | None = None, max_records: int = 25000) -> list[dict]:
    results = []
    skip = 0
    while skip < min(max_records, MAX_SKIP):
        params = {"limit": PAGE_SIZE, "skip": skip}
        if search:
            params["search"] = search
        data = api_get(endpoint, params)
        batch = data.get("results", [])
        if not batch:
            break
        results.extend(batch)
        total = data.get("meta", {}).get("results", {}).get("total", 0)
        skip += PAGE_SIZE
        if skip >= total:
            break
        time.sleep(REQUEST_DELAY)
    return results


def count_endpoint(endpoint: str, count_field: str, search: str | None = None,
                   limit: int = 1000) -> list[dict]:
    params = {"count": count_field, "limit": limit}
    if search:
        params["search"] = search
    data = api_get(endpoint, params)
    return data.get("results", [])


def ensure_dirs():
    for folder in FOLDERS:
        (BASE_DIR / folder).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 1. Adverse Events — Aggregated counts (most useful for analysis)
# ---------------------------------------------------------------------------
def scrape_adverse_event_counts():
    folder = BASE_DIR / "Adverse_Events_Counts"
    log.info("Scraping FAERS adverse event counts...")

    count_queries = [
        ("top_reactions", "patient.reaction.reactionmeddrapt.exact", None),
        ("top_drugs_reported", "patient.drug.openfda.generic_name.exact", None),
        ("top_brands_reported", "patient.drug.openfda.brand_name.exact", None),
        ("reports_by_country", "occurcountry.exact", None),
        ("reports_by_age_group", "patient.patientonsetageunit", None),
        ("reports_by_sex", "patient.patientsex", None),
        ("reports_by_year", "receiptdateformat", None),
        ("serious_outcomes", "serious", None),
        ("reporter_qualification", "primarysource.qualification", None),
        ("drug_characterization", "patient.drug.drugcharacterization", None),
        ("top_indications", "patient.drug.drugindication.exact", None),
        ("top_routes", "patient.drug.openfda.route.exact", None),
        ("top_manufacturers", "patient.drug.openfda.manufacturer_name.exact", None),
        ("top_pharm_classes", "patient.drug.openfda.pharm_class_epc.exact", None),
        ("reaction_outcomes", "patient.reaction.reactionoutcome", None),
    ]

    for name, count_field, search in count_queries:
        log.info("  Counting: %s", name)
        results = count_endpoint("/drug/event.json", count_field, search)
        if results:
            rows = [{"term": r.get("term", ""), "count": r.get("count", 0)} for r in results]
            write_csv(rows, folder / f"{name}.csv", ["term", "count"])
        time.sleep(2)


# ---------------------------------------------------------------------------
# 2. Adverse Events — Recent detailed reports (paginated, max 25K)
# ---------------------------------------------------------------------------
def scrape_adverse_events_detail():
    folder = BASE_DIR / "Adverse_Events"
    log.info("Scraping FAERS detailed adverse event reports (recent)...")

    fields = [
        "safetyreportid", "receiptdate", "serious", "seriousnessdeath",
        "seriousnesshospitalization", "seriousnesslifethreatening",
        "seriousnessdisabling", "occurcountry", "reporttype",
        "drug_name", "drug_brand", "drug_generic", "drug_substance",
        "drug_indication", "drug_route", "drug_characterization",
        "drug_pharm_class", "drug_manufacturer",
        "reaction", "reaction_outcome",
        "patient_sex", "patient_age", "patient_age_unit", "patient_weight",
    ]

    date_ranges = [
        ("2020", "20200101", "20201231"),
        ("2021", "20210101", "20211231"),
        ("2022", "20220101", "20221231"),
        ("2023", "20230101", "20231231"),
        ("2024Q1", "20240101", "20240331"),
        ("2024Q2", "20240401", "20240630"),
        ("2024Q3", "20240701", "20240930"),
        ("2024Q4", "20241001", "20241231"),
        ("2025Q1", "20250101", "20250331"),
        ("2025Q2", "20250401", "20250630"),
        ("2025Q3_Q4", "20250701", "20251231"),
        ("2026Q1", "20260101", "20260331"),
    ]

    for period, start, end in date_ranges:
        csv_path = folder / f"faers_{period}.csv"
        if csv_path.exists() and csv_path.stat().st_size > 100:
            log.info("  Skipping FAERS %s (already exists)", period)
            continue
        log.info("  Fetching FAERS %s (%s - %s)...", period, start, end)

        search = f"receiptdate:[{start} TO {end}]"
        row_count = 0
        skip = 0

        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()

            while skip < MAX_SKIP:
                params = {"limit": PAGE_SIZE, "skip": skip, "search": search}
                data = api_get("/drug/event.json", params)
                batch = data.get("results", [])
                if not batch:
                    break
                total = data.get("meta", {}).get("results", {}).get("total", 0)

                for report in batch:
                    patient = report.get("patient", {})
                    drugs = patient.get("drug", [])
                    reactions = patient.get("reaction", [])
                    reaction_str = "; ".join(r.get("reactionmeddrapt", "") for r in reactions)
                    outcome_str = "; ".join(str(r.get("reactionoutcome", "")) for r in reactions)

                    for drug in drugs:
                        openfda = drug.get("openfda", {})
                        writer.writerow({
                            "safetyreportid": report.get("safetyreportid", ""),
                            "receiptdate": report.get("receiptdate", ""),
                            "serious": report.get("serious", ""),
                            "seriousnessdeath": report.get("seriousnessdeath", ""),
                            "seriousnesshospitalization": report.get("seriousnesshospitalization", ""),
                            "seriousnesslifethreatening": report.get("seriousnesslifethreatening", ""),
                            "seriousnessdisabling": report.get("seriousnessdisabling", ""),
                            "occurcountry": report.get("occurcountry", ""),
                            "reporttype": report.get("reporttype", ""),
                            "drug_name": drug.get("medicinalproduct", ""),
                            "drug_brand": "; ".join(openfda.get("brand_name", [])),
                            "drug_generic": "; ".join(openfda.get("generic_name", [])),
                            "drug_substance": "; ".join(openfda.get("substance_name", [])),
                            "drug_indication": drug.get("drugindication", ""),
                            "drug_route": "; ".join(openfda.get("route", [])),
                            "drug_characterization": drug.get("drugcharacterization", ""),
                            "drug_pharm_class": "; ".join(openfda.get("pharm_class_epc", [])),
                            "drug_manufacturer": "; ".join(openfda.get("manufacturer_name", [])),
                            "reaction": reaction_str,
                            "reaction_outcome": outcome_str,
                            "patient_sex": patient.get("patientsex", ""),
                            "patient_age": patient.get("patientonsetage", ""),
                            "patient_age_unit": patient.get("patientonsetageunit", ""),
                            "patient_weight": patient.get("patientweight", ""),
                        })
                        row_count += 1

                skip += PAGE_SIZE
                if skip >= total:
                    break
                time.sleep(REQUEST_DELAY)

        if row_count == 0:
            csv_path.unlink(missing_ok=True)
            log.warning("  No data for %s", period)
        else:
            log.info("Saved %d rows -> %s", row_count, csv_path)
        time.sleep(REQUEST_DELAY)


# ---------------------------------------------------------------------------
# 3. Drug Labels (full label info with openfda metadata)
# ---------------------------------------------------------------------------
def scrape_drug_labels():
    folder = BASE_DIR / "Drug_Labels"
    log.info("Scraping FDA drug labels...")

    fields = [
        "application_number", "brand_name", "generic_name", "substance_name",
        "manufacturer_name", "product_type", "route", "pharm_class_epc",
        "pharm_class_moa", "dosage_form",
        "indications_and_usage", "warnings", "adverse_reactions",
        "drug_interactions", "contraindications",
        "boxed_warning", "pregnancy",
    ]

    raw = paginate("/drug/label.json", search="openfda.brand_name:*", max_records=25000)
    log.info("  Fetched %d drug labels", len(raw))

    rows = []
    for label in raw:
        openfda = label.get("openfda", {})
        rows.append({
            "application_number": "; ".join(openfda.get("application_number", [])),
            "brand_name": "; ".join(openfda.get("brand_name", [])),
            "generic_name": "; ".join(openfda.get("generic_name", [])),
            "substance_name": "; ".join(openfda.get("substance_name", [])),
            "manufacturer_name": "; ".join(openfda.get("manufacturer_name", [])),
            "product_type": "; ".join(openfda.get("product_type", [])),
            "route": "; ".join(openfda.get("route", [])),
            "pharm_class_epc": "; ".join(openfda.get("pharm_class_epc", [])),
            "pharm_class_moa": "; ".join(openfda.get("pharm_class_moa", [])),
            "dosage_form": "; ".join(openfda.get("dosage_form", [])),
            "indications_and_usage": (label.get("indications_and_usage", [""])[0])[:500],
            "warnings": (label.get("warnings", [""])[0])[:500],
            "adverse_reactions": (label.get("adverse_reactions", [""])[0])[:500],
            "drug_interactions": (label.get("drug_interactions", [""])[0])[:500],
            "contraindications": (label.get("contraindications", [""])[0])[:500],
            "boxed_warning": (label.get("boxed_warning", [""])[0])[:500],
            "pregnancy": (label.get("pregnancy", [""])[0])[:500],
        })

    write_csv(rows, folder / "drug_labels.csv", fields)


# ---------------------------------------------------------------------------
# 4. Drug Recalls / Enforcement
# ---------------------------------------------------------------------------
def scrape_drug_recalls():
    folder = BASE_DIR / "Drug_Recalls"
    log.info("Scraping FDA drug recalls/enforcement...")

    fields = [
        "recall_number", "event_id", "status", "classification",
        "recalling_firm", "city", "state", "country",
        "product_description", "reason_for_recall", "product_quantity",
        "distribution_pattern", "voluntary_mandated",
        "recall_initiation_date", "center_classification_date",
        "termination_date", "report_date",
        "brand_name", "generic_name", "manufacturer_name",
    ]

    raw = paginate("/drug/enforcement.json", max_records=25000)
    log.info("  Fetched %d recall records", len(raw))

    rows = []
    for rec in raw:
        openfda = rec.get("openfda", {})
        rows.append({
            "recall_number": rec.get("recall_number", ""),
            "event_id": rec.get("event_id", ""),
            "status": rec.get("status", ""),
            "classification": rec.get("classification", ""),
            "recalling_firm": rec.get("recalling_firm", ""),
            "city": rec.get("city", ""),
            "state": rec.get("state", ""),
            "country": rec.get("country", ""),
            "product_description": rec.get("product_description", ""),
            "reason_for_recall": rec.get("reason_for_recall", ""),
            "product_quantity": rec.get("product_quantity", ""),
            "distribution_pattern": rec.get("distribution_pattern", ""),
            "voluntary_mandated": rec.get("voluntary_mandated", ""),
            "recall_initiation_date": rec.get("recall_initiation_date", ""),
            "center_classification_date": rec.get("center_classification_date", ""),
            "termination_date": rec.get("termination_date", ""),
            "report_date": rec.get("report_date", ""),
            "brand_name": "; ".join(openfda.get("brand_name", [])),
            "generic_name": "; ".join(openfda.get("generic_name", [])),
            "manufacturer_name": "; ".join(openfda.get("manufacturer_name", [])),
        })

    write_csv(rows, folder / "drug_recalls.csv", fields)


# ---------------------------------------------------------------------------
# 5. FDA Drug Approvals (Drugs@FDA)
# ---------------------------------------------------------------------------
def scrape_drug_approvals():
    folder = BASE_DIR / "Drug_Approvals"
    log.info("Scraping FDA drug approvals...")

    fields = [
        "application_number", "sponsor_name",
        "product_number", "brand_name", "active_ingredients",
        "dosage_form", "route", "marketing_status",
        "submission_type", "submission_number",
        "submission_status", "submission_status_date",
    ]

    raw = paginate("/drug/drugsfda.json", max_records=25000)
    log.info("  Fetched %d approval records", len(raw))

    rows = []
    for app in raw:
        app_num = app.get("application_number", "")
        sponsor = app.get("sponsor_name", "")
        products = app.get("products", [])
        submissions = app.get("submissions", [])

        for prod in (products or [{}]):
            ingredients = prod.get("active_ingredients", [])
            ingredient_str = "; ".join(
                f"{i.get('name', '')} {i.get('strength', '')}".strip()
                for i in ingredients
            ) if ingredients else ""

            sub_info = {}
            if submissions:
                latest = submissions[0]
                sub_info = {
                    "submission_type": latest.get("submission_type", ""),
                    "submission_number": latest.get("submission_number", ""),
                    "submission_status": latest.get("submission_status", ""),
                    "submission_status_date": latest.get("submission_status_date", ""),
                }

            rows.append({
                "application_number": app_num,
                "sponsor_name": sponsor,
                "product_number": prod.get("product_number", ""),
                "brand_name": prod.get("brand_name", ""),
                "active_ingredients": ingredient_str,
                "dosage_form": prod.get("dosage_form", ""),
                "route": prod.get("route", ""),
                "marketing_status": prod.get("marketing_status", ""),
                **sub_info,
            })

    write_csv(rows, folder / "drug_approvals.csv", fields)


# ---------------------------------------------------------------------------
# 6. NDC Directory
# ---------------------------------------------------------------------------
def scrape_ndc():
    folder = BASE_DIR / "NDC_Directory"
    log.info("Scraping FDA NDC directory...")

    fields = [
        "product_ndc", "brand_name", "generic_name", "labeler_name",
        "active_ingredients", "dosage_form", "route",
        "marketing_category", "marketing_start_date",
        "product_type", "pharm_class",
    ]

    raw = paginate("/drug/ndc.json", max_records=25000)
    log.info("  Fetched %d NDC records", len(raw))

    rows = []
    for ndc in raw:
        openfda = ndc.get("openfda", {})
        ingredients = ndc.get("active_ingredients", [])
        ingredient_str = "; ".join(
            f"{i.get('name', '')} {i.get('strength', '')}".strip()
            for i in ingredients
        ) if ingredients else ""

        rows.append({
            "product_ndc": ndc.get("product_ndc", ""),
            "brand_name": ndc.get("brand_name", ""),
            "generic_name": ndc.get("generic_name", ""),
            "labeler_name": ndc.get("labeler_name", ""),
            "active_ingredients": ingredient_str,
            "dosage_form": ndc.get("dosage_form", ""),
            "route": "; ".join(ndc.get("route", [])) if isinstance(ndc.get("route"), list) else ndc.get("route", ""),
            "marketing_category": ndc.get("marketing_category", ""),
            "marketing_start_date": ndc.get("marketing_start_date", ""),
            "product_type": ndc.get("product_type", ""),
            "pharm_class": "; ".join(openfda.get("pharm_class_epc", [])),
        })

    write_csv(rows, folder / "ndc_directory.csv", fields)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    log.info("=" * 60)
    log.info("OpenFDA Scraper — Safety & Pharmacovigilance")
    log.info("=" * 60)

    ensure_dirs()

    scrape_adverse_event_counts()
    scrape_adverse_events_detail()
    scrape_drug_labels()
    scrape_drug_recalls()
    scrape_drug_approvals()
    scrape_ndc()

    # Post-processing
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    log.info("=" * 60)
    log.info("OpenFDA scraper complete")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
