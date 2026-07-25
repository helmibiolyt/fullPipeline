"""
anzctr_downloader.py
========================
Automates anzctr.org.au to:
  1. Open the trial search page
  2. Click search (blank search) to retrieve all trials
  3. Wait for results to load
  4. Select "Download ALL ANZCTR trials to Excel" from the download dropdown
  5. Click the "DOWNLOAD" button (with no_wait_after=True to prevent timeout)
  6. Save the downloaded ZIP file
  7. Extract the Excel/PDF files from the ZIP archive
  8. Convert the Excel file to CSV using Pandas and clean up temporary files

Requirements:
    pip install playwright openpyxl pandas
    python -m playwright install chromium
    (Make sure Google Chrome is installed on the system)

Usage:
    python anzctr_downloader.py           # Runs in headless mode (if Cloudflare permits)
    python anzctr_downloader.py --visible # Runs in headful mode (recommended to bypass Turnstile)
"""

import sys
import time
import zipfile
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# -- Configuration -----------------------------------------------------------
BASE_DIR      = Path(__file__).resolve().parent
SEARCH_URL    = "https://www.anzctr.org.au/TrialSearch.aspx"
DOWNLOAD_DIR  = BASE_DIR / "anzctr_trials"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

TIMEOUT_MS      = 60_000   # 60 s for each wait
DOWNLOAD_WAIT_S = 600      # max seconds for ZIP to be generated and downloaded (42k trials)

