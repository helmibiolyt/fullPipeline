# NUPCO Scraper

Scrapes data from the Saudi National Unified Procurement Company (nupco.com).

## Data Collected
- **Unified Catalogue**: 4 Excel files (Pharmaceuticals, Medical Equipment, Medical Supplies, Laboratory) converted to CSV
- **Tenders List**: Active tenders with status, dates, and details
- **Tenders Plan**: Planned tenders across 3 categories
- **News/Posts**: All published news via WordPress REST API

## Setup
```bash
pip install -r requirements.txt
python scraper.py
```

## Output Structure
```
nupco.com/
  Unified Catalogue/   - Downloaded .xlsx files and converted .csv files
  Tenders/             - tenders_list.csv, tenders_plan.csv
  News/                - news.csv
  scraper.log          - Execution log
```

## Skip Logic
Uses `.done_*` marker files to skip completed sections on re-run. Delete markers to re-scrape.
