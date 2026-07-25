#!/usr/bin/env python3
"""
Europe PMC Downloader & Intelligent Crawler
Author: Antigravity AI Coding Assistant
Description: Extracts biomedical literature metadata from Europe PMC and dynamically distills 
             key scientific/clinical details from open-access full-text XMLs using the MiniMax API.
             Features robust cursor-based resume capabilities and concurrent processing.
"""

import os
import sys
import json
import time
import csv
import argparse
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Load credentials from .env file
load_dotenv()

# Resolve all paths relative to this script, never the current working directory.
BASE_DIR = Path(__file__).resolve().parent

# Standard headers to play nice with EBI servers
HEADERS = {
    "User-Agent": "BiolytInternEuropePMCDownloader/1.0 (Python; requests; Antigravity)",
    "Accept": "application/json,application/xml",
}

# LLM fields to extract from full text
LLM_FIELDS = [
    "nct_id", "brief_title", "official_title", "overall_status", "start_date",
    "sponsor", "condition", "phase", "intervention_name", "intervention_type",
    "primary_outcome", "secondary_outcome", "country", "study_type", "enrollment",
    "medicine_name", "ema_product_number", "active_substance", "composition", 
    "therapeutic_indications", "posology", "contraindications", "undesirable_effects", 
    "benefits_in_studies", "benefit_risk_balance", "document_type", "document_url",
    "llm_relevant_details"
]

# Logger setup
logger = logging.getLogger("europe_pmc_downloader")

def setup_logging(verbose: bool):
    """Configures the logging output level."""
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("europe_pmc_downloader.log", encoding="utf-8")
        ]
    )

# ---------------------------------------------------------------------------
# XML Parsing & Clean Text Extraction
# ---------------------------------------------------------------------------

def extract_clean_text_from_xml(xml_content: str) -> str:
    """
    Parses JATS XML and extracts clean narrative text, stripping tags 
    and removing bibliographies/references.
    """
    soup = BeautifulSoup(xml_content, "xml")
    
    # Try to find the body element (contains actual publication narrative)
    body = soup.find("body")
    if body:
        # Decompose reference citations (e.g. <xref ref-type="bibr">) to avoid text noise
        for xref in body.find_all("xref"):
            xref.decompose()
        
        # Decompose tables/math figures if needed, but keeping text is usually okay
        paragraphs = []
        for p in body.find_all("p"):
            text = p.get_text()
            cleaned_text = " ".join(text.split())
            if cleaned_text:
                paragraphs.append(cleaned_text)
        return "\n\n".join(paragraphs)
    
    # Fallback: if no body, decompose ref-list and back sections
    for ref_list in soup.find_all("ref-list"):
        ref_list.decompose()
    for back in soup.find_all("back"):
        back.decompose()
        
    text = soup.get_text()
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    return "\n\n".join(lines)

def fetch_full_text_xml(pmcid: str) -> str:
    """Downloads full-text XML for a given PMCID from Europe PMC."""
    url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text

