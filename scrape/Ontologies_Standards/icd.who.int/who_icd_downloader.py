#!/usr/bin/env python3
"""
WHO ICD-10/11 Disease Classification Downloader and Parser
Author: Antigravity AI Coding Assistant
Description: Crawls and compiles WHO ICD-10 (2019) and ICD-11 (2026-01) disease classifications.
             Includes block-level checkpointing for ICD-10 and atomic CDN ingestion for ICD-11.
"""

import os
import sys
import time
import json
import csv
import zipfile
import argparse
import logging
import requests
from bs4 import BeautifulSoup
from pathlib import Path

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("who_icd_downloader")

# Resolve all output paths relative to this script's folder (pipeline contract)
BASE_DIR = Path(__file__).resolve().parent

# HTTP headers to mimic browser access and avoid blocks
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9"
}

BASE_ICD10_URL = "https://icd.who.int/browse10/2019/en"
ICD11_ZIP_URL = "https://icdcdn.who.int/static/releasefiles/2026-01/SimpleTabulation-ICD-11-MMS-en.zip"

class ICD10Crawler:
    def __init__(self, data_dir: Path, delay: float = 1.5, force: bool = False):
        self.data_dir = data_dir
        self.temp_dir = data_dir / "temp_blocks"
        self.progress_file = data_dir / "progress_icd10.json"
        self.delay = delay
        self.force = force
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        
    def fetch_json(self, url: str) -> list:
        logger.debug(f"GET JSON: {url}")
        try:
            r = self.session.get(url, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"Failed to fetch JSON from {url}: {e}")
            raise

    def fetch_html(self, url: str) -> str:
        logger.debug(f"GET HTML: {url}")
        try:
            r = self.session.get(url, timeout=20)
            r.raise_for_status()
            return r.text
        except Exception as e:
            logger.error(f"Failed to fetch HTML from {url}: {e}")
            raise

    def run_discovery(self) -> dict:
        """
        Queries the JSON API to discover all chapters and blocks.
        """
        logger.info("Starting ICD-10 discovery phase...")
        
        # 1. Get Chapters (Roots)
        root_url = f"{BASE_ICD10_URL}/JsonGetRootConcepts?useHtml=false"
        chapters_data = self.fetch_json(root_url)
        
        progress = {
            "chapters": [],
            "blocks": []
        }
        
        logger.info(f"Discovered {len(chapters_data)} chapters.")
        
        for ch in chapters_data:
            ch_id = ch.get("ID")
            ch_label = ch.get("label", "")
            # Clean title
            clean_title = ch_label.replace(ch_id, "", 1).strip()
            
            progress["chapters"].append({
                "id": ch_id,
                "title": clean_title,
                "label": ch_label,
                "status": "pending"
            })
            
            # 2. Get Blocks under this chapter
            logger.info(f"Discovering blocks for Chapter {ch_id}...")
            time.sleep(self.delay)
            blocks_url = f"{BASE_ICD10_URL}/JsonGetChildrenConcepts?ConceptId={ch_id}&useHtml=false"
            blocks_data = self.fetch_json(blocks_url)
            
            for bl in blocks_data:
                bl_id = bl.get("ID")
                bl_label = bl.get("label", "")
                
                progress["blocks"].append({
                    "id": bl_id,
                    "title": bl_label,
                    "chapter_id": ch_id,
                    "status": "pending"
                })
                
        # Save progress
        with open(self.progress_file, "w", encoding="utf-8") as f:
            json.dump(progress, f, indent=2)
            
        logger.info(f"Discovery complete. Discovered {len(progress['blocks'])} blocks total.")
        return progress

    def load_or_create_progress(self) -> dict:
        if self.progress_file.exists() and not self.force:
            logger.info("Loading existing progress file...")
            try:
                with open(self.progress_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Error loading progress file: {e}. Re-creating progress.")
                
        return self.run_discovery()

    def parse_chapter_html(self, html_content: str, chapter_id: str) -> dict:
        """
        Parses chapter details page to extract inclusions, exclusions, and notes.
        """
        soup = BeautifulSoup(html_content, "html.parser")
        main_div = soup.find("div", id="classicont")
        if not main_div:
            return {}
            
        inclusions = []
        exclusions = []
        notes = []
        chapter_range = ""
        
        # We parse elements before the list of blocks
        import re
        for child in main_div.children:
            if child.name is None:
                continue
            
            classes = child.get("class", [])
            classes_str = " ".join(classes)
            
            if "Chapter" in classes_str:
                h1 = child.find("h1")
                if h1:
                    chapter_text = h1.get_text(separator=" ", strip=True)
                else:
                    chapter_text = child.get_text(separator=" ", strip=True)
                
                range_match = re.search(r'\(([A-Z][0-9]{2}-[A-Z][0-9]{2})\)', chapter_text)
                if range_match:
                    chapter_range = range_match.group(1)
                
                # Check for nested dl
                for dl in child.find_all("dl"):
                    dl_classes = dl.get("class", [])
                    dl_classes_str = " ".join(dl_classes)
                    dd_texts = [" ".join(dd.get_text(separator=" ", strip=True).split()) for dd in dl.find_all("dd")]
                    if "Rubric-inclusion" in dl_classes_str:
                        inclusions.extend(dd_texts)
                    elif "Rubric-exclusion" in dl_classes_str:
                        exclusions.extend(dd_texts)
                    elif "Rubric-note" in dl_classes_str or "Rubric-coding-hint" in dl_classes_str:
                        notes.extend(dd_texts)
                
            elif child.name == "dl":
                dd_texts = [" ".join(dd.get_text(separator=" ", strip=True).split()) for dd in child.find_all("dd")]
                if "Rubric-inclusion" in classes_str:
                    inclusions.extend(dd_texts)
                elif "Rubric-exclusion" in classes_str:
                    exclusions.extend(dd_texts)
                elif "Rubric-note" in classes_str or "Rubric-coding-hint" in classes_str:
                    notes.extend(dd_texts)
            elif "Block" in classes_str or child.name == "table" or child.find("table"):
                break
                
        return {
            "inclusions": inclusions,
            "exclusions": exclusions,
            "notes": notes,
            "code_range": chapter_range
        }

    def parse_block_html(self, html_content: str) -> dict:
        """
        Parses block HTML containing block-level metadata and all codes in that block.
        """
        soup = BeautifulSoup(html_content, "html.parser")
        main_div = soup.find("div", id="classicont")
        if not main_div:
            return {"block_info": {}, "codes": []}
            
        import re
        block_info = {
            "title": "",
            "code_range": "",
            "inclusions": [],
            "exclusions": [],
            "notes": []
        }
        
        codes = []
        
        # Linear scan of main_div's children
        for child in main_div.children:
            if child.name is None:
                continue
                
            classes = child.get("class", [])
            classes_str = " ".join(classes)
            
            if "Block" in classes_str:
                anchor = child.find("a", class_="anchor")
                block_info["id"] = anchor.get("name") if anchor else ""
                
                h2 = child.find("h2")
                if h2:
                    block_text = h2.get_text(separator=" ", strip=True)
                else:
                    block_text = child.get_text(separator=" ", strip=True)
                
                block_info["title"] = " ".join(block_text.split())
                range_match = re.search(r'\(([A-Z][0-9]{2}-[A-Z][0-9]{2})\)', block_text)
                if range_match:
                    block_info["code_range"] = range_match.group(1)
                    
                # Check for block-level inclusions/exclusions nested inside the Block div
                for dl in child.find_all("dl"):
                    dl_classes = dl.get("class", [])
                    dl_classes_str = " ".join(dl_classes)
                    dd_texts = [" ".join(dd.get_text(separator=" ", strip=True).split()) for dd in dl.find_all("dd")]
                    if "Rubric-inclusion" in dl_classes_str:
                        block_info["inclusions"].extend(dd_texts)
                    elif "Rubric-exclusion" in dl_classes_str:
                        block_info["exclusions"].extend(dd_texts)
                    elif "Rubric-note" in dl_classes_str or "Rubric-coding-hint" in dl_classes_str:
                        block_info["notes"].extend(dd_texts)
                    
            elif child.name == "dl":
                dd_texts = [" ".join(dd.get_text(separator=" ", strip=True).split()) for dd in child.find_all("dd")]
                if "Rubric-inclusion" in classes_str:
                    block_info["inclusions"].extend(dd_texts)
                elif "Rubric-exclusion" in classes_str:
                    block_info["exclusions"].extend(dd_texts)
                elif "Rubric-note" in classes_str or "Rubric-coding-hint" in classes_str:
                    block_info["notes"].extend(dd_texts)
                    
            elif any(c.startswith("Category") for c in classes):
                code_anchors = child.find_all("a", class_="code")
                for code_a in code_anchors:
                    code_id = code_a.get("name")
                    if not code_id:
                        continue
                        
                    category_divs = code_a.find_parents("div", class_=lambda x: x and any(c.startswith("Category") for c in x.split()))
                    if not category_divs:
                        continue
                        
                    outer_container = category_divs[-1]
                    inner_container = category_divs[0]
                    
                    label_span = inner_container.find("span", class_="label")
                    label_text = label_span.get_text(separator=" ", strip=True) if label_span else ""
                    if not label_text:
                        header = inner_container.find(["h1", "h2", "h3", "h4", "h5", "h6"])
                        if header:
                            label_text = header.get_text(separator=" ", strip=True).replace(code_id, "").strip()
                            
                    label_text = " ".join(label_text.split())
                    
                    inclusions = []
                    exclusions = []
                    hints = []
                    
                    for dl in outer_container.find_all("dl"):
                        dl_category_divs = dl.find_parents("div", class_=lambda x: x and any(c.startswith("Category") for c in x.split()))
                        if dl_category_divs and dl_category_divs[-1] == outer_container:
                            dl_classes = dl.get("class", [])
                            dl_classes_str = " ".join(dl_classes)
                            dd_texts = [" ".join(dd.get_text(separator=" ", strip=True).split()) for dd in dl.find_all("dd")]
                            
                            if "Rubric-inclusion" in dl_classes_str:
                                inclusions.extend(dd_texts)
                            elif "Rubric-exclusion" in dl_classes_str:
                                exclusions.extend(dd_texts)
                            elif "Rubric-note" in dl_classes_str or "Rubric-coding-hint" in dl_classes_str:
                                hints.extend(dd_texts)
                                
                    codes.append({
                        "code": code_id,
                        "title": label_text,
                        "class_kind": inner_container.get("class")[0],
                        "inclusions": inclusions,
                        "exclusions": exclusions,
                        "hints": hints
                    })
                    
        return {
            "block_info": block_info,
            "codes": codes
        }

    def crawl(self, limit: int = None):
        progress = self.load_or_create_progress()
        
        # 1. Fetch Chapter HTML details first
        for ch in progress["chapters"]:
            if ch.get("status") == "completed":
                continue
                
            ch_id = ch["id"]
            logger.info(f"Crawling Chapter {ch_id} details...")
            url = f"{BASE_ICD10_URL}/GetConcept?ConceptId={ch_id}"
            
            try:
                html_content = self.fetch_html(url)
                ch_details = self.parse_chapter_html(html_content, ch_id)
                
                ch_file = self.temp_dir / f"chapter_{ch_id}.json"
                with open(ch_file, "w", encoding="utf-8") as f:
                    json.dump({
                        "id": ch_id,
                        "title": ch["title"],
                        "code_range": ch_details.get("code_range", ""),
                        "inclusions": ch_details.get("inclusions", []),
                        "exclusions": ch_details.get("exclusions", []),
                        "notes": ch_details.get("notes", [])
                    }, f, indent=2)
                    
                ch["status"] = "completed"
                self.save_progress(progress)
                
            except Exception as e:
                logger.error(f"Error crawling Chapter {ch_id}: {e}")
                
            time.sleep(self.delay)

        # 2. Fetch Blocks
        pending_blocks = [b for b in progress["blocks"] if b.get("status") != "completed"]
        logger.info(f"Total blocks to crawl: {len(pending_blocks)}")
        
        if limit:
            logger.info(f"Limit option is set. Crawling only first {limit} pending blocks.")
            pending_blocks = pending_blocks[:limit]
            
        success_count = 0
        for idx, bl in enumerate(pending_blocks, 1):
            bl_id = bl["id"]
            logger.info(f"[{idx}/{len(pending_blocks)}] Crawling Block {bl_id} ({bl['title']})...")
            url = f"{BASE_ICD10_URL}/GetConcept?ConceptId={bl_id}"
            
            try:
                html_content = self.fetch_html(url)
                parsed_data = self.parse_block_html(html_content)
                
                parsed_data["block_info"]["id"] = bl_id
                parsed_data["block_info"]["chapter_id"] = bl["chapter_id"]
                
                if not parsed_data["block_info"].get("title"):
                    parsed_data["block_info"]["title"] = bl["title"]
                    
                bl_file = self.temp_dir / f"block_{bl_id}.json"
                with open(bl_file, "w", encoding="utf-8") as f:
                    json.dump(parsed_data, f, indent=2)
                    
                for pb in progress["blocks"]:
                    if pb["id"] == bl_id:
                        pb["status"] = "completed"
                        break
                        
                self.save_progress(progress)
                success_count += 1
                
            except Exception as e:
                logger.error(f"Error crawling Block {bl_id}: {e}")
                
            time.sleep(self.delay)
            
        logger.info(f"Block crawl phase complete. Succeeded: {success_count} blocks.")
        
    def save_progress(self, progress: dict):
        with open(self.progress_file, "w", encoding="utf-8") as f:
            json.dump(progress, f, indent=2)

    def compile_csvs(self):
        """
        Compiles all the block and chapter temp json files into final CSVs.
        """
        logger.info("Compiling temporary files into final ICD-10 CSVs...")
        
        # 1. Compile Chapters
        chapters_csv = self.data_dir / "icd10_chapters.csv"
        chapters = []
        for ch_file in self.temp_dir.glob("chapter_*.json"):
            try:
                with open(ch_file, "r", encoding="utf-8") as f:
                    chapters.append(json.load(f))
            except Exception as e:
                logger.error(f"Error reading chapter temp file {ch_file.name}: {e}")
                
        chapters.sort(key=lambda x: x["id"])
        
        with open(chapters_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["chapter_id", "title", "code_range", "inclusions", "exclusions", "notes"])
            for ch in chapters:
                writer.writerow([
                    ch["id"],
                    ch["title"],
                    ch["code_range"],
                    ";".join(ch["inclusions"]),
                    ";".join(ch["exclusions"]),
                    ";".join(ch["notes"])
                ])
        logger.info(f"Saved {len(chapters)} chapters -> {chapters_csv}")

        # 2. Compile Blocks and Codes
        blocks_csv = self.data_dir / "icd10_blocks.csv"
        codes_csv = self.data_dir / "icd10_codes.csv"
        
        blocks = []
        all_codes = []
        all_codes_set = set()
        
        for bl_file in self.temp_dir.glob("block_*.json"):
            try:
                with open(bl_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    blocks.append(data["block_info"])
                    for c in data["codes"]:
                        c["block_id"] = data["block_info"]["id"]
                        c["chapter_id"] = data["block_info"]["chapter_id"]
                        all_codes.append(c)
                        all_codes_set.add(c["code"])
            except Exception as e:
                logger.error(f"Error reading block temp file {bl_file.name}: {e}")
                
        blocks.sort(key=lambda x: x.get("id", ""))
        
        with open(blocks_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["block_id", "title", "code_range", "chapter_id", "inclusions", "exclusions", "notes"])
            for bl in blocks:
                title = bl.get("title", "")
                title = " ".join(title.split())
                writer.writerow([
                    bl.get("id"),
                    title,
                    bl.get("code_range"),
                    bl.get("chapter_id"),
                    ";".join(bl.get("inclusions", [])),
                    ";".join(bl.get("exclusions", [])),
                    ";".join(bl.get("notes", []))
                ])
        logger.info(f"Saved {len(blocks)} blocks -> {blocks_csv}")

        all_parents_set = set()
        for c in all_codes:
            code = c["code"]
            parent_code = None
            if len(code) > 3 and "." in code:
                parts = code.split(".")
                dot_part = parts[1]
                if len(dot_part) == 1:
                    parent_code = parts[0]
                elif len(dot_part) > 1:
                    parent_code = f"{parts[0]}.{dot_part[:-1]}"
            
            c["parent_code"] = parent_code
            if parent_code:
                all_parents_set.add(parent_code)

        all_codes.sort(key=lambda x: x["code"])

        with open(codes_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "code", "title", "class_kind", "block_id", 
                "chapter_id", "parent_code", "inclusions", 
                "exclusions", "hints", "is_leaf"
            ])
            for c in all_codes:
                is_leaf = c["code"] not in all_parents_set
                writer.writerow([
                    c["code"],
                    c["title"],
                    c["class_kind"],
                    c["block_id"],
                    c["chapter_id"],
                    c["parent_code"] or "",
                    ";".join(c["inclusions"]),
                    ";".join(c["exclusions"]),
                    ";".join(c["hints"]),
                    str(is_leaf)
                ])
        logger.info(f"Saved {len(all_codes)} codes -> {codes_csv}")
        
    def cleanup_temp_files(self):
        """Deletes temporary block and chapter JSON files."""
        logger.info("Cleaning up temporary JSON files...")
        for f in self.temp_dir.glob("*.json"):
            try:
                f.unlink()
            except Exception as e:
                logger.warning(f"Failed to delete temp file {f}: {e}")
        try:
            self.temp_dir.rmdir()
            logger.info("Temporary files cleaned successfully.")
        except Exception as e:
            logger.warning(f"Failed to delete temp directory {self.temp_dir}: {e}")


def download_icd11(data_dir: Path, force: bool = False):
    """
    Downloads and compiles the ICD-11 simple tabulation ZIP.
    """
    logger.info("=" * 60)
    logger.info("          WHO ICD-11 DATA INGESTION PROCESS")
    logger.info("=" * 60)
    
    zip_path = data_dir / "SimpleTabulation-ICD-11-MMS-en.zip"
    extract_dir = data_dir / "icd11_extracted"
    output_csv = data_dir / "icd11_codes.csv"
    
    if output_csv.exists() and not force:
        logger.info(f"ICD-11 CSV already exists at {output_csv}. Skipping download.")
        return
        
    logger.info(f"Downloading ICD-11 Simple Tabulation from {ICD11_ZIP_URL}...")
    try:
        response = requests.get(ICD11_ZIP_URL, headers=HEADERS, stream=True, timeout=60)
        response.raise_for_status()
        
        with open(zip_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024*1024):
                if chunk:
                    f.write(chunk)
        logger.info("Successfully downloaded ICD-11 Simple Tabulation ZIP.")
    except Exception as e:
        logger.error(f"Failed to download ICD-11 ZIP: {e}")
        return

    logger.info("Extracting ZIP archive...")
    extract_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(extract_dir)
        logger.info("Extraction complete.")
    except Exception as e:
        logger.error(f"Failed to extract ZIP: {e}")
        return

    txt_files = list(extract_dir.glob("*.txt"))
    # Filter out readme.txt
    txt_files = [f for f in txt_files if "readme" not in f.name.lower()]
    if not txt_files:
        logger.error("No tab-delimited text file found in the extracted files.")
        return
        
    src_txt_path = txt_files[0]
    logger.info(f"Parsing tab-delimited data file: {src_txt_path.name}")
    
    count = 0
    import re
    with open(src_txt_path, "r", encoding="utf-8") as infile, \
         open(output_csv, "w", newline="", encoding="utf-8") as outfile:
         
        reader = csv.reader(infile, delimiter="\t")
        writer = csv.writer(outfile)
        
        # Read header
        header = next(reader)
        writer.writerow([
            "code", "title", "class_kind", "depth_in_kind", 
            "is_residual", "chapter_no", "block_id", "foundation_uri", 
            "linearization_uri", "browser_link", "is_leaf", "coding_note", "parent"
        ])
        
        for row in reader:
            if not row or len(row) < 5:
                continue
                
            foundation_uri = row[0].strip() if len(row) > 0 else ""
            linearization_uri = row[1].strip() if len(row) > 1 else ""
            code = row[2].strip() if len(row) > 2 else ""
            block_id = row[3].strip() if len(row) > 3 else ""
            title_raw = row[4].strip() if len(row) > 4 else ""
            class_kind = row[5].strip() if len(row) > 5 else ""
            depth_in_kind = row[6].strip() if len(row) > 6 else ""
            is_residual = row[7].strip() if len(row) > 7 else ""
            chapter_no = row[8].strip() if len(row) > 8 else ""
            
            browser_link_raw = row[9].strip() if len(row) > 9 else ""
            browser_link = browser_link_raw
            if "hyperlink" in browser_link_raw:
                url_match = re.search(r'https?://[^\s"]+', browser_link_raw)
                if url_match:
                    browser_link = url_match.group(0)
                    
            is_leaf = row[10].strip() if len(row) > 10 else ""
            
            coding_note = ""
            parent = ""
            if len(row) > 17:
                coding_note = row[17].strip()
            if len(row) > 18:
                parent = row[18].strip()
                
            title = title_raw
            while title.startswith("-") or title.startswith(" "):
                title = title[1:].strip()
                
            writer.writerow([
                code,
                title,
                class_kind,
                depth_in_kind,
                is_residual,
                chapter_no,
                block_id,
                foundation_uri,
                linearization_uri,
                browser_link,
                is_leaf,
                coding_note,
                parent
            ])
            count += 1
            
    logger.info(f"ICD-11 processing complete. Parsed {count} codes -> {output_csv}")

    logger.info("Cleaning up raw zip and extracted text/xlsx files...")
    try:
        zip_path.unlink()
        for f in extract_dir.glob("*"):
            f.unlink()
        extract_dir.rmdir()
        logger.info("ICD-11 temporary files cleaned successfully.")
    except Exception as e:
        logger.warning(f"Error cleaning up ICD-11 temporary files: {e}")


def main():
    import re
    global re
    import re

    parser = argparse.ArgumentParser(description="Download and compile WHO ICD-10 and ICD-11 disease classification standards.")
    parser.add_argument("--output-dir", "-o", default=str(BASE_DIR / "icd_data"), help="Target output folder for CSV files.")
    parser.add_argument("--limit", "-l", type=int, default=None, help="ICD-10: Maximum number of blocks to download (for testing).")
    parser.add_argument("--delay", "-d", type=float, default=1.5, help="Delay in seconds between crawler requests to avoid rate limits.")
    parser.add_argument("--force", "-f", action="store_true", help="Force download and overwrite existing CSVs/checkpoints.")
    parser.add_argument("--icd10-only", action="store_true", help="Download/process only ICD-10.")
    parser.add_argument("--icd11-only", action="store_true", help="Download/process only ICD-11.")
    parser.add_argument("--compile-only", action="store_true", help="Skip crawling and only compile current temp files to ICD-10 CSVs.")
    parser.add_argument("--cleanup-only", action="store_true", help="Clean up any temporary folders and exit.")
    
    args = parser.parse_args()
    
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    
    crawler = ICD10Crawler(out_dir, delay=args.delay, force=args.force)

    if args.cleanup_only:
        crawler.cleanup_temp_files()
        return

    if args.compile_only:
        crawler.compile_csvs()
        return

    run_icd10 = not args.icd11_only
    run_icd11 = not args.icd10_only

    if run_icd10:
        logger.info("=" * 60)
        logger.info("          WHO ICD-10 DATA COLLECTION PROCESS")
        logger.info("=" * 60)
        logger.info(f"Target Output Folder: {out_dir.resolve()}")
        logger.info(f"Request Delay:        {args.delay} seconds")
        logger.info(f"Overwrite Force:      {args.force}")
        if args.limit:
            logger.info(f"Block Limit:          {args.limit}")
            
        try:
            crawler.crawl(limit=args.limit)
            
            if not args.limit:
                crawler.compile_csvs()
                crawler.cleanup_temp_files()
            else:
                logger.info("\nLimit option was used. Temp files preserved. Run without --limit to finalize compilation.")
                
        except Exception as e:
            logger.exception(f"ICD-10 crawl failed or was interrupted: {e}")
            logger.info("\nProgress has been saved. Run the script again to resume from the last checkpoint.")

    if run_icd11:
        download_icd11(out_dir, force=args.force)

    logger.info("=" * 60)
    logger.info("            ICD DATA COLLECTION COMPLETE")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
