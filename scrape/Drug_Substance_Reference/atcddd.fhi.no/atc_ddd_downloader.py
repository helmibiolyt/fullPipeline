#!/usr/bin/env python3
"""
WHO ATC/DDD Database Downloader and Parser
Author: Antigravity AI Coding Assistant
Description: Recursively crawls the official WHO Collaborating Centre for Drug Statistics Methodology
             website (https://atcddd.fhi.no) to extract the complete therapeutic classification
             hierarchy (Levels 1 to 4) and Defined Daily Doses (Level 5 substances).
"""

import os
import sys
import time
import re
import csv
import json
import argparse
import collections
from pathlib import Path
import requests
from bs4 import BeautifulSoup

# Resolve output paths relative to this script, never the current working dir.
BASE_DIR = Path(__file__).resolve().parent

# Standard WHO ATC Level 1 codes
LEVEL1_CODES = ["A", "B", "C", "D", "G", "H", "J", "L", "M", "N", "P", "R", "S", "V"]

# HTTP Headers for crawling
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive",
}

# Regex to match ATC codes of different levels
LEVEL_REGEXES = {
    1: re.compile(r'^[A-Z]$'),
    2: re.compile(r'^[A-Z]\d{2}$'),
    3: re.compile(r'^[A-Z]\d{2}[A-Z]$'),
    4: re.compile(r'^[A-Z]\d{2}[A-Z]{2}$'),
    5: re.compile(r'^[A-Z]\d{2}[A-Z]{2}\d{2}$')
}

def get_level(code):
    """
    Determines the ATC level (1 to 5) of a given code based on its pattern.
    """
    for level, regex in LEVEL_REGEXES.items():
        if regex.match(code):
            return level
    return None

def extract_description(b_tag):
    """
    Extracts the guideline descriptions or grey note tables following a <b> tag.
    Traverses the siblings of the <b> tag until it hits the next code or table/list.
    """
    desc_parts = []
    sibling = b_tag.next_sibling
    while sibling:
        if sibling.name == 'b':
            # Next code started, stop
            break
        if sibling.name == 'ul':
            # Substance list started, stop
            break
        if sibling.name == 'table':
            # Grey notes table has bgcolor=#cccccc
            if sibling.get('bgcolor') == '#cccccc':
                text = sibling.get_text(strip=True).replace('\xa0', '').replace('', '')
                if text:
                    desc_parts.append(f"[Note Table: {text}]")
            else:
                # Other tables (substances table), stop
                break
        elif sibling.name == 'p':
            # Skip navigation or standard footer paragraphs
            if sibling.get('align') != 'right' and 'List of abbreviations' not in sibling.text:
                text = sibling.get_text(strip=True).replace('', '')
                if text:
                    desc_parts.append(text)
        sibling = sibling.next_sibling
    return "\n".join(desc_parts).strip()

def parse_hierarchy_on_page(soup):
    """
    Parses all the classification classes (Levels 1 to 4) shown in the hierarchy on the page.
    Every page displays the hierarchy leading up to the current group.
    """
    results = []
    content_div = soup.find(id="content")
    if not content_div:
        return results
    
    b_tags = content_div.find_all("b")
    for b in b_tags:
        a = b.find("a", href=True)
        if a and "code=" in a["href"]:
            prev = b.previous_sibling
            if prev:
                # Clean up the code string
                code = str(prev).strip().replace('\xa0', '').replace('', '')
                code = re.sub(r'[^A-Z0-9]', '', code)
                level = get_level(code)
                if level and level < 5:
                    name = a.get_text(strip=True).replace('', '')
                    
                    # Compute parent code
                    parent_code = ""
                    if level == 2:
                        parent_code = code[0]
                    elif level == 3:
                        parent_code = code[:3]
                    elif level == 4:
                        parent_code = code[:4]
                        
                    description = extract_description(b)
                    
                    results.append({
                        'code': code,
                        'level': level,
                        'name': name,
                        'parent_code': parent_code,
                        'description': description
                    })
    return results

def parse_substances_on_page(soup):
    """
    Parses the substance table (Level 5) on the page if present.
    """
    substances = []
    tables = soup.find_all("table")
    for table in tables:
        rows = table.find_all("tr")
        if not rows:
            continue
            
        # Verify if this is the substance table by checking if any row contains a Level 5 code
        is_substance_table = False
        for row in rows:
            cells = row.find_all(["td", "th"])
            if cells:
                first_cell_text = cells[0].get_text(strip=True).replace('\xa0', '').replace('', '')
                if LEVEL_REGEXES[5].match(first_cell_text):
                    is_substance_table = True
                    break
                    
        if not is_substance_table:
            continue
            
        # Parse the substance rows
        for row in rows:
            cells = row.find_all(["td", "th"])
            if not cells or len(cells) < 2:
                continue
                
            code = cells[0].get_text(strip=True).replace('\xa0', '').replace('', '')
            if not LEVEL_REGEXES[5].match(code):
                # Skip header, footer, or spacer rows
                continue
                
            name = cells[1].get_text(strip=True).replace('', '')
            
            # Extract DDD, Unit, Route, and Note
            ddd = cells[2].get_text(strip=True).replace('\xa0', '').replace('', '') if len(cells) > 2 else ""
            unit = cells[3].get_text(strip=True).replace('\xa0', '').replace('', '') if len(cells) > 3 else ""
            adm_route = cells[4].get_text(strip=True).replace('\xa0', '').replace('', '') if len(cells) > 4 else ""
            note = cells[5].get_text(strip=True).replace('\xa0', '').replace('', '') if len(cells) > 5 else ""
            
            substances.append({
                'code': code,
                'name': name,
                'ddd': ddd,
                'unit': unit,
                'adm_route': adm_route,
                'note': note,
                'parent_code': code[:5]  # Parent is the Level 4 code prefix
            })
    return substances

