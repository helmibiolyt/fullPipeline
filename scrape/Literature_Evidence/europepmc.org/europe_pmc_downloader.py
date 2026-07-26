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

# Columns of europe_pmc_full_text.csv. The text is parsed straight out of the
# JATS XML — no LLM is involved anywhere in this scraper.
FULLTEXT_FIELDS = ["full_text", "text_chars"]

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
# Parallel Worker for Article Processing
# ---------------------------------------------------------------------------

def process_single_article(article_data: dict, worker_id: int = 0) -> dict:
    """Fetch an article's JATS XML and return its cleaned narrative text."""
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
            
        return {
            "id": art_id,
            "status": "success",
            "full_text": clean_text,
            "text_chars": len(clean_text),
        }
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
        description="Search Europe PMC, download metadata to CSV, and store cleaned open-access full text."
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
        "--extract-fulltext", "--extract-llm",
        dest="extract_fulltext",
        action="store_true",
        help="Fetch open-access JATS XML and store the cleaned article text as CSV."
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=5,
        help="Number of concurrent threads to use for XML fetching."
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

    completed_ft_ids = set()
    if not args.fresh and fulltext_csv_file.exists():
        try:
            with open(fulltext_csv_file, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if header:
                    for row in reader:
                        if row:
                            completed_ft_ids.add(row[0])
            logger.info(f"Loaded {len(completed_ft_ids):,} existing records from {fulltext_csv_file} for full-text de-duplication.")
        except Exception as e:
            logger.warning(f"Could not read existing full text CSV: {e}")

    # 6. Initialize CSV Writers
    metadata_fields = [
        "id", "source", "pmid", "pmcid", "doi", "title", "abstract", "authors", 
        "author_affiliations", "author_orcids", "journal_title", "journal_issn", 
        "journal_volume", "journal_issue", "pub_year", "pub_type", "is_open_access", 
        "has_pdf", "cited_by_count", "date_of_creation", "first_publication_date", "full_text_urls"
    ]
    
    fulltext_csv_fields = ["id", "status"] + FULLTEXT_FIELDS + ["extracted_at"]
    
    meta_csv_exists = metadata_csv_file.exists() and not args.fresh
    ft_csv_exists = fulltext_csv_file.exists() and not args.fresh
    
    meta_mode = "a" if meta_csv_exists else "w"
    ft_mode = "a" if ft_csv_exists else "w"
    
    # Open CSV files
    f_meta = open(metadata_csv_file, meta_mode, newline="", encoding="utf-8")
    meta_writer = csv.DictWriter(f_meta, fieldnames=metadata_fields)
    if not meta_csv_exists:
        meta_writer.writeheader()
        f_meta.flush()

    f_ft = None
    ft_writer = None
    if args.extract_fulltext:
        f_ft = open(fulltext_csv_file, ft_mode, newline="", encoding="utf-8")
        ft_writer = csv.DictWriter(f_ft, fieldnames=fulltext_csv_fields)
        if not ft_csv_exists:
            ft_writer.writeheader()
            f_ft.flush()

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
            ft_queue = []
            
            for res in results:
                art_id = res.get("id")
                if not art_id:
                    continue
                    
                meta_already_exists = art_id in completed_ids
                needs_ft = args.extract_fulltext and art_id not in completed_ft_ids
                
                # Skip if metadata exists and we do not need full-text extraction
                if meta_already_exists and not needs_ft:
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
                
                # Queue open-access article for full-text extraction if enabled and needed
                if needs_ft:
                    # Check if article has XML capability (must have a PMCID and be open-access/inPMC)
                    if res.get("pmcid") and (res.get("isOpenAccess") == "Y" or res.get("inEPMC") == "Y" or res.get("inPMC") == "Y"):
                        ft_queue.append(res)
            
            f_meta.flush()
            processed_count += len(new_records)
            pages_count += 1
            
            logger.info(f"  Saved {len(new_records)} new metadata rows. Progress: {processed_count}/{args.limit if args.limit > 0 else hit_count}")

            # 7b. Fetch & save cleaned full text (thread pool processing)
            if args.extract_fulltext and ft_queue:
                logger.info(f"  Starting full-text XML processing for {len(ft_queue)} open-access XMLs with {args.threads} threads...")
                
                with ThreadPoolExecutor(max_workers=args.threads) as executor:
                    futures = {
                        executor.submit(process_single_article, art, idx): art["id"] 
                        for idx, art in enumerate(ft_queue)
                    }
                    
                    ft_success = 0
                    ft_fail = 0
                    for fut in as_completed(futures):
                        art_id = futures[fut]
                        try:
                            result = fut.result()
                            if result.get("status") == "success":
                                result["extracted_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                                # Write to full-text CSV
                                ft_writer.writerow({k: result.get(k, "") for k in fulltext_csv_fields})
                                f_ft.flush()
                                completed_ft_ids.add(art_id)
                                ft_success += 1
                            else:
                                logger.warning(f"    Failed to extract facts for {art_id}: {result.get('error')}")
                                ft_fail += 1
                        except Exception as exc:
                            logger.error(f"    Exception raised for {art_id}: {exc}")
                            ft_fail += 1
                logger.info(f"  Full-text batch processing complete. Success: {ft_success}, Failed: {ft_fail}")

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
        if f_ft:
            f_ft.close()
            
    logger.info("=" * 60)
    logger.info(f"CRAWL COMPLETE. Metadata saved to: {metadata_csv_file}")
    if args.extract_fulltext:
        logger.info(f"Full text saved to: {fulltext_csv_file}")
    logger.info("=" * 60)

if __name__ == "__main__":
    main()
