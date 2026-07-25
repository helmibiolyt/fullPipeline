# DHA (Dubai Health Authority) Scraper

## Data Sources

| Source | URL | Format |
|--------|-----|--------|
| Drug Price List | dha.gov.ae/uploads/022022/PriceList... | XLSX |
| Regulatory Circulars | dha.gov.ae/licensing-regulations-circulars | HTML (6 pages) |
| Drug Control Dept | dha.gov.ae/HealthRegulationSector/DrugControl | HTML + PDFs |
| Pharmaceutical Policies | dha.gov.ae/licensing-regulations-policies | PDFs |
| Clinical Guidelines | dha.gov.ae/licensing-regulations-clinical-guidelines | PDFs |

## Setup

```bash
pip install -r requirements.txt
python scraper.py
```

## Output Structure

```
dha.gov.ae/
  Drug Pricing/          - Price list Excel + CSV conversion
  Circulars/             - circulars.csv with all regulatory circulars
  Drug Control/          - Forms, checklists, policies (PDF + CSV)
  Policies/              - Pharmaceutical policy PDFs + CSV
  Guidelines/            - Clinical guideline PDFs + CSV
  scraper.log            - Execution log
```

## Features

- Session management with cookie initialization
- Skip logic using `.done_` marker files (re-run safely)
- Retry mechanism (3 retries with exponential backoff)
- Excel to CSV conversion (sheets merged by column count)
- PDF table extraction to CSV (text fallback if no tables)
- 0.5s delay between requests

## Known Limitations

- Licensed Facilities endpoint returns 500 errors
- Licensed Professionals directory requires JS rendering (not supported)
- Open Data portal has low pharma relevance (statistical yearbooks only)
- Circular detail pages may not always be accessible
- Drug Price List URL may change over time
