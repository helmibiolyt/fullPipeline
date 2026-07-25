# Qatar Ministry of Public Health (MOPH) - Data Source Analysis

## High Value Sources

### 1. Priced Products List (XLSX)
- **URL:** `https://www.moph.gov.qa/_layouts/15/download.aspx?SourceUrl=/Admin/Lists/PublicationsAttachments/Attachments/66/Priced%20Products%20(15-08-2025).xlsx`
- **Content:** 1,019+ pharmaceutical products with pricing data
- **Format:** XLSX download
- **Access:** Behind WAF (Imperva) - may require browser-based download or session cookies

### 2. Qatar National Formulary (QNF)
- **URL:** `https://qnf.moph.gov.qa/`
- **Content:** 4,500+ medicines with formulary information
- **Platform:** Wolters Kluwer / Lexicomp (commercial, proprietary)
- **Access:** Very hard to scrape - proprietary platform with dynamic content loading

### 3. Pharmacy Locator
- **URL:** `https://www.moph.gov.qa/english/OurServices/eservices/Pages/pharmacy-locator.aspx`
- **Content:** ~406 pharmacies in Qatar with location data
- **Access:** Behind WAF (Imperva)

### 4. DHP Search Practitioners
- **URL:** `https://dhp.moph.gov.qa/en/Pages/SearchPractitionersPage.aspx`
- **Content:** Licensed pharmacists and healthcare practitioners
- **Platform:** SharePoint-based
- **Access:** May be accessible without CAPTCHA, but SharePoint forms can be complex

## Medium Value Sources

### 5. data.gov.qa Open Data
- **URL:** `https://www.data.gov.qa/`
- **Platform:** OpenDataSoft
- **API Pattern:** `https://www.data.gov.qa/api/explore/v2.1/catalog/datasets/{id}/records?limit=100`
- **Content:** Health facility statistics, hospital counts, health indicators
- **Access:** Publicly accessible REST API, no WAF

### 6. Guidelines for Pharmacists (PDF)
- **URL:** `https://dhp.moph.gov.qa/en/Documents/Guidelines%20for%20Pharmacists.pdf`
- **Content:** Regulatory guidelines for pharmacists in Qatar
- **Format:** PDF with tables

## Notes
- The main MOPH website (www.moph.gov.qa) uses Imperva WAF/CAPTCHA, blocking most automated access
- QNF is a commercial Lexicomp product - not feasible to scrape without license
- Drug registration portal (eservices.moph.gov.qa/dps/) requires authentication
- data.gov.qa is the most reliably accessible source via its public API
