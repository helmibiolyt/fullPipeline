# DHA (Dubai Health Authority) - Source Analysis

## High Value

### 1. Drug Price List
- **URL:** `https://www.dha.gov.ae/uploads/022022/PriceList%20en2022239251.xlsx`
- **Format:** XLSX (direct download)
- **Content:** Drug pricing data for Dubai
- **Access:** Direct file download, no authentication

### 2. Regulatory Circulars
- **URL:** `https://www.dha.gov.ae/licensing-regulations-circulars`
- **Format:** Paginated HTML with embedded JSON (~120 circulars across 6 pages)
- **Content:** Drug recalls, safety alerts, regulatory notices
- **Access:** HTTP GET with page parameter

### 3. Drug Control Department
- **URL:** `https://www.dha.gov.ae/HealthRegulationSector/DrugControl`
- **Format:** HTML page with links to forms, checklists, policies (PDFs/Excel)
- **Access:** Standard HTTP

## Medium Value

### 4. Pharmacy Self-Inspection Checklists
- **Format:** 7 PDFs at various `/uploads/` paths
- **Source:** Drug Control page

### 5. Narcotic & Controlled Drug Forms
- **Format:** 9 PDFs
- **Source:** Drug Control page

### 6. Pharmaceutical Policies
- **URL:** `https://www.dha.gov.ae/licensing-regulations-policies`
- **Format:** 3 PDFs

### 7. Clinical Guidelines
- **URL:** `https://www.dha.gov.ae/licensing-regulations-clinical-guidelines`
- **Format:** PDF

## Directories (JS-rendered, may not be scrapable)

### 8. Licensed Facilities
- **URL:** `https://www.dha.gov.ae/medical-listing/facilities`
- **Status:** Returns 500 error

### 9. Licensed Professionals
- **URL:** `https://www.dha.gov.ae/medical-listing/professionals`
- **Status:** JS-driven filters, requires browser rendering

## Open Data

### 10. Open Data Portal
- **URL:** `https://www.dha.gov.ae/open-data`
- **Format:** Statistical yearbooks (PDFs)
- **Pharma relevance:** Low