def run():
    print("=" * 60)
    print("  ANZCTR Registry - Download All Results")
    print(f"  Output folder: {DOWNLOAD_DIR}")
    print("=" * 60)

    # By default, run headful (headless=False) to bypass Cloudflare and allow Turnstile solving.
    # If "--headless" is passed, run headless.
    # Otherwise, check for "--visible" to stay consistent with other scripts.
    headless = True
    if "--visible" in sys.argv:
        headless = False
    elif "--headless" in sys.argv:
        headless = True
    else:
        # Defaulting to headful (False) because Cloudflare Turnstile blocks standard headless.
        # This makes it robust out-of-the-box for the user.
        headless = False

    print(f"Running in {'headless' if headless else 'headful/visible'} mode...")
    if headless:
        print("Note: If Cloudflare blocks the download in headless mode, run with '--visible' to solve the challenge.")

    with sync_playwright() as p:
        # Launch browser using Google Chrome channel for clean fingerprint
        browser = p.chromium.launch(
            channel="chrome",
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            viewport={"width": 1400, "height": 900},
            accept_downloads=True
        )
        page = context.new_page()

        # -- Step 1: Open Search Page ----------------------------------------
        print("\n[1/5] Opening ANZCTR search page...")
        page.goto(SEARCH_URL, timeout=TIMEOUT_MS, wait_until="domcontentloaded")
        time.sleep(2)

        # -- Step 2: Perform blank search ------------------------------------
        print("[2/5] Performing search for all trials...")
        search_btn = page.locator("#ctl00_body_SearchSubmit")
        if search_btn.count() == 0:
            raise RuntimeError("Could not find the search button (#ctl00_body_SearchSubmit)")
        search_btn.click()

        # Wait for results to load
        print("      Waiting for results grid to load (15 seconds)...")
        time.sleep(15)

        # -- Step 3: Configure Download Dropdown -----------------------------
        print("[3/5] Selecting 'Download ALL' option...")
        dropdown = page.locator("#ctl00_body_drpDwnLstDownload")
        if dropdown.count() == 0:
            # Capture debug screenshot in case results didn't load
            screenshot_path = DOWNLOAD_DIR / "debug_search_failed.png"
            page.screenshot(path=str(screenshot_path))
            print(f"      [ERROR] Download dropdown not found! Debug screenshot saved to: {screenshot_path}")
            raise RuntimeError("Download dropdown (#ctl00_body_drpDwnLstDownload) not found. Did results load?")

        dropdown.select_option("all.xls")
        print("      Selected option: Download ALL ANZCTR trials to Excel (all.xls)")
        time.sleep(1)

        # -- Step 4: Click DOWNLOAD and save file ---------------------------
        print("[4/5] Clicking DOWNLOAD and waiting for ZIP generation...")
        download_btn = page.locator("#ctl00_body_btnDownload")
        if download_btn.count() == 0:
            raise RuntimeError("Could not find the download button (#ctl00_body_btnDownload)")

        try:
            # We use no_wait_after=True so Playwright doesn't wait for page navigation
            with page.expect_download(timeout=DOWNLOAD_WAIT_S * 1000) as download_info:
                download_btn.click(no_wait_after=True)
                print("      Download triggered. Generating ZIP on server (this may take a few minutes)...")
            
            download = download_info.value
            suggested_name = download.suggested_filename or "anzctr_export.zip"
            zip_path = DOWNLOAD_DIR / suggested_name
            
            print(f"      Saving ZIP download to: {zip_path}")
            download.save_as(str(zip_path))
            print(f"      [OK] Downloaded ZIP file: {zip_path.name} ({zip_path.stat().st_size / (1024 * 1024):.2f} MB)")
            
        except Exception as e:
            screenshot_path = DOWNLOAD_DIR / "debug_download_failed.png"
            page.screenshot(path=str(screenshot_path))
            print(f"      [ERROR] Download failed: {e}")
            print(f"      Debug screenshot saved to: {screenshot_path}")
            raise

        # -- Step 5: Extract ZIP and convert XLSX/XLS to CSV -----------------
        print("[5/5] Extracting ZIP contents...")
        excel_file_path = None
        pdf_file_path = None
        csv_file_path = None
        
        try:
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                for file_info in zip_ref.infolist():
                    filename = file_info.filename
                    if filename.endswith(('.xlsx', '.xls')):
                        excel_file_path = DOWNLOAD_DIR / filename
                        print(f"      Found Excel file in ZIP: {filename}")
                        zip_ref.extract(file_info, path=DOWNLOAD_DIR)
                    elif filename.endswith('.pdf'):
                        pdf_file_path = DOWNLOAD_DIR / filename
                        print(f"      Found PDF file in ZIP: {filename}")
                        zip_ref.extract(file_info, path=DOWNLOAD_DIR)
                    elif filename.endswith('.csv'):
                        csv_file_path = DOWNLOAD_DIR / filename
                        print(f"      Found CSV file in ZIP: {filename}")
                        zip_ref.extract(file_info, path=DOWNLOAD_DIR)

            if excel_file_path:
                # Convert the Excel file to a consistently named CSV
                csv_file_path = DOWNLOAD_DIR / "anzctr_trials.csv"
                print(f"      Converting Excel to CSV: {excel_file_path.name} -> {csv_file_path.name}...")
                
                import pandas as pd
                df = pd.read_excel(excel_file_path, engine='openpyxl')
                df.to_csv(csv_file_path, index=False)
                print(f"      Successfully converted! Shape: {df.shape}")
                
                # Delete temporary Excel file
                try:
                    excel_file_path.unlink()
                    print(f"      Cleaned up temporary Excel file: {excel_file_path.name}")
                except Exception as ex:
                    print(f"      (warning: failed to delete Excel file: {ex})")
            
            # Clean up the ZIP file
            try:
                zip_path.unlink()
                print("      Cleaned up temporary ZIP archive.")
            except Exception as ex:
                print(f"      (warning: failed to delete temporary ZIP: {ex})")

            if not csv_file_path or not csv_file_path.exists():
                raise RuntimeError("No trial data file (CSV or Excel) was successfully extracted or converted.")

            print("\n" + "=" * 60)
            print("  *  DOWNLOAD AND CONVERSION COMPLETE")
            print(f"  Final CSV : {csv_file_path}")
            print(f"  Final PDF : {pdf_file_path if pdf_file_path else 'None'}")
            print(f"  CSV Size  : {csv_file_path.stat().st_size / (1024 * 1024):.2f} MB")
            print("=" * 60)

        except Exception as e:
            print(f"      [ERROR] Extraction/Conversion failed: {e}")
            raise

        browser.close()

if __name__ == "__main__":
    run()
