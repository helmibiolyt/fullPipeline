# euclinicaltrials.eu Scraper

This scraper extracts clinical trial details from the Clinical Trials Information System (CTIS) public API.

## What it scrapes
Extracts trials from the CTIS public portal (`euclinicaltrials.eu`). By default, it retrieves the comprehensive search results details for all trials. An optional `--full` flag retrieves granular detail sections (such as inclusion/exclusion criteria) for each trial from the retrieve endpoint.

## Source URLs
- Search Endpoint: `https://euclinicaltrials.eu/ctis-public-api/search`
- Retrieve Endpoint: `https://euclinicaltrials.eu/ctis-public-api/retrieve/{trial_id}`

## Output
- `ctis_data/ctis_all.csv` - The consolidated CSV file containing flattened trial data.

## Run
```bash
# Run a test crawl (1 page of 5 trials)
python eu_ctis_downloader.py --limit 1 --page-size 5

# Run a full snapshot of search data (recommended/fast, retrieves ~12,000+ trials in ~2 mins)
python eu_ctis_downloader.py

# Run a full snapshot including retrieval of detailed inclusion/exclusion criteria (slow, ~3+ hours)
python eu_ctis_downloader.py --full
```