# ---------------------------------------------------------------------------
# MiniMax LLM Extraction
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Multi-LLM Extraction (Groq, Gemini, MiniMax)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are an expert clinical trials and biomedical data extractor. Read the provided scientific article text "
    "and extract the following details if they exist in the text. For any field not mentioned or not found in the text, "
    "you MUST set its value to 'None'.\n\n"
    "FIELDS TO EXTRACT:\n"
    "- nct_id: ClinicalTrials.gov NCT identifier (e.g. NCT01234567)\n"
    "- brief_title: Short title of the study\n"
    "- official_title: Full/official title of the study\n"
    "- overall_status: Recruitment/overall status of the study (e.g. Active, Completed, recruiting)\n"
    "- start_date: Start date of the study\n"
    "- sponsor: Lead sponsor or organization\n"
    "- condition: The disease, syndrome, or health condition being studied\n"
    "- phase: The clinical trial phase (e.g. Phase 1, Phase 2, Phase 3, Phase 4)\n"
    "- intervention_name: Name of the drug, biologic, device, or intervention being tested\n"
    "- intervention_type: Type of the intervention (e.g. Drug, Biologic, Procedure, Device)\n"
    "- primary_outcome: The main measurement/outcome used to evaluate efficacy/safety\n"
    "- secondary_outcome: Other important measurements/outcomes evaluated\n"
    "- country: Country or countries where the study was conducted\n"
    "- study_type: Study design (e.g. Interventional, Observational, Randomized Controlled Trial)\n"
    "- enrollment: Planned or actual number of study participants\n"
    "- medicine_name: Brand or common name of the medicine\n"
    "- ema_product_number: The European Medicines Agency (EMA) product number if mentioned\n"
    "- active_substance: Name of the active substance or drug compound\n"
    "- composition: Chemical composition, ingredients, or vector details if mentioned\n"
    "- therapeutic_indications: The approved or studied medical uses/indications\n"
    "- posology: Dosage, administration route, schedule, or frequency\n"
    "- contraindications: Conditions or factors that serve as a reason to withhold treatment\n"
    "- undesirable_effects: Reported side effects, toxicities, or adverse events\n"
    "- benefits_in_studies: Documented clinical benefits or positive outcomes from the studies\n"
    "- benefit_risk_balance: The model's or study's assessment of the benefits vs risk ratio\n"
    "- document_type: The type of document (e.g. overview, assessment report, scientific discussion)\n"
    "- document_url: The URL or links to regulatory documents if mentioned in the text\n\n"
    "Additionally, identify other important clinical/scientific facts not captured by the fields above, "
    "and return them as key-value pairs in the 'relevant_info' object.\n\n"
    "You MUST respond ONLY with a JSON object containing these exact 27 keys + 'relevant_info', with no extra formatting, "
    "no explanation, and no comments. All values must be strings:\n"
    "{\n"
    "  \"nct_id\": \"None\",\n"
    "  \"brief_title\": \"None\",\n"
    "  \"official_title\": \"None\",\n"
    "  \"overall_status\": \"None\",\n"
    "  \"start_date\": \"None\",\n"
    "  \"sponsor\": \"None\",\n"
    "  \"condition\": \"None\",\n"
    "  \"phase\": \"None\",\n"
    "  \"intervention_name\": \"None\",\n"
    "  \"intervention_type\": \"None\",\n"
    "  \"primary_outcome\": \"None\",\n"
    "  \"secondary_outcome\": \"None\",\n"
    "  \"country\": \"None\",\n"
    "  \"study_type\": \"None\",\n"
    "  \"enrollment\": \"None\",\n"
    "  \"medicine_name\": \"None\",\n"
    "  \"ema_product_number\": \"None\",\n"
    "  \"active_substance\": \"None\",\n"
    "  \"composition\": \"None\",\n"
    "  \"therapeutic_indications\": \"None\",\n"
    "  \"posology\": \"None\",\n"
    "  \"contraindications\": \"None\",\n"
    "  \"undesirable_effects\": \"None\",\n"
    "  \"benefits_in_studies\": \"None\",\n"
    "  \"benefit_risk_balance\": \"None\",\n"
    "  \"document_type\": \"None\",\n"
    "  \"document_url\": \"None\",\n"
    "  \"relevant_info\": {\n"
    "     \"key_name_1\": \"value_1\"\n"
    "  }\n"
    "}"
)

def clean_json_response(content: str) -> dict:
    """Helper to clean reasoning thoughts and markdown backticks from LLM responses."""
    if "<think>" in content and "</think>" in content:
        content = content.split("</think>", 1)[1].strip()
    elif "<think>" in content:
        parts = content.split("<think>", 1)
        content = parts[0].strip() if len(parts[0].strip()) > 10 else parts[1].strip()
        
    content = content.strip()
    if content.startswith("```json"):
        content = content[7:]
    elif content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]
    content = content.strip()
    
    parsed = json.loads(content)
    if isinstance(parsed, list) and len(parsed) > 0 and isinstance(parsed[0], dict):
        parsed = parsed[0]
    return parsed if isinstance(parsed, dict) else {}