def find_child_codes(soup, current_code):
    """
    Finds all links to direct child subgroups on the page.
    """
    current_level = get_level(current_code)
    if current_level is None or current_level >= 4:
        return []
        
    child_level = current_level + 1
    children = set()
    
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "code=" in href:
            parts = href.split("code=")
            if len(parts) > 1:
                code_param = parts[1].split("&")[0]
                code_param = code_param.strip().upper()
                if code_param.startswith(current_code) and get_level(code_param) == child_level:
                    children.add(code_param)
                    
    return sorted(list(children))

def build_json_tree(classes, substances):
    """
    Assembles a nested JSON tree from the classes and substances dictionaries.
    """
    nodes = {}
    
    # Initialize all class nodes
    for code, item in classes.items():
        nodes[code] = {
            "code": code,
            "name": item["name"],
            "level": item["level"],
            "description": item["description"],
            "children": []
        }
        
    # Initialize all substance nodes
    for code, item in substances.items():
        nodes[code] = {
            "code": code,
            "name": item["name"],
            "level": 5,
            "ddd": item["ddd"],
            "unit": item["unit"],
            "adm_route": item["adm_route"],
            "note": item["note"]
        }
        
    roots = []
    # Sort keys by length so we process parents before children
    sorted_codes = sorted(nodes.keys(), key=len)
    
    for code in sorted_codes:
        node = nodes[code]
        level = node["level"]
        
        if level == 1:
            roots.append(node)
        else:
            if level == 5:
                parent_code = code[:5]
            elif level == 4:
                parent_code = code[:4]
            elif level == 3:
                parent_code = code[:3]
            elif level == 2:
                parent_code = code[0]
            else:
                parent_code = None
                
            if parent_code and parent_code in nodes:
                nodes[parent_code].setdefault("children", []).append(node)
            else:
                roots.append(node)
                
    return roots

