"""
rxnorm_scraper.py
=================
1. Fetches ALL drug/substance concepts (TTY=IN and TTY=MIN) from the RxNorm API.
2. Saves every concept name in  Drug & Substance Reference/rxnorm_data/drug_names.txt
3. For each concept, retrieves via the API:
       query, rxcui, name, tty, synonym, ingredient_rxcui, ingredient_name
4. Writes results to  Drug & Substance Reference/rxnorm_data/rxnorm_drugs.csv

API base: https://rxnav.nlm.nih.gov/REST

Rate-limiting: The script automatically throttles requests to avoid overloading
the public NIH API.  A configurable delay (DEFAULT_DELAY_S) is applied between
every call.  A fast-path works directly from the rxcui/name pairs returned by
/allconcepts (they already give rxcui, name, tty), making only *two* additional
calls per concept:
   - /rxcui/{rxcui}/allrelated      -> synonyms (Brand Names, TTY=BN, pipe-sep)
   - /rxcui/{rxcui}/related?tty=IN -> ingredient rxcui + name
"""

import csv
import json
import logging
import os
import sys
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_URL      = "https://rxnav.nlm.nih.gov/REST"
TTY_FILTER    = "IN MIN"         # term-types to harvest from /allconcepts
DEFAULT_DELAY_S = 0.15           # seconds between API calls (be a good citizen)
REQUEST_TIMEOUT = 30             # seconds per request
MAX_RETRIES     = 5
# Resolve output paths relative to this script, never the current working dir.
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "rxnorm_data"
NAMES_FILE = OUTPUT_DIR / "drug_names.txt"
CSV_FILE   = OUTPUT_DIR / "rxnorm_drugs.csv"
CSV_FIELDS = [
    "query", "rxcui", "name", "tty",
    "synonym", "ingredient_rxcui", "ingredient_name",
]
LOG_FILE = OUTPUT_DIR / "rxnorm_scraper.log"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HTTP session with automatic retry / back-off
# ---------------------------------------------------------------------------
def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=MAX_RETRIES,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://",  adapter)
    session.headers.update({"Accept": "application/json"})
    return session


SESSION = _build_session()


def _get(url: str, params: dict = None) -> dict | None:
    """GET a JSON endpoint and return the parsed dict, or None on error."""
    try:
        resp = SESSION.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as e:
        log.warning(f"HTTP error for {url}: {e}")
    except requests.exceptions.ConnectionError as e:
        log.warning(f"Connection error for {url}: {e}")
    except requests.exceptions.Timeout:
        log.warning(f"Timeout for {url}")
    except json.JSONDecodeError:
        log.warning(f"JSON decode error for {url}")
    return None


# ---------------------------------------------------------------------------
# Step 1 - Fetch all concepts from /allconcepts
# ---------------------------------------------------------------------------
def fetch_all_concepts() -> list:
    """
    Call /allconcepts?tty=IN+MIN and return a list of
    {rxcui, name, tty} dicts.
    """
    url = f"{BASE_URL}/allconcepts.json"
    log.info(f"Fetching all concepts from {url} (tty={TTY_FILTER}) ...")
    data = _get(url, params={"tty": TTY_FILTER})
    if not data:
        log.error("Failed to fetch /allconcepts — aborting.")
        sys.exit(1)

    concepts = data.get("minConceptGroup", {}).get("minConcept", [])
    log.info(f"  -> {len(concepts):,} concepts retrieved.")
    return concepts


def save_names(concepts: list) -> None:
    """Write one drug name per line to NAMES_FILE."""
    with open(NAMES_FILE, "w", encoding="utf-8") as fh:
        for c in concepts:
            fh.write(c["name"] + "\n")
    log.info(f"Names saved to {NAMES_FILE}  ({len(concepts):,} lines)")


# ---------------------------------------------------------------------------
# Step 2 - Synonyms via /allrelated  (Brand Names = TTY=BN)
# ---------------------------------------------------------------------------
def fetch_synonyms(rxcui: str) -> str:
    """
    Return a pipe-separated string of brand-name synonyms for the given rxcui.

    RxNorm encodes brand-name synonyms as TTY=BN concepts.  These are
    available via /rxcui/{rxcui}/allrelated.json  under the 'BN'
    conceptGroup.  For example metformin (6809) returns: Fortamet,
    Glucophage, Glumetza, Riomet.

    Falls back to an empty string on any error.
    """
    url = f"{BASE_URL}/rxcui/{rxcui}/allrelated.json"
    time.sleep(DEFAULT_DELAY_S)
    data = _get(url)
    if not data:
        return ""

    groups = data.get("allRelatedGroup", {}).get("conceptGroup", [])
    synonyms = [
        cp.get("name", "")
        for g in groups if g.get("tty") == "BN"
        for cp in g.get("conceptProperties", [])
        if cp.get("name")
    ]
    # Deduplicate while preserving order
    seen: set = set()
    unique: list = []
    for s in synonyms:
        if s not in seen:
            seen.add(s)
            unique.append(s)

    return "|".join(unique)


