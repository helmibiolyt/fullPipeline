import os
import sys
import time
import csv
import argparse
from urllib.parse import urlparse, parse_qs, urlencode
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

def build_page_url(base_url, page_num):
    """Replaces or adds the 'page' parameter in the query URL."""
    parsed = urlparse(base_url)
    query_params = parse_qs(parsed.query)
    query_params["page"] = [str(page_num)]
    new_query = urlencode(query_params, doseq=True)
    return parsed._replace(query=new_query).geturl()

def get_total_count(soup):
    """Extracts the total number of trial records from the page span."""
    total_span = soup.find("span", id="data-totalEN")
    if total_span:
        try:
            return int(total_span.get_text(strip=True))
        except ValueError:
            pass
    return None

def fetch_page_content(browser, url):
    """
    Creates an isolated browser context to bypass WAF tracking,
    loads the URL, and returns the HTML content.
    """
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    page = context.new_page()
    
    html_content = ""
    try:
        page.goto(url, wait_until="networkidle", timeout=60000)
        time.sleep(2)  # Allow page JS to settle
        html_content = page.content()
    except Exception as e:
        print(f"  [Error] Failed to fetch {url}: {e}")
    finally:
        context.close()
        
    return html_content

def main():
    parser = argparse.ArgumentParser(description="Manual ChiCTR Clinical Trial ID Scraper")
    parser.add_argument(
        "--pages", 
        type=int, 
        default=5, 
        help="Number of pages to scrape (default: 5)."
    )
    parser.add_argument(
        "--output", 
        type=str, 
        default="chictr_ids.csv", 
        help="Path to the output CSV file (default: chictr_ids.csv)."
    )
    parser.add_argument(
        "--visible", 
        action="store_true", 
        help="Run browser in visible (non-headless) mode to watch the process."
    )
    parser.add_argument(
        "--delay", 
        type=float, 
        default=2.0, 
        help="Delay in seconds between page requests to avoid rate limits (default: 2.0)."
    )
    args = parser.parse_args()

    # Reconfigure stdout to use UTF-8 for clean printing on Windows terminals
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    base_search_url = "https://www.chictr.org.cn/searchprojEN.html"
    all_trials = []
    seen_ids = set()

    print("=" * 60)
    print("  ChiCTR Clinical Trial ID Scraper (From Zero)")
    print(f"  Target URL : {base_search_url}")
    print(f"  Max Pages  : {args.pages}")
    print(f"  Output File: {args.output}")
    print(f"  Browser    : {'Visible (Headful)' if args.visible else 'Headless'}")
    print("=" * 60)

    print("\nLaunching Playwright browser...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.visible)
        
        # Step 1: Open page 1 using a fresh context to check total trials
        first_page_url = build_page_url(base_search_url, 1)
        print(f"Fetching page 1 to check registry size: {first_page_url}")
        page1_html = fetch_page_content(browser, first_page_url)
        
        if not page1_html:
            print("[Critical Error] Could not load page 1. Scraping aborted.")
            browser.close()
            sys.exit(1)

        soup = BeautifulSoup(page1_html, "html.parser")
        total_records = get_total_count(soup)
        if total_records:
            total_pages = (total_records + 9) // 10
            print(f"[OK] Total trials in registry: {total_records:,} (~{total_pages:,} pages)")
            max_pages_to_scrape = min(args.pages, total_pages)
        else:
            print("[Warning] Could not detect total trials count. Scrape limit will be used.")
            max_pages_to_scrape = args.pages

        # Step 2: Loop through pages
        for page_num in range(1, max_pages_to_scrape + 1):
            page_url = build_page_url(base_search_url, page_num)
            print(f"\n[Page {page_num}/{max_pages_to_scrape}] Fetching: {page_url}")
            
            if page_num == 1:
                html = page1_html
            else:
                html = fetch_page_content(browser, page_url)
                
            if not html:
                print("  [Error] No content received for this page. Skipping.")
                continue

            soup = BeautifulSoup(html, "html.parser")
            table = soup.find("table", class_="table1")
            if not table:
                print("  [Error] Could not find results table of class 'table1' on this page.")
                # Save a debug HTML to see what WAF or page error loaded
                debug_file = f"chictr_error_page_{page_num}.html"
                with open(debug_file, "w", encoding="utf-8") as df:
                    df.write(html)
                print(f"  Debug HTML page saved to {debug_file} for inspection.")
                break

            rows = table.find_all("tr")
            page_trials_count = 0
            
            for row in rows:
                tds = row.find_all("td")
                if len(tds) < 5:
                    continue  # Skip header row or incomplete rows

                reg_no = tds[1].get_text(strip=True)
                title_a = tds[2].find("a")
                title = title_a.get_text(strip=True) if title_a else ""
                detail_url = title_a.get("href") if title_a else ""
                
                study_type = tds[3].get_text(strip=True)
                reg_date = tds[4].get_text(strip=True)

                if reg_no and reg_no not in seen_ids:
                    seen_ids.add(reg_no)
                    all_trials.append({
                        "registration_number": reg_no,
                        "public_title": title,
                        "study_type": study_type,
                        "registration_date": reg_date,
                        "detail_url": f"https://www.chictr.org.cn/{detail_url}" if detail_url else ""
                    })
                    page_trials_count += 1

            print(f"  Successfully extracted {page_trials_count} trials from this page.")
            
            # Save progress incrementally to output CSV
            if all_trials:
                with open(args.output, "w", newline="", encoding="utf-8") as f:
                    fieldnames = ["registration_number", "public_title", "study_type", "registration_date", "detail_url"]
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(all_trials)
            
            # Rate limit polite delay
            if page_num < max_pages_to_scrape:
                time.sleep(args.delay)

        browser.close()

    print("\n" + "=" * 60)
    print("  SCRAPING COMPLETE")
    print(f"  Total trials collected: {len(all_trials)}")
    print(f"  CSV file saved to     : {os.path.abspath(args.output)}")
    print("=" * 60)

if __name__ == "__main__":
    main()