def extract_info_via_groq(text: str, groq_key: str) -> dict:
    """Sends text to Groq API (llama-3.3-70b-versatile)."""
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"}
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"ARTICLE TEXT:\n{text[:30000]}"}
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"}
    }
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    return clean_json_response(content)

def extract_info_via_gemini(text: str, gemini_key: str) -> dict:
    """Sends text to Google Gemini REST API (gemini-3.5-flash-lite)."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent?key={gemini_key}"
    payload = {
        "contents": [{"parts": [{"text": f"{SYSTEM_PROMPT}\n\nARTICLE TEXT:\n{text[:30000]}"}]}],
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0.1}
    }
    r = requests.post(url, json=payload, timeout=60)
    r.raise_for_status()
    content = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    return clean_json_response(content)

def extract_info_via_minimax(text: str, api_key: str, base_url: str, model: str) -> dict:
    """Sends text to MiniMax API."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"ARTICLE TEXT:\n{text[:30000]}"}
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "extra_body": {"reasoning_split": True}
    }
    r = requests.post(url, headers=headers, json=payload, timeout=90)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    return clean_json_response(content)

def extract_info_multi_llm(text: str, worker_id: int = 0) -> dict:
    """
    Multi-LLM extractor that load-balances requests across Groq, Gemini, and MiniMax
    with automatic failover to alternative providers.
    """
    groq_key = os.getenv("GROQ_API_KEY")
    gemini_key = os.getenv("GEMINI_API_KEY")
    minimax_key = os.getenv("MINIMAX_API_KEY")
    minimax_base = os.getenv("MINIMAX_BASE_URL", "https://api.minimax.io/v1")
    
    # Build list of active providers
    providers = []
    if groq_key:
        providers.append("groq")
    if gemini_key:
        providers.append("gemini")
    if minimax_key:
        providers.append("minimax")
        
    if not providers:
        raise RuntimeError("No LLM API keys found in environment.")
        
    # Rotate primary provider based on worker_id
    start_index = worker_id % len(providers)
    ordered_providers = providers[start_index:] + providers[:start_index]
    
    last_error = None
    for p in ordered_providers:
        try:
            if p == "groq":
                logger.debug("Calling Groq API...")
                return extract_info_via_groq(text, groq_key)
            elif p == "gemini":
                logger.debug("Calling Gemini API...")
                return extract_info_via_gemini(text, gemini_key)
            elif p == "minimax":
                logger.debug("Calling MiniMax API...")
                return extract_info_via_minimax(text, minimax_key, minimax_base, "MiniMax-M3")
        except Exception as e:
            logger.warning(f"Provider {p} failed: {e}. Trying next provider...")
            last_error = e
            
    raise RuntimeError(f"All LLM providers failed. Last error: {last_error}")

# ---------------------------------------------------------------------------
# Parallel Worker for Article Processing
# ---------------------------------------------------------------------------

