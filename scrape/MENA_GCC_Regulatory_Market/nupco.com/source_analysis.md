# NUPCO (National Unified Procurement Company) - Source Analysis

## Site Overview
- **URL:** https://www.nupco.com
- **Platform:** WordPress with Elementor page builder and wpDataTables plugin
- **Languages:** Arabic (primary) and English
- **Staging domain observed:** stg.nupco.com (used for file hosting)

---

## Data Source 1: Unified Catalogue (HIGHEST VALUE)

**Source Page:** https://www.nupco.com/suppliers/unified-catalogue/

Four Excel catalogues hosted on stg.nupco.com:

| Category | File | Date |
|---|---|---|
| Pharmaceuticals | NUPCO-Pharmaceuticals-Catalogue-June-2026.xlsx | June 2026 |
| Medical Equipment | NUPCOs-Medical-Equipment-Catalogue-April-2026.xlsx | April 2026 |
| Medical Supplies | NUPCOS-MEDICAL-SUPPLIES-Catalogue-Dec2025.xlsx | Dec 2025 |
| Laboratory Supplies | NUPCOS-LABORATORY-Catalogue-APRIL-2024.xlsx | April 2024 |

**Technical Notes:**
- Links are embedded in the HTML of the catalogue page
- Files are .xlsx format with potentially multiple sheets
- Sheets with the same column count can be merged; different structures saved separately

---

## Data Source 2: Tenders List

**URL:** https://www.nupco.com/tenders/tenders-list/

- ~25+ tenders rendered in HTML
- Uses `ajaxSwapGrid()` JavaScript function for client-side filtering
- Fields: Tender ID, Title, Opening Date, Submission Deadline, Status
- Statuses: Available, Updated, Cancelled, Direct Purchase, Results, Under Study

---

## Data Source 3: Tenders Plan

**URL:** https://www.nupco.com/tenders/tenders-plan/

- Uses wpDataTables plugin for rendering
- 3 category tables:
  - Medical & Lab Supplies (25 entries)
  - Pharma (4 entries)
  - Medical Devices (5 entries)
- Columns: Month, Number, Title, Tender Opening Date, Final Announcement Date

---

## Data Source 4: WordPress REST API

**Base URL:** https://www.nupco.com/wp-json/wp/v2/

- Standard WP REST endpoints: posts, pages, media, categories
- Pagination via `X-WP-TotalPages` header
- Posts contain: id, title, date, link, excerpt
