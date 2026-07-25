"""
isrctn_downloader.py
========================
Automates isrctn.com to:
  1. Open the homepage
  2. Accept cookies if the banner appears
  3. Click "View all studies" to go to the search results page
  4. Click "Export as CSV" to open the export options modal
  5. Check "Select All" to include all data fields in the export
  6. Click the "Export" button and wait for the download to complete
  7. Save the CSV file to the "Clinical Trials & Pipeline Intelligence/isrctn_trials" directory

Requirements:
    pip install playwright
    python -m playwright install chromium
"""

import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# -- Configuration -----------------------------------------------------------
BASE_DIR      = Path(__file__).resolve().parent
HOMEPAGE      = "https://www.isrctn.com/"
DOWNLOAD_DIR  = BASE_DIR / "isrctn_trials"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

TIMEOUT_MS      = 60_000   # 60 s for each wait
DOWNLOAD_WAIT_S = 300      # max seconds for CSV to be generated and downloaded


def run():
    print("=" * 60)
    print("  ISRCTN Registry - Download All Results")
    print(f"  Output folder: {DOWNLOAD_DIR}")
    print("=" * 60)

    with sync_playwright() as p:

        # Launch browser (headless by default for background compatibility, use --visible flag to run with window)
        headless = "--visible" not in sys.argv
        browser = p.chromium.launch(
            headless=headless,
        )
        context = browser.new_context(
            accept_downloads=True,
            viewport={"width": 1400, "height": 900},
        )
        page = context.new_page()

        # -- Step 1: Homepage ------------------------------------------------
        print("\n[1/6] Visiting homepage...")
        page.goto(HOMEPAGE, timeout=TIMEOUT_MS, wait_until="domcontentloaded")
        time.sleep(2)

        # -- Step 2: Accept cookies ------------------------------------------
        print("[2/6] Checking for cookie banner...")
        try:
            cookie_btn = page.locator("button:has-text('Accept all cookies')")
            if cookie_btn.count() > 0:
                cookie_btn.click()
                print("       [OK] Accepted cookies")
                time.sleep(1)
            else:
                print("       (no cookie banner found, continuing)")
        except Exception as e:
            print(f"       (error checking cookie banner: {e}, continuing)")

        # -- Step 3: Go to search results page -------------------------------
        print("[3/6] Navigating to search results page...")
        try:
            view_all_link = page.locator("a:has-text('View all studies')").first
            view_all_link.click()
            print("       [OK] Clicked 'View all studies'")
        except Exception as e:
            print(f"       Could not click 'View all studies': {e}")
            print(f"       Directly navigating to search page instead...")
            page.goto(f"{HOMEPAGE}search?q=", timeout=TIMEOUT_MS, wait_until="domcontentloaded")
            
        time.sleep(3)

        # -- Step 4: Click 'Export as CSV' -----------------------------------
        print("[4/6] Opening Export as CSV options modal...")
        try:
            export_csv_link = page.locator("a:has-text('Export as CSV')").first
            export_csv_link.click()
            print("       [OK] Clicked 'Export as CSV'")
        except Exception as e:
            raise RuntimeError(f"Failed to find or click 'Export as CSV': {e}")
            
        time.sleep(2)

        # -- Step 5: Check 'Select All' in modal ----------------------------
        print("[5/6] Selecting all export fields...")
        try:
            checkboxes = page.locator("input[type='checkbox']").all()
            select_all_clicked = False
            for cb in checkboxes:
                parent_text = cb.evaluate("el => el.parentElement.innerText").strip()
                if "Select All" in parent_text:
                    cb.click()
                    print("       [OK] Checked 'Select All'")
                    select_all_clicked = True
                    break
            
            if not select_all_clicked:
                print("       [WARNING] Could not find 'Select All' checkbox by text, checking first checkbox as fallback")
                if len(checkboxes) > 0:
                    checkboxes[0].click()
        except Exception as e:
            raise RuntimeError(f"Failed to configure export checkboxes: {e}")
            
        time.sleep(1)

        # -- Step 6: Click Export and download file -------------------------
        print("[6/6] Clicking 'Export' and downloading file...")
        try:
            export_confirm_btn = page.locator("button:has-text('Export')").first
            if export_confirm_btn.count() > 0:
                with page.expect_download(timeout=DOWNLOAD_WAIT_S * 1000) as download_info:
                    export_confirm_btn.click()
                    print("       [OK] Clicked Export button, waiting for generation...")
                
                download = download_info.value
                suggested_name = download.suggested_filename or "isrctn_export.csv"
                dest_path = DOWNLOAD_DIR / suggested_name
                
                print(f"       Saving download to: {dest_path}")
                download.save_as(str(dest_path))
                
                print("\n" + "=" * 60)
                print("  *  DOWNLOAD COMPLETE")
                print(f"  File : {dest_path}")
                print(f"  Size : {dest_path.stat().st_size / (1024 * 1024):.2f} MB")
                print("=" * 60)
            else:
                raise RuntimeError("Could not find 'Export' confirmation button in modal")
        except Exception as e:
            # Capture debug screenshot in case of failure
            screenshot_path = DOWNLOAD_DIR / "debug_download_failed.png"
            page.screenshot(path=str(screenshot_path))
            print(f"       [ERROR] Export failed: {e}")
            print(f"       Debug screenshot saved to: {screenshot_path}")
            raise

        # Give user a moment to see the browser before it closes
        time.sleep(3)
        context.close()
        browser.close()


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run()
