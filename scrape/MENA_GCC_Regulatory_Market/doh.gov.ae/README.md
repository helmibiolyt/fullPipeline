# DOH Abu Dhabi Scraper

Scrapes pharmaceutical-related documents from the Abu Dhabi Department of Health (doh.gov.ae).

## Sources Scraped
1. **Circulars** - via RSS feed (drug recalls, safety alerts, regulatory notices)
2. **Policies & Manuals** - medication safety, quarantine/recall, reimbursement policies
3. **Medications & Supplements** - educational awareness materials
4. **Guidelines** - clinical/regulatory guidelines (JS-rendered, best-effort)
5. **Standards** - healthcare standards (JS-rendered, best-effort)

## Setup
```bash
pip install -r requirements.txt
python scraper.py
```

## Output Structure
```
doh.gov.ae/
  Circulars/          # PDFs + circulars.csv index
  Policies/           # PDFs + policies_index.csv
  Medications & Supplements/  # PDFs + CSVs
  Guidelines/         # PDFs if available
  Standards/          # PDFs if available
  scraper.log         # Detailed log
```

## Features
- RSS feed parsing for circulars
- PDF table extraction with pdfplumber (merge by column count)
- Excel to CSV conversion
- Skip logic with `.done_` markers (re-runnable)
- Retry with exponential backoff
- Text fallback when no tables found in PDFs
