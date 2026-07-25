# clinicaltrials.gov Scraper

This scraper extracts clinical trial details from the official ClinicalTrials.gov API v2.

## What it scrapes
Retrieves all trials registered on ClinicalTrials.gov (or a subset based on criteria) using the official REST API v2. It fetches nested details (Protocol Section and Results availability) and flattens them into a unified CSV dataset.

## Source URLs
- Base API: `https://clinicaltrials.gov/api/v2/studies`

## Output
- `clinicaltrials_data/clinicaltrials_all.csv` - The consolidated CSV file containing flattened trial data.

## Run
```bash
# Run with a limit to verify (fetches 2 pages of 100 trials each)
python clinicaltrials_gov_downloader.py --limit 2 --page-size 100

# Run a full snapshot
python clinicaltrials_gov_downloader.py
```