def process_single_article(article_data: dict, api_key: str, base_url: str, model: str, worker_id: int = 0) -> dict:
    """
    Worker function to fetch XML, parse clean text, and call multi-LLM extraction.
    """
    art_id = article_data["id"]
    pmcid = article_data.get("pmcid")
    
    if not pmcid:
        return {"id": art_id, "status": "no_pmcid", "error": "No PMCID available for full-text XML"}
        
    try:
        # 1. Fetch XML
        logger.debug(f"Fetching XML for {art_id} ({pmcid})...")
        xml_content = fetch_full_text_xml(pmcid)
        
        # 2. Extract clean text
        logger.debug(f"Parsing text for {art_id}...")
        clean_text = extract_clean_text_from_xml(xml_content)
        
        if not clean_text or len(clean_text.strip()) < 100:
            return {"id": art_id, "status": "empty_text", "error": "Cleaned text body was too short or empty"}
            
        # 3. Call Multi-LLM Extractor
        llm_data = extract_info_multi_llm(clean_text, worker_id=worker_id)
        
        # Build result dictionary with fallback to 'None'
        res_dict = {
            "id": art_id,
            "status": "success"
        }
        for field in LLM_FIELDS:
            if field == "llm_relevant_details":
                relevant = llm_data.get("relevant_info", {})
                res_dict["llm_relevant_details"] = json.dumps(relevant, ensure_ascii=False) if relevant else "None"
            else:
                res_dict[field] = llm_data.get(field, "None")
        return res_dict
    except Exception as e:
        logger.error(f"Error processing article {art_id} ({pmcid}): {e}")
        return {"id": art_id, "status": "failed", "error": str(e)}

# ---------------------------------------------------------------------------
# Helper Parsers for JSON Metadata fields
# ---------------------------------------------------------------------------

def parse_authors(result: dict) -> tuple:
    """Parses authors, affiliations, and ORCIDs from core result metadata."""
    authors = []
    affiliations = set()
    orcids = []
    
    author_list = result.get("authorList", {}).get("author", [])
    if not author_list and "authorString" in result:
        return result["authorString"], "", ""
        
    for auth in author_list:
        fullName = auth.get("fullName", auth.get("lastName", ""))
        if fullName:
            authors.append(fullName)
            
        # ORCIDs
        auth_id = auth.get("authorId", {})
        if auth_id.get("type") == "ORCID" and auth_id.get("value"):
            orcids.append(auth_id["value"])
            
        # Affiliations
        aff_list = auth.get("authorAffiliationDetailsList", {}).get("authorAffiliation", [])
        for aff in aff_list:
            aff_text = aff.get("affiliation")
            if aff_text:
                affiliations.add(aff_text.strip())
                
    return "; ".join(authors), "; ".join(sorted(list(affiliations))), "; ".join(orcids)

def parse_journal_info(result: dict) -> tuple:
    """Parses journal details from journalInfo nested object."""
    j_info = result.get("journalInfo", {})
    j_title = j_info.get("journal", {}).get("title", result.get("journalTitle", ""))
    j_issn = j_info.get("journal", {}).get("issn", "")
    j_volume = j_info.get("volume", "")
    j_issue = j_info.get("issue", "")
    return j_title, j_issn, j_volume, j_issue

def parse_mesh_headings(result: dict) -> tuple:
    """Parses MeSH headings and identifies major medical topics."""
    descriptors = []
    major_topics = []
    mesh_list = result.get("meshHeadingList", {}).get("meshHeading", [])
    for mesh in mesh_list:
        desc = mesh.get("descriptorName")
        if desc:
            descriptors.append(desc)
            if mesh.get("majorTopic_YN") == "Y":
                major_topics.append(desc)
    return "; ".join(descriptors), "; ".join(major_topics)

def parse_chemicals(result: dict) -> tuple:
    """Parses chemical substances and registry numbers."""
    names = []
    registries = []
    chem_list = result.get("chemicalList", {}).get("chemical", [])
    for chem in chem_list:
        name = chem.get("name")
        reg = chem.get("registryNumber")
        if name:
            names.append(name)
        if reg and reg != "0":
            registries.append(f"{name} ({reg})")
    return "; ".join(names), "; ".join(registries)

def parse_grants(result: dict) -> str:
    """Parses funding grant details."""
    grants = []
    grant_list = result.get("grantsList", {}).get("grant", [])
    for gr in grant_list:
        agency = gr.get("agency", "Unknown Agency")
        g_id = gr.get("grantId")
        acronym = gr.get("acronym")
        details = agency
        if g_id:
            details += f" (ID: {g_id}"
            if acronym:
                details += f", Acronym: {acronym}"
            details += ")"
        grants.append(details)
    return "; ".join(grants)