# ---------------------------------------------------------------------------
# Step 3 - Ingredient lookup via /related
# ---------------------------------------------------------------------------
def fetch_ingredients(rxcui: str, tty: str, name: str) -> list:
    """
    Return a list of (ingredient_rxcui, ingredient_name) tuples.

    Logic:
    - If the concept is already an IN, it IS the ingredient -> return itself.
    - If it is a MIN, query /rxcui/{rxcui}/related?tty=IN to get component
      ingredients and return all of them.
    """
    if tty == "IN":
        return [(rxcui, name)]

    # MIN -> find related IN concepts
    url = f"{BASE_URL}/rxcui/{rxcui}/related.json"
    time.sleep(DEFAULT_DELAY_S)
    data = _get(url, params={"tty": "IN"})
    if not data:
        return [("", "")]

    ingredients = []
    for group in data.get("relatedGroup", {}).get("conceptGroup", []):
        if group.get("tty") == "IN":
            for cp in group.get("conceptProperties", []):
                ingredients.append((cp.get("rxcui", ""), cp.get("name", "")))

    return ingredients if ingredients else [("", "")]


# ---------------------------------------------------------------------------
# Step 4 - Build CSV rows for one concept
# ---------------------------------------------------------------------------
def build_rows(concept: dict) -> list:
    """
    Given a concept dict {rxcui, name, tty} produce one or more CSV rows.

    When a MIN has multiple IN ingredients, one row is written per ingredient
    so that ingredient_rxcui / ingredient_name are always atomic values.
    """
    rxcui = concept["rxcui"]
    name  = concept["name"]
    tty   = concept["tty"]

    synonym     = fetch_synonyms(rxcui)
    ingredients = fetch_ingredients(rxcui, tty, name)

    rows = []
    for ing_rxcui, ing_name in ingredients:
        rows.append({
            "query":            name,
            "rxcui":            rxcui,
            "name":             name,
            "tty":              tty,
            "synonym":          synonym,
            "ingredient_rxcui": ing_rxcui,
            "ingredient_name":  ing_name,
        })
    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    log.info("=== RxNorm Scraper started ===")

    # 1. Fetch concept list from /allconcepts
    concepts = fetch_all_concepts()

    # 2. Save all drug names to text file
    save_names(concepts)

    # 3. Determine resume point (in case of a previous interrupted run)
    processed_rxcuis: set = set()
    csv_exists = CSV_FILE.exists()
    if csv_exists:
        with open(CSV_FILE, "r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                processed_rxcuis.add(row.get("rxcui", ""))
        log.info(
            f"Resuming — {len(processed_rxcuis):,} rxcuis already processed."
        )

    remaining = [c for c in concepts if c["rxcui"] not in processed_rxcuis]
    log.info(f"{len(remaining):,} concepts left to process.")

    if not remaining:
        log.info("Nothing to do — all concepts already processed.")
        return

    # 4. Process concepts and stream rows to CSV
    write_mode = "a" if csv_exists else "w"
    with open(CSV_FILE, write_mode, encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        if not csv_exists:
            writer.writeheader()

        total = len(remaining)
        for idx, concept in enumerate(remaining, 1):
            rxcui = concept["rxcui"]
            name  = concept["name"]
            tty   = concept["tty"]

            if idx == 1 or idx % 500 == 0:
                pct = idx / total * 100
                log.info(
                    f"  [{idx:,}/{total:,}] ({pct:.1f}%) "
                    f"Processing: {name!r} ({rxcui}, {tty})"
                )

            try:
                rows = build_rows(concept)
            except Exception as exc:
                log.error(f"Unexpected error for {name!r} ({rxcui}): {exc}")
                rows = [{
                    "query":            name,
                    "rxcui":            rxcui,
                    "name":             name,
                    "tty":              tty,
                    "synonym":          "",
                    "ingredient_rxcui": "",
                    "ingredient_name":  "",
                }]

            writer.writerows(rows)
            fh.flush()   # ensure each row is written to disk immediately

    log.info(f"Done! CSV saved to {CSV_FILE}")
    if NAMES_FILE.exists():
        try:
            NAMES_FILE.unlink()
            log.info(f"Cleaned up intermediate text file: {NAMES_FILE}")
        except Exception as e:
            log.warning(f"Could not remove {NAMES_FILE}: {e}")
    log.info("=== RxNorm Scraper finished ===")


if __name__ == "__main__":
    main()
