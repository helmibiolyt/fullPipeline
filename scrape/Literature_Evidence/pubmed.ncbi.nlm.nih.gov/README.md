# pubmed.ncbi.nlm.nih.gov

Crawls biomedical literature metadata from PubMed/MEDLINE using the official NCBI E-utilities API.
Supports query translation, cursor-based resume via Entrez history server context, and duplicate checking.

## What it scrapes
- **Metadata** for articles matching a biomedical search query (PMID, DOI, PMCID, title, abstract, authors, affiliations, journal title, journal ISSN, volume, issue, publication date, publication year, publication type, keywords).

## Source URLs
- https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi — search endpoint (caching results on server via `usehistory=y`)
- https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi — fetch endpoint (retrieves raw XML details in batches)

## Output
Everything is written into `pubmed/`:
- `pubmed_metadata.csv` — one row per article (metadata).
- `pubmed_progress.json` — cursor/resume checkpoint.
- `pubmed_downloader.log` — run log.

Output format: **CSV**.

## Run
Run the script from the root folder:
```bash
pip install -r requirements.txt
python pubmed_downloader.py --query "clinical trials breast cancer" --limit 100
```

Useful options:
- `--query`: Search query (default: standard oncology/clinical queries).
- `--limit`: Maximum number of records to retrieve (default: 1000). Set to 0 or negative for unlimited.
- `--page-size`: Batch size for EFetch requests (default: 200).
- `--delay`: Polite delay in seconds between NCBI E-utilities API requests (default: 0.35s).
- `--api-key`: NCBI API key to increase rate limits to 10 requests/sec.
- `--email`: Contact email address (recommended by NCBI).
- `--fresh`: Start a fresh search, ignoring any previous checkpoint.
- `--output-dir`: Output directory path (default: `pubmed`).
- `--verbose`: Enable debug logs.
