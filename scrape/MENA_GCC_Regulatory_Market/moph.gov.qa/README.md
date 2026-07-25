# Qatar MOPH Scraper

Scrapes publicly available pharmaceutical and health data from the Qatar Ministry of Public Health (MOPH) and related portals.

## Data Sources

| Source | URL | Status |
|--------|-----|--------|
| Priced Products (XLSX) | www.moph.gov.qa | Behind WAF - may fail |
| Open Data (data.gov.qa) | data.gov.qa API | Public API - reliable |
| DHP Practitioners | dhp.moph.gov.qa | SharePoint - may fail |
| Pharmacist Guidelines (PDF) | dhp.moph.gov.qa | Direct download |

## Setup

```bash
pip install -r requirements.txt
python scraper.py
```

## Output Structure

```
Drug Pricing/          - Priced products XLSX + CSV conversion
Open Data/             - Health datasets from data.gov.qa as CSV files
Licensed Practitioners/ - DHP practitioner search results
Guidelines/            - Pharmacist guidelines PDF + CSV extraction
```

## Known Limitations

- **WAF/CAPTCHA:** The main MOPH site (www.moph.gov.qa) uses Imperva WAF which blocks automated requests. The priced products download may require manual browser download.
- **QNF:** The Qatar National Formulary (qnf.moph.gov.qa) is a commercial Wolters Kluwer/Lexicomp platform and is not scraped.
- **Drug Registration Portal:** eservices.moph.gov.qa/dps/ requires authentication and is not scraped.
- **SharePoint:** The DHP practitioners search is SharePoint-based and may require JavaScript rendering.

## Logs

Check `scraper.log` for detailed execution logs.
