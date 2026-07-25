# SFDA Data Scraper

Extracts all publicly available datasets from the Saudi Food and Drug Authority
(SFDA) website for two sections:

| Section | Source URL |
|---|---|
| Drugs | `https://www.sfda.gov.sa/en/lists-categories?keys=&tags=2` |
| Clinical Trials | `https://www.sfda.gov.sa/en/lists-categories?keys=&tags=3` |

For each discovered dataset the script:
1. Automatically discovers dataset URLs by walking the paginated listing pages.
2. Detects the table schema from the page HTML dynamically.
3. Walks all pagination pages to collect every record.
4. Falls back to download links (Excel/CSV) when pages use JavaScript rendering.
5. Writes one CSV file per dataset, with `# Source URL` and `# Extraction Date`
   metadata rows at the top.

---

## Requirements

```
Python 3.8+
requests>=2.28
beautifulsoup4>=4.11
lxml>=4.9
openpyxl>=3.0   # for Excel download fallback (optional but recommended)
```

Install with:

```bash
pip install requests beautifulsoup4 lxml openpyxl
```

---

## Usage

### Scrape everything (both sections, all datasets)

```bash
python sfda_scraper.py
```

CSV files are written to `sfda_data/` in the current directory.

### Custom output directory

```bash
python sfda_scraper.py -o my_output_folder
```

### Slower crawl (more polite, avoids rate limits)

```bash
python sfda_scraper.py -d 2.0        # 2-second delay between requests
```

### Single dataset by URL slug

```bash
python sfda_scraper.py --dataset safety-alert
python sfda_scraper.py --dataset new-sfda-drug-approvals
python sfda_scraper.py --dataset clinical-trials-list
python sfda_scraper.py --dataset drug-clinical-trials-list
python sfda_scraper.py --dataset drugs-safety-labeling
```

---

## Output

One CSV per dataset, named after the dataset title.  
First three rows are metadata:

```
# Source URL: https://www.sfda.gov.sa/en/safety-alert
# Extraction Date: 2026-06-29
# Total Records: 259
#,Title,Type of news,Date,Link of news
257,Safety Signal of Cefadroxil...,Signal,2026-06-17,https://...
```

Files use UTF-8 with BOM (`utf-8-sig`) for compatibility with Excel.

---

## Datasets discovered (as of 2026-06-29)

### Drugs section (19 datasets)

| Dataset | URL slug | Status |
|---|---|---|
| Results and Studies of Marketed Pharmaceutical Products | `marketed-products` | ✓ scraped |
| Drug Approvals | `new-sfda-drug-approvals` | ✓ scraped (modal content extracted) |
| List of available drugs in reference warehouses | `reference-repositories-drugs-list` | ✓ scraped |
| Hospitals that reported side effects | `list-of-hospitals-that-reported-sides-effects` | ⚠ empty (no data) |
| Drugs Safety Labeling Updates | `drugs-safety-labeling` | ✓ scraped |
| Incentive Project List | `unregistered-pharmaceuticals-list` | ⚠ requires JS / download |
| Drug Companies List | `drug-companies` | ⚠ requires JS |
| Release Vaccine | `released-vaccines` | ⚠ requires JS |
| Drugs Under Studying List | `underStudyingList` | ⚠ empty (no data) |
| Saudi Public Assessment Report | `drug-evaluation-reports` | ✓ scraped |
| Risk Minimization Measures List | `RMM` | ✓ scraped |
| International Standards Lists | `international-standards-lists` | varies |
| Saudi Standards Lists | `saudi-standards-lists` | varies |
| Licensed Pharmacies List | `list-registered-pharmacies` | ✓ scraped |
| Safety Alert | `safety-alert` | ✓ scraped |
| Drug Clinical Trials List | `drug-clinical-trials-list` | ✓ scraped |
| Pharmacovigilance | `pharmacovigilance` | ✓ scraped |
| Shortage Drugs List | `currentlyInShortageList` | ⚠ requires JS |
| List of registered human/herbal/veterinary drugs | `drugs-list` | ⚠ requires JS |

### Clinical Trials section (5 datasets)

| Dataset | URL slug | Status |
|---|---|---|
| Clinical Trials List | `clinical-trials-list` | ✓ scraped (167 records) |
| International Standards Lists | `international-standards-lists` | varies |
| Saudi Standards Lists | `saudi-standards-lists` | varies |
| Weekly Alert | `weekly-alert` | varies |
| Medical Equipment List | `medical-equipment-list` | varies |

Pages marked ⚠ `requires JS` render their data via JavaScript/AJAX after page
load; standard HTTP requests return an empty "Displaying 0 - 0 of 0" table.
The scraper attempts a download-link fallback for these pages automatically.
To scrape them fully, use a headless browser (Playwright/Selenium).

---

## Architecture

```
sfda_scraper.py
└── SFDAScraper
    ├── discover_categories()   walk listing pages → list of {title, url}
    ├── parse_table()           extract headers + rows from an HTML table
    │   └── _cell_text()        handle modal triggers; extract full popup text
    ├── last_page_number()      detect total pages from Drupal pager
    ├── has_next_page()         check for "next" link
    ├── try_download()          Excel/CSV download-link fallback
    └── write_csv()             UTF-8 CSV with metadata header rows
```

Key design choices:
- `recursive=False` in BeautifulSoup prevents nested modal tables from being
  mistaken for data rows or extra columns.
- Drupal `views-field-nothing` columns (modal containers) are automatically
  skipped; their modal body text is extracted into the preceding data column.
- Absolute and relative dataset URLs are both handled in discovery.
- Rate-limited by `--delay` (default 1.2 s) to avoid overwhelming the server.