def main():
    parser = argparse.ArgumentParser(description="Scrape official WHO ATC/DDD database.")
    parser.add_argument("--output-dir", default=str(BASE_DIR / "atc_ddd_data"), help="Directory to save the outputs (default: BASE_DIR/atc_ddd_data)")
    parser.add_argument("--delay", type=float, default=0.2, help="Delay between requests in seconds (default: 0.2)")
    parser.add_argument("--max-retries", type=int, default=5, help="Maximum retries for failed requests (default: 5)")
    parser.add_argument("--limit-branch", choices=LEVEL1_CODES, help="Limit crawl to a single Level 1 branch (e.g. V) for testing")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    args = parser.parse_args()
    
    os.makedirs(args.output_dir, exist_ok=True)
    
    # Determine starting codes
    start_codes = [args.limit_branch] if args.limit_branch else LEVEL1_CODES
    
    print("=" * 60)
    print("  WHO ATC/DDD DATABASE DOWNLOADER")
    print(f"  Target Website : https://atcddd.fhi.no")
    print(f"  Output Dir     : {args.output_dir}")
    print(f"  Politeness Del : {args.delay}s")
    print(f"  Max Retries    : {args.max_retries}")
    if args.limit_branch:
        print(f"  Limit Branch   : {args.limit_branch} (Testing mode)")
    print("=" * 60)
    
    session = requests.Session()
    queue = collections.deque(start_codes)
    visited = set()
    
    classes_data = {}
    substances_data = {}
    pages_crawled = 0
    
    try:
        while queue:
            code = queue.popleft()
            if code in visited:
                continue
                
            visited.add(code)
            pages_crawled += 1
            
            url = f"https://atcddd.fhi.no/atc_ddd_index/?code={code}"
            
            if args.verbose:
                print(f"[{pages_crawled}] Fetching {code:<8} | Queue size: {len(queue):<4} | URL: {url}")
            else:
                print(f"Fetching code: {code:<8} | Queue size: {len(queue):<4} | Crawled pages: {pages_crawled}", end="\r")
                
            # Request with retry logic
            html = None
            for attempt in range(1, args.max_retries + 1):
                try:
                    resp = session.get(url, headers=HEADERS, timeout=20)
                    if resp.status_code == 200:
                        html = resp.content
                        break
                    elif resp.status_code == 429:
                        wait_time = attempt * 10
                        print(f"\n  [!] HTTP 429 Rate Limit. Waiting {wait_time}s (attempt {attempt}/{args.max_retries})...")
                        time.sleep(wait_time)
                    else:
                        print(f"\n  [!] HTTP {resp.status_code} for {code}. Retrying in {attempt * 2}s...")
                        time.sleep(attempt * 2)
                except Exception as e:
                    print(f"\n  [!] Network error fetching {code}: {e}. Retrying in {attempt * 2}s...")
                    time.sleep(attempt * 2)
                    
            if not html:
                print(f"\n  [X] Failed to fetch {code} after {args.max_retries} attempts.")
                continue
                
            soup = BeautifulSoup(html, "html.parser")
            
            # 1. Parse hierarchy classes (Levels 1 to 4) shown on the page
            page_classes = parse_hierarchy_on_page(soup)
            for item in page_classes:
                c_code = item['code']
                if c_code in classes_data:
                    # Update description if the new one is longer/more detailed
                    if len(item['description']) > len(classes_data[c_code]['description']):
                        classes_data[c_code]['description'] = item['description']
                    if item['name'] and not classes_data[c_code]['name']:
                        classes_data[c_code]['name'] = item['name']
                else:
                    classes_data[c_code] = item
                    
            # 2. Parse substances (Level 5) from table if present
            page_substances = parse_substances_on_page(soup)
            for sub in page_substances:
                sub_code = sub['code']
                substances_data[sub_code] = sub
                
            # 3. Find child codes to queue
            current_level = get_level(code)
            if current_level and current_level < 4:
                children = find_child_codes(soup, code)
                for child in children:
                    if child not in visited and child not in queue:
                        queue.append(child)
                        
            # Politeness delay
            if args.delay > 0:
                time.sleep(args.delay)
                
    except KeyboardInterrupt:
        print("\n\n[!] Crawling interrupted by user. Saving collected data so far...")
        
    print(f"\n\nCrawl complete. Pages visited: {pages_crawled}")
    print(f"Unique classes collected (Levels 1-4)  : {len(classes_data)}")
    print(f"Unique substances collected (Level 5) : {len(substances_data)}")
    
    # --- Exporters ---
    print("\nSaving datasets...")
    
    # 1. Save atc_classes.csv
    classes_file = os.path.join(args.output_dir, "atc_classes.csv")
    with open(classes_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["atc_code", "level", "name", "parent_code", "description"])
        # Sort by code for neatness
        for code in sorted(classes_data.keys()):
            item = classes_data[code]
            writer.writerow([item["code"], item["level"], item["name"], item["parent_code"], item["description"]])
    print(f"  Saved -> {classes_file}")
            
    # 2. Save atc_substances.csv
    substances_file = os.path.join(args.output_dir, "atc_substances.csv")
    with open(substances_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["atc_code", "name", "ddd", "unit", "adm_route", "note", "parent_code"])
        for code in sorted(substances_data.keys()):
            item = substances_data[code]
            writer.writerow([item["code"], item["name"], item["ddd"], item["unit"], item["adm_route"], item["note"], item["parent_code"]])
    print(f"  Saved -> {substances_file}")
            
    # 3. Save atc_ddd_full.csv (Unified and Denormalized)
    full_file = os.path.join(args.output_dir, "atc_ddd_full.csv")
    with open(full_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["atc_code", "level", "name", "parent_code", "ddd", "unit", "adm_route", "note", "description"])
        
        # Write classes
        for code in sorted(classes_data.keys()):
            item = classes_data[code]
            writer.writerow([item["code"], item["level"], item["name"], item["parent_code"], "", "", "", "", item["description"]])
            
        # Write substances
        for code in sorted(substances_data.keys()):
            item = substances_data[code]
            writer.writerow([item["code"], 5, item["name"], item["parent_code"], item["ddd"], item["unit"], item["adm_route"], item["note"], ""])
    print(f"  Saved -> {full_file}")
            
    # 4. Clean up any JSON files if present
    json_file = os.path.join(args.output_dir, "atc_hierarchy.json")
    if os.path.exists(json_file):
        try:
            os.remove(json_file)
            print(f"  Cleaned up -> {json_file}")
        except Exception as e:
            print(f"  Warning: Could not remove {json_file}: {e}")

    
    # Print sample using pandas if installed, otherwise show summary
    try:
        import pandas as pd
        print("\n--- Output Datasets Sample Preview ---")
        df_classes = pd.read_csv(classes_file)
        df_substances = pd.read_csv(substances_file)
        print("\nClasses (First 5 rows):")
        print(df_classes.head())
        print("\nSubstances (First 5 rows):")
        print(df_substances.head())
    except ImportError:
        print("\n(Install pandas to see inline previews of the saved files)")
        
    print("\n=" * 60)
    print("  COLLECTION PROCESS COMPLETE")
    print("=" * 60)

if __name__ == "__main__":
    main()