def parse_fulltext_urls(result: dict) -> str:
    """Parses direct URLs from fullTextUrlList."""
    urls = []
    url_list = result.get("fullTextUrlList", {}).get("fullTextUrl", [])
    for u in url_list:
        url_val = u.get("url")
        style = u.get("documentStyle", "link")
        site = u.get("site", "External")
        if url_val:
            urls.append(f"[{site} {style}] {url_val}")
    return "; ".join(urls)

# ---------------------------------------------------------------------------
# Main Crawler Orchestration
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Search Europe PMC, download metadata to CSV, and extract clinical facts with MiniMax LLM."
    )
    parser.add_argument(
        "--query",
        type=str,
        default="cancer OR diabetes OR influenza OR malaria OR cardiovascular OR pulmonary OR hypertension OR asthma OR arthritis OR vaccine OR \"clinical trial\"",
        help="Biomedical search query keywords for Europe PMC."
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(BASE_DIR / "europe_pmc"),
        help="Directory to save downloaded CSVs and logs."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="Maximum total records to download. Set to 0 or negative for unlimited."
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=1000,
        help="Page size (1-1000) for Europe PMC requests."
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Polite sleep delay in seconds between sequential requests."
    )
    parser.add_argument(
        "--result-type",
        type=str,
        default="core",
        choices=["core", "lite"],
        help="Detail level of results returned from Europe PMC (core or lite)."
    )
    parser.add_argument(
        "--open-access",
        action="store_true",
        help="Filter results to open-access articles only."
    )
    parser.add_argument(
        "--has-pdf",
        action="store_true",
        help="Filter results to articles with a PDF available."
    )
    parser.add_argument(
        "--from-year",
        type=int,
        help="Filter publications starting from this year (inclusive)."
    )
    parser.add_argument(
        "--to-year",
        type=int,
        help="Filter publications ending at this year (inclusive)."
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore previous progress and run a clean search crawl."
    )
    parser.add_argument(
        "--extract-llm",
        action="store_true",
        help="Enable full-text XML extraction and MiniMax LLM clinical facts extraction."
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=5,
        help="Number of concurrent threads to use for XML fetching and LLM calls."
    )
    parser.add_argument(
        "--llm-model",
        type=str,
        default="MiniMax-M3",
        help="MiniMax model name to use."
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose DEBUG logging."
    )
    parser.add_argument(
        "--email",
        type=str,
        help="Optional email address to attach to User-Agent (recommended by Europe PMC)."
    )

    args = parser.parse_args()
    setup_logging(args.verbose)
    
    # Configure headers with email if provided
    if args.email:
        HEADERS["User-Agent"] = f"BiolytInternEuropePMCDownloader/1.0 (Python; requests; Antigravity; contact: {args.email})"

    # 1. Resolve Directories
    output_path = Path(args.output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    metadata_csv_file = output_path / "europe_pmc_metadata.csv"
    fulltext_csv_file = output_path / "europe_pmc_full_text.csv"
    progress_file = output_path / "europe_pmc_progress.json"

    # 2. Secure API Credentials if LLM is enabled
    api_key = os.getenv("MINIMAX_API_KEY")
    base_url = os.getenv("MINIMAX_BASE_URL", "https://api.minimax.io/v1")
    
    if args.extract_llm:
        if not api_key:
            logger.error("MINIMAX_API_KEY environment variable not found in .env or environment. LLM extraction aborted.")
            sys.exit(1)
        logger.info(f"LLM Extraction active using model: {args.llm_model}")

    # 3. Formulate Query
    query_parts = [args.query]
    if args.open_access:
        query_parts.append("(OPEN_ACCESS:y)")
    if args.has_pdf:
        query_parts.append("(HAS_PDF:y)")
    if args.from_year or args.to_year:
        fy = args.from_year if args.from_year else "1800"
        ty = args.to_year if args.to_year else "3000"
        query_parts.append(f"(PUB_YEAR:[{fy} TO {ty}])")
        
    search_query = " AND ".join(query_parts)
    logger.info(f"Target Europe PMC Search Query: {search_query}")

    # 4. Load Progress
    cursor_mark = "*"
    processed_count = 0
    pages_count = 0
    
    if not args.fresh and progress_file.exists():
        try:
            with open(progress_file, "r", encoding="utf-8") as pf:
                progress = json.load(pf)
            # Verify query matches to prevent resuming different queries by mistake
            if progress.get("query") == search_query:
                cursor_mark = progress.get("next_cursor_mark", "*")
                processed_count = progress.get("total_records_processed", 0)
                pages_count = progress.get("total_pages_processed", 0)
                logger.info(f"Resuming progress from last run. Processed={processed_count}, Pages={pages_count}, Cursor={cursor_mark}")
            else:
                logger.warning("Search query changed. Starting fresh crawl. Use --fresh to suppress this warning.")
        except Exception as e:
            logger.warning(f"Could not load progress file: {e}. Starting fresh.")

    # 5. Populate De-duplication Cache (Read existing CSV rows)
    completed_ids = set()
    if not args.fresh and metadata_csv_file.exists():
        try:
            with open(metadata_csv_file, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if header:
                    # 'id' is the first column
                    for row in reader:
                        if row:
                            completed_ids.add(row[0])
            logger.info(f"Loaded {len(completed_ids):,} existing records from {metadata_csv_file} for de-duplication.")
        except Exception as e:
            logger.warning(f"Could not read existing metadata CSV: {e}")

    completed_llm_ids = set()
    if not args.fresh and fulltext_csv_file.exists():
        try:
            with open(fulltext_csv_file, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if header:
                    for row in reader:
                        if row:
                            completed_llm_ids.add(row[0])
            logger.info(f"Loaded {len(completed_llm_ids):,} existing records from {fulltext_csv_file} for LLM de-duplication.")
        except Exception as e:
            logger.warning(f"Could not read existing full text CSV: {e}")

    # 6. Initialize CSV Writers
    metadata_fields = [
        "id", "source", "pmid", "pmcid", "doi", "title", "abstract", "authors", 
        "author_affiliations", "author_orcids", "journal_title", "journal_issn", 
        "journal_volume", "journal_issue", "pub_year", "pub_type", "is_open_access", 
        "has_pdf", "cited_by_count", "date_of_creation", "first_publication_date", "full_text_urls"
    ]
    
    llm_fields = ["id"] + LLM_FIELDS + ["extracted_at"]
    
    meta_csv_exists = metadata_csv_file.exists() and not args.fresh
    llm_csv_exists = fulltext_csv_file.exists() and not args.fresh
    
    meta_mode = "a" if meta_csv_exists else "w"
    llm_mode = "a" if llm_csv_exists else "w"
    
    # Open CSV files
    f_meta = open(metadata_csv_file, meta_mode, newline="", encoding="utf-8")
    meta_writer = csv.DictWriter(f_meta, fieldnames=metadata_fields)
    if not meta_csv_exists:
        meta_writer.writeheader()
        f_meta.flush()

    f_llm = None
    llm_writer = None
    if args.extract_llm:
        f_llm = open(fulltext_csv_file, llm_mode, newline="", encoding="utf-8")
        llm_writer = csv.DictWriter(f_llm, fieldnames=llm_fields)
        if not llm_csv_exists:
            llm_writer.writeheader()
            f_llm.flush()

    # 7. Search & Pagination Loop
    api_url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    hit_count = None
    
    logger.info("=" * 60)
    logger.info("          STARTING EUROPE PMC CRAWL PROCESS")
    logger.info("=" * 60)

    try:
        while True:
            # Respect limit
            if args.limit > 0 and processed_count >= args.limit:
                logger.info(f"Target limit of {args.limit} records reached. Finishing crawl.")
                break
                
            current_page_size = args.page_size
            if args.limit > 0:
                # Adjust final page size to hit the limit exactly
                current_page_size = min(args.page_size, args.limit - processed_count)
                
            logger.info(f"Requesting page {pages_count + 1} (pageSize={current_page_size}, cursorMark={cursor_mark})...")
            
            params = {
                "query": search_query,
                "resultType": args.result_type,
                "pageSize": current_page_size,
                "cursorMark": cursor_mark,
                "format": "json"
            }
            
            # API request with retries
            resp_data = None
            for attempt in range(100):
                try:
                    r = requests.get(api_url, params=params, headers=HEADERS, timeout=40)
                    if r.status_code == 429:
                        logger.warning(f"EBI API rate limited (429). Sleeping 10s...")
                        time.sleep(10)
                        continue
                    r.raise_for_status()
                    resp_data = r.json()
                    break
                except Exception as e:
                    logger.error(f"EBI API call attempt {attempt+1}/100 failed: {e}. Retrying in 30s...")
                    if attempt == 99:
                        raise e
                    time.sleep(30)
            
            if not resp_data:
                logger.error("Could not fetch page from Europe PMC API. Aborting.")
                break
                
            hit_count = resp_data.get("hitCount", 0)
            next_cursor = resp_data.get("nextCursorMark")
            results = resp_data.get("resultList", {}).get("result", [])
            
            logger.info(f"  Found {len(results)} results on this page. Total matching articles: {hit_count:,}")
            
            if not results:
                logger.info("No more results returned from EBI. Crawl complete.")
                break

            # 7a. Parse & Save Metadata
            new_records = []
            llm_queue = []
            
            for res in results:
                art_id = res.get("id")
                if not art_id:
                    continue
                    
                meta_already_exists = art_id in completed_ids
                needs_llm = args.extract_llm and art_id not in completed_llm_ids
                
                # Skip if metadata exists and we do not need LLM extraction
                if meta_already_exists and not needs_llm:
                    logger.debug(f"  Skipping existing record for {art_id}")
                    continue
                    
                if not meta_already_exists:
                    # Parse authors and affiliations
                    authors, affiliations, orcids = parse_authors(res)
                    
                    # Parse journal info
                    j_title, j_issn, j_volume, j_issue = parse_journal_info(res)
                    
                    # Parse MeSH headings
                    descriptors, major_topics = parse_mesh_headings(res)
                    
                    # Parse chemicals
                    chems, registries = parse_chemicals(res)
                    
                    # Parse grants
                    grants = parse_grants(res)
                    
                    # Parse full-text URLs
                    urls = parse_fulltext_urls(res)
                    
                    # Format pub types
                    pub_types_list = []
                    raw_pub_types = res.get("pubTypeList", {}).get("pubType", [])
                    if isinstance(raw_pub_types, list):
                        for pt in raw_pub_types:
                            if isinstance(pt, dict):
                                pt_val = pt.get("pubType")
                                if pt_val:
                                    pub_types_list.append(pt_val)
                            elif isinstance(pt, str):
                                pub_types_list.append(pt)
                    elif isinstance(raw_pub_types, str):
                        pub_types_list.append(raw_pub_types)
                    pub_types = "; ".join(pub_types_list)
                    
                    meta_row = {
                        "id": art_id,
                        "source": res.get("source", ""),
                        "pmid": res.get("pmid", ""),
                        "pmcid": res.get("pmcid", ""),
                        "doi": res.get("doi", ""),
                        "title": res.get("title", ""),
                        "abstract": res.get("abstractText", ""),
                        "authors": authors,
                        "author_affiliations": affiliations,
                        "author_orcids": orcids,
                        "journal_title": j_title,
                        "journal_issn": j_issn,
                        "journal_volume": j_volume,
                        "journal_issue": j_issue,
                        "pub_year": res.get("pubYear", ""),
                        "pub_type": pub_types,
                        "is_open_access": res.get("isOpenAccess", ""),
                        "has_pdf": res.get("hasPDF", ""),
                        "cited_by_count": res.get("citedByCount", 0),
                        "date_of_creation": res.get("dateOfCreation", ""),
                        "first_publication_date": res.get("firstPublicationDate", ""),
                        "full_text_urls": urls
                    }
                    
                    meta_writer.writerow(meta_row)
                    completed_ids.add(art_id)
                    new_records.append(art_id)
                
                # Queue open-access article for LLM extraction if enabled and needed
                if needs_llm:
                    # Check if article has XML capability (must have a PMCID and be open-access/inPMC)
                    if res.get("pmcid") and (res.get("isOpenAccess") == "Y" or res.get("inEPMC") == "Y" or res.get("inPMC") == "Y"):
                        llm_queue.append(res)
            
            f_meta.flush()
            processed_count += len(new_records)
            pages_count += 1
            
            logger.info(f"  Saved {len(new_records)} new metadata rows. Progress: {processed_count}/{args.limit if args.limit > 0 else hit_count}")

            # 7b. Parse & Save LLM Dynamic Details (Thread Pool processing)
            if args.extract_llm and llm_queue:
                logger.info(f"  Starting LLM processing for {len(llm_queue)} open-access XMLs with {args.threads} threads...")
                
                with ThreadPoolExecutor(max_workers=args.threads) as executor:
                    futures = {
                        executor.submit(process_single_article, art, api_key, base_url, args.llm_model, idx): art["id"] 
                        for idx, art in enumerate(llm_queue)
                    }
                    
                    llm_success = 0
                    llm_fail = 0
                    for fut in as_completed(futures):
                        art_id = futures[fut]
                        try:
                            result = fut.result()
                            if result.get("status") == "success":
                                result["extracted_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                                # Write to LLM CSV
                                llm_writer.writerow({k: result[k] for k in llm_fields})
                                f_llm.flush()
                                completed_llm_ids.add(art_id)
                                llm_success += 1
                            else:
                                logger.warning(f"    Failed to extract facts for {art_id}: {result.get('error')}")
                                llm_fail += 1
                        except Exception as exc:
                            logger.error(f"    Exception raised for {art_id}: {exc}")
                            llm_fail += 1
                logger.info(f"  LLM batch processing complete. Success: {llm_success}, Failed: {llm_fail}")

            # 7c. Save Progress State
            with open(progress_file, "w", encoding="utf-8") as pf:
                json.dump({
                    "query": search_query,
                    "next_cursor_mark": next_cursor,
                    "total_records_processed": processed_count,
                    "total_pages_processed": pages_count,
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                }, pf, indent=2)

            # Check if we should break
            if cursor_mark == next_cursor:
                logger.info("CursorMark unchanged. Finished all available results.")
                break
                
            cursor_mark = next_cursor
            
            # Polite delay between pages
            if args.delay > 0:
                time.sleep(args.delay)

    except KeyboardInterrupt:
        logger.warning("Crawl interrupted by user. Saving progress state...")
        # Save progress state on CTRL+C
        with open(progress_file, "w", encoding="utf-8") as pf:
            json.dump({
                "query": search_query,
                "next_cursor_mark": cursor_mark,
                "total_records_processed": processed_count,
                "total_pages_processed": pages_count,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
            }, pf, indent=2)
        logger.info("Progress saved. You can run the command again to resume.")
    finally:
        f_meta.close()
        if f_llm:
            f_llm.close()
            
    logger.info("=" * 60)
    logger.info(f"CRAWL COMPLETE. Metadata saved to: {metadata_csv_file}")
    if args.extract_llm:
        logger.info(f"LLM details saved to: {fulltext_csv_file}")
    logger.info("=" * 60)

if __name__ == "__main__":
    main()
