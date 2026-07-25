# Abu Dhabi Department of Health (DOH) - Source Analysis

## Site Overview
- **URL:** https://www.doh.gov.ae
- **CMS:** Sitecore
- **Key Feature:** RSS feed available for circulars; most resource pages use JS-rendered tables

## Data Sources

### 1. Policies & Manuals (HIGHEST VALUE)
- **URL:** https://www.doh.gov.ae/en/resources/policies
- **Count:** 32+ PDFs (policies and manuals)
- **PDF Pattern:** `https://www.doh.gov.ae/-/media/{GUID}.ashx`
- **Key Pharmaceutical Policies:**
  - Medication Safety Policy (MSP/V1/2026)
  - Quarantine and Recall of Medical Products (QRMP/2023)
  - Thiqa Reimbursement for Obesity Medications (2025)
  - Medical Products Transportation incl. Medications
  - ~28 more policies + 6 manuals
- **Scraping Method:** Static HTML, extract `<a>` tags linking to `/-/media/*.ashx`

### 2. Circulars via RSS Feed (HIGH VALUE)
- **RSS URL:** https://www.doh.gov.ae/en/resources/circulars-rss-feed
- **Count:** 50+ circulars
- **PDF Pattern:** `https://www.doh.gov.ae/-/media/Feature/Resources/Circulars/2026/XX--2026.ashx`
- **Content:** Drug recalls, safety alerts, pharmaceutical regulatory notices
- **Scraping Method:** Parse RSS/XML feed, extract item metadata and PDF links

### 3. Medications & Supplements Awareness Materials
- **URL:** https://www.doh.gov.ae/en/resources/Supplement-Product
- **Content:** Educational PDFs about dietary supplements, safe disposal, cosmetics, herbs
- **Scraping Method:** Static HTML, extract PDF links

### 4. Guidelines
- **URL:** https://www.doh.gov.ae/en/resources/guidelines
- **Content:** Clinical and regulatory guidelines
- **Scraping Method:** JS-rendered table - may need API endpoint discovery; fallback to static HTML

### 5. Standards
- **URL:** https://www.doh.gov.ae/en/resources/standards
- **Content:** Healthcare standards documents
- **Scraping Method:** JS-rendered table - same approach as guidelines

### 6. Scope of Practice
- **URL:** https://www.doh.gov.ae/en/resources/scope-of-practice
- **Content:** Scope of practice documents for healthcare professionals
- **Scraping Method:** JS-rendered table

## Technical Notes
- Sitecore CMS serves media files via `/-/media/{GUID}.ashx` pattern
- RSS feed provides structured XML with title, date, description, and enclosure/link for PDFs
- JS-rendered tables on guidelines/standards/scope-of-practice pages may not yield data from static HTML scraping
- Rate limiting: 0.5s delay between requests recommended
- User-Agent header required for reliable access
