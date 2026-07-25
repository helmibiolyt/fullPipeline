#!/usr/bin/env python3
"""Generate the SFDA Scraper technical report PDF."""

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak, KeepTogether
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from datetime import datetime

OUT = "/home/dev-helmi/Desktop/scrape/SFDA_Saud_/SFDA_Scraper_Report.pdf"

# ── Colours ──────────────────────────────────────────────────────────────────
SFDA_GREEN  = colors.HexColor("#006633")
SFDA_LIGHT  = colors.HexColor("#E8F5EE")
HEADER_BG   = colors.HexColor("#004422")
ALT_ROW     = colors.HexColor("#F2F9F5")
GREY_LINE   = colors.HexColor("#CCCCCC")
CODE_BG     = colors.HexColor("#F4F4F4")
RED         = colors.HexColor("#CC0000")
BLUE        = colors.HexColor("#003399")

# ── Doc setup ─────────────────────────────────────────────────────────────────
doc = SimpleDocTemplate(
    OUT,
    pagesize=A4,
    leftMargin=2*cm, rightMargin=2*cm,
    topMargin=2.5*cm, bottomMargin=2.5*cm,
    title="SFDA Data Scraper — Technical Report",
    author="SFDA Scraper System",
)

W = A4[0] - 4*cm   # usable width

# ── Styles ────────────────────────────────────────────────────────────────────
base = getSampleStyleSheet()

def S(name, **kw):
    s = ParagraphStyle(name, **kw)
    return s

TITLE   = S("Title",   fontSize=26, textColor=SFDA_GREEN, spaceAfter=6,
            fontName="Helvetica-Bold", alignment=TA_CENTER)
SUBTITLE= S("Sub",     fontSize=13, textColor=colors.HexColor("#444444"),
            spaceAfter=4, fontName="Helvetica", alignment=TA_CENTER)
DATE_S  = S("Date",    fontSize=10, textColor=colors.grey,
            spaceAfter=20, fontName="Helvetica", alignment=TA_CENTER)
H1      = S("H1",      fontSize=16, textColor=SFDA_GREEN, spaceBefore=18,
            spaceAfter=6, fontName="Helvetica-Bold", borderPad=4)
H2      = S("H2",      fontSize=12, textColor=HEADER_BG, spaceBefore=12,
            spaceAfter=4, fontName="Helvetica-Bold")
H3      = S("H3",      fontSize=10, textColor=SFDA_GREEN, spaceBefore=8,
            spaceAfter=3, fontName="Helvetica-Bold")
BODY    = S("Body",    fontSize=9,  spaceAfter=4,  fontName="Helvetica",
            leading=14, alignment=TA_JUSTIFY)
BULLET  = S("Bullet",  fontSize=9,  spaceAfter=3,  fontName="Helvetica",
            leftIndent=16, leading=13)
CODE    = S("Code",    fontSize=8,  fontName="Courier", backColor=CODE_BG,
            leftIndent=12, rightIndent=12, spaceAfter=6, spaceBefore=4,
            leading=12, borderPad=6)
NOTE    = S("Note",    fontSize=8,  fontName="Helvetica-Oblique",
            textColor=colors.HexColor("#555555"), spaceAfter=4, leading=12)
TH      = S("TH",      fontSize=8,  fontName="Helvetica-Bold",
            textColor=colors.white, alignment=TA_CENTER)
TD      = S("TD",      fontSize=8,  fontName="Helvetica", leading=11)
TD_C    = S("TDC",     fontSize=8,  fontName="Helvetica", alignment=TA_CENTER, leading=11)

def hr(): return HRFlowable(width="100%", thickness=1, color=SFDA_GREEN, spaceAfter=8, spaceBefore=4)
def sp(h=6): return Spacer(1, h)
def p(text, style=BODY): return Paragraph(text, style)
def h1(t): return p(t, H1)
def h2(t): return p(t, H2)
def h3(t): return p(t, H3)
def b(t):  return p(f"• {t}", BULLET)
def code(t): return p(t, CODE)
def note(t): return p(f"<i>Note: {t}</i>", NOTE)

def tbl(data, col_widths, style_extra=None):
    ts = TableStyle([
        ("BACKGROUND",  (0,0), (-1,0), HEADER_BG),
        ("TEXTCOLOR",   (0,0), (-1,0), colors.white),
        ("FONTNAME",    (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",    (0,0), (-1,-1), 8),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, ALT_ROW]),
        ("GRID",        (0,0), (-1,-1), 0.4, GREY_LINE),
        ("VALIGN",      (0,0), (-1,-1), "TOP"),
        ("TOPPADDING",  (0,0), (-1,-1), 4),
        ("BOTTOMPADDING",(0,0),(-1,-1), 4),
        ("LEFTPADDING", (0,0), (-1,-1), 5),
        ("RIGHTPADDING",(0,0),(-1,-1), 5),
    ])
    if style_extra:
        for s in style_extra: ts.add(*s)
    t = Table(data, colWidths=col_widths, repeatRows=1)
    t.setStyle(ts)
    return t


# ══════════════════════════════════════════════════════════════════════════════
# Content
# ══════════════════════════════════════════════════════════════════════════════
story = []

# ── Cover page ───────────────────────────────────────────────────────────────
story += [
    sp(60),
    p("SFDA Data Scraper", TITLE),
    p("Technical Reference & Operations Manual", SUBTITLE),
    sp(6),
    hr(),
    sp(6),
    p(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", DATE_S),
    sp(20),
    p(
        "This document describes the architecture, dataset catalogue, column schemas, "
        "identifier strategy, SCD Type 2 change-tracking logic, and operational "
        "procedures for the SFDA (Saudi Food and Drug Authority) data extraction system.",
        BODY,
    ),
    sp(10),
    tbl(
        [["Property", "Value"],
         ["Source Website", "https://www.sfda.gov.sa"],
         ["Sections Scraped", "Drugs · Clinical Trials"],
         ["Total Datasets", "20"],
         ["Scraper File", "sfda_scraper.py"],
         ["Output Directory", "sfda_data/"],
         ["Change Tracking", "SCD Type 2 (identifier-based)"],
         ["Python Requirement", "Python 3.10+"]],
        [4*cm, 11*cm],
    ),
    PageBreak(),
]

# ── 1. Architecture ──────────────────────────────────────────────────────────
story += [
    h1("1. System Architecture"),
    hr(),
    h2("1.1  How the Scraper Works"),
    p(
        "The scraper discovers and extracts data from 20 SFDA datasets across two passes: "
        "a <b>static HTTP pass</b> (using Requests + BeautifulSoup) and a "
        "<b>JavaScript-rendered pass</b> (using Playwright/Chromium). "
        "Both passes write their output into the same CSV files using the same SCD Type 2 logic.",
        BODY,
    ),
    sp(4),
    h3("Pass 1 — Static (Requests + BeautifulSoup)"),
    b("Fetches SFDA listing pages and discovers dataset URLs automatically."),
    b("Walks all pagination pages to collect every table row."),
    b("Falls back to Excel/CSV download links when the page reports 0 records."),
    b("For RMM dataset: visits each detail page to extract risk string and materials JSON."),
    b("For Reference Warehouses and International Standards: parses modal popups as JSON."),
    sp(4),
    h3("Pass 2 — JavaScript-Rendered (Playwright Chromium)"),
    b("Launches a headless Chromium browser for datasets that require JS to render."),
    b("Paginates by following the actual pager link href (pg=N), not manually incrementing."),
    b("Uses MD5 hash of page content to detect and stop pagination loops."),
    b("Covers: Registered Drugs, Released Vaccines, Drugs Under Studying, Drug Companies, Incentive Project List."),
    sp(6),
    h2("1.2  Data Flow"),
    tbl(
        [["Step", "Action", "Tool"],
         ["1", "Discover dataset URLs from listing pages", "Requests + BS4"],
         ["2", "Paginate through all table pages", "Requests / Playwright"],
         ["3", "Parse table rows and modal details", "BeautifulSoup"],
         ["4", "Deduplicate incoming rows (source duplicates)", "MD5 hash"],
         ["5", "SCD Type 2 merge against existing CSV", "write_csv()"],
         ["6", "Write output CSV with SCD columns", "csv module"]],
        [0.6*cm, 9*cm, 4.4*cm],
    ),
    PageBreak(),
]

# ── 2. How to Run ────────────────────────────────────────────────────────────
story += [
    h1("2. How to Run the Scraper"),
    hr(),
    h2("2.1  Prerequisites"),
    b("Python 3.10 or higher"),
    b("Install dependencies:"),
    code("pip install requests beautifulsoup4 lxml openpyxl playwright"),
    code("playwright install chromium"),
    sp(4),
    h2("2.2  Scrape All Datasets"),
    p("Run both the static and Playwright passes over all 20 datasets:", BODY),
    code("python sfda_scraper.py --playwright"),
    p("Static-only (skips JS-rendered datasets):", BODY),
    code("python sfda_scraper.py"),
    sp(4),
    h2("2.3  Scrape a Single Specific Dataset"),
    p(
        "Use the <b>--dataset</b> flag followed by the URL slug "
        "(the last part of the dataset URL). Examples:",
        BODY,
    ),
    tbl(
        [["Dataset", "Command"],
         ["Released Vaccines",         "python sfda_scraper.py --playwright --dataset released-vaccines"],
         ["Drugs Under Studying",       "python sfda_scraper.py --playwright --dataset underStudyingList"],
         ["Registered Drugs",           "python sfda_scraper.py --playwright --dataset drugs-list"],
         ["Incentive Project List",     "python sfda_scraper.py --playwright --dataset unregistered-pharmaceuticals-list"],
         ["Drug Companies",             "python sfda_scraper.py --playwright --dataset drug-companies"],
         ["Safety Alert",               "python sfda_scraper.py --dataset safety-alert"],
         ["Saudi Standards",            "python sfda_scraper.py --dataset saudi-standards-lists"],
         ["Risk Minimization (RMM)",    "python sfda_scraper.py --dataset RMM"],
         ["Shortage Drugs",             "python sfda_scraper.py --dataset currentlyInShortageList"],
         ["Drug Approvals",             "python sfda_scraper.py --dataset new-sfda-drug-approvals"]],
        [5.5*cm, 9.5*cm],
    ),
    sp(6),
    h2("2.4  Additional Options"),
    tbl(
        [["Flag", "Default", "Description"],
         ["--playwright",  "off",       "Enable Playwright pass for JS-rendered datasets"],
         ["--dataset SLUG","all",       "Scrape only the dataset whose URL contains SLUG"],
         ["-o DIR",        "sfda_data/","Output directory for CSV files"],
         ["-d SECONDS",    "1.2",       "Delay between HTTP requests (be polite to server)"]],
        [3.5*cm, 2.5*cm, 9*cm],
    ),
    sp(6),
    h2("2.5  Output Location"),
    p("All CSVs are written to <b>sfda_data/</b> in the same directory as the script. "
      "Each file is named after the dataset title (spaces → underscores). "
      "Re-running the scraper on an existing CSV applies SCD Type 2 updates — "
      "it does not overwrite the file from scratch.", BODY),
    PageBreak(),
]

# ── 3. Output CSV Format ─────────────────────────────────────────────────────
story += [
    h1("3. Output CSV Format"),
    hr(),
    h2("3.1  File Structure"),
    p("Every CSV file has the same structure:", BODY),
    tbl(
        [["Row", "Content", "Example"],
         ["1",   "Metadata comment — source URL",           "# Source URL: https://www.sfda.gov.sa/en/..."],
         ["2",   "Metadata comment — extraction date",      "# Extraction Date: 2026-06-30"],
         ["3",   "Metadata comment — active record count",  "# Total Active Records: 297"],
         ["4",   "Column header row",                       "Standard Name, Standard Number, start_date, ..."],
         ["5+",  "Data rows (active and expired)",          "ISO 1234, ..., 2026-01-01, , true"]],
        [1.2*cm, 6.5*cm, 7.3*cm],
    ),
    sp(6),
    h2("3.2  SCD Type 2 Tracking Columns"),
    p("Three columns are appended to every data row:", BODY),
    tbl(
        [["Column",      "Type",   "Description"],
         ["start_date",  "Date",   "Date this version of the record first appeared (YYYY-MM-DD)"],
         ["end_date",    "Date",   "Date this version was superseded or removed. Empty = still active"],
         ["is_active",   "Bool",   "true = current record.  false = expired/changed/deleted"]],
        [3*cm, 2*cm, 10*cm],
    ),
    sp(6),
    h2("3.3  Reading Only Current Records"),
    p("To read only the latest active records, filter on <b>is_active = true</b>:", BODY),
    code("import pandas as pd"),
    code("df = pd.read_csv('sfda_data/Released_Vaccines.csv', comment='#')"),
    code("active = df[df['is_active'] == True]"),
    sp(4),
    note("The # comment lines at the top are metadata and must be skipped. "
         "Pandas comment='#' handles this automatically. "
         "Safety_Alert.csv has a column literally named '#' — its header row "
         "starts with '#,' (no space), distinguishing it from metadata lines."),
    PageBreak(),
]

# ── 4. SCD Type 2 Logic ──────────────────────────────────────────────────────
story += [
    h1("4. SCD Type 2 Change Tracking"),
    hr(),
    h2("4.1  What Happens on First Scrape"),
    b("All scraped rows are inserted as active (start_date = today, is_active = true)."),
    b("No history rows exist yet."),
    sp(4),
    h2("4.2  What Happens on Re-Scrape"),
    p("The scraper merges the new scrape against the existing CSV using "
      "the dataset's <b>identifier columns</b> as the matching key:", BODY),
    sp(4),
    tbl(
        [["Scenario",           "Action",                                       "Result in CSV"],
         ["Record unchanged",   "Identifier matches, all values same",          "Kept as-is, start_date preserved"],
         ["Record changed",     "Identifier matches, but a value column differs","Old row expired (end_date=today, is_active=false)\nNew row inserted (start_date=today, is_active=true)"],
         ["New record",         "Identifier not found in existing CSV",         "Inserted (start_date=today, is_active=true)"],
         ["Record deleted",     "Identifier in existing CSV but missing from new scrape","Expired (end_date=today, is_active=false)"]],
        [3.5*cm, 5*cm, 6.5*cm],
    ),
    sp(6),
    h2("4.3  Identifier vs Full-Row Hash"),
    p(
        "The <b>identifier</b> is a set of columns that form the natural business key for a record. "
        "When a value column changes (e.g. a vaccine batch Result changes from Pass to Fail), "
        "the scraper knows it is the <i>same</i> record (same Batch No) that was updated, "
        "not two unrelated rows. It expires the old version and inserts the new version — "
        "keeping both in the CSV so the full history is preserved.",
        BODY,
    ),
    sp(4),
    p(
        "Three datasets have no discoverable stable natural key. "
        "For those, the scraper falls back to hashing the <b>entire row</b> — "
        "any change to any field creates a new record and expires the old one.",
        BODY,
    ),
    sp(4),
    h2("4.4  Source Deduplication"),
    p(
        "The SFDA website sometimes serves identical rows multiple times on the same page. "
        "Before the SCD merge, the scraper deduplicates incoming rows by identifier, "
        "keeping only the first occurrence of each identifier. "
        "This is done in memory and does not affect the CSV history.",
        BODY,
    ),
    PageBreak(),
]

# ── 5. Dataset Catalogue ─────────────────────────────────────────────────────
story += [
    h1("5. Dataset Catalogue"),
    hr(),
    p(
        "All 20 datasets extracted from the SFDA website. "
        "Datasets marked JS require Playwright (--playwright flag). "
        "Empty datasets are written as CSV files with headers only.",
        BODY,
    ),
    sp(6),
]

DATASETS = [
    {
        "name":   "Clinical Trials List",
        "file":   "Clinical_Trials_List.csv",
        "url":    "https://www.sfda.gov.sa/en/clinical-trials-list",
        "rows":   167,
        "js":     False,
        "cols":   ["Trial Title", "Device name", "Protocol Number", "Sponsor", "Status", "Trial Site"],
        "id":     ["Trial Title", "Device name", "Trial Site"],
        "notes":  "Lists registered clinical trials for drugs and medical devices.",
    },
    {
        "name":   "Drug Approvals",
        "file":   "Drug_Approvals.csv",
        "url":    "https://www.sfda.gov.sa/en/new-sfda-drug-approvals",
        "rows":   336,
        "js":     False,
        "cols":   ["Request Type", "Drug Type", "Trade Name", "Scientific Name", "Strength", "Dosage Form", "Approval Date", "SFDA Approved Use"],
        "id":     ["Trade Name", "Strength", "Dosage Form"],
        "notes":  "New drug approvals issued by SFDA.",
    },
    {
        "name":   "Drug Clinical Trials List",
        "file":   "Drug_Clinical_Trials_List.csv",
        "url":    "https://www.sfda.gov.sa/en/drug-clinical-trials-list",
        "rows":   821,
        "js":     False,
        "cols":   ["Study Title", "Study Sponsor", "Status", "Study Drug", "Trial Phase", "Study Protocol Number", "Site"],
        "id":     "FULL-ROW HASH (no stable unique key)",
        "notes":  "Drug-specific clinical study registry.",
    },
    {
        "name":   "Drug Companies List",
        "file":   "Drug_Companies_List.csv",
        "url":    "https://www.sfda.gov.sa/en/drug-companies",
        "rows":   0,
        "js":     True,
        "cols":   [],
        "id":     "N/A (empty dataset)",
        "notes":  "Licensed drug companies. The SFDA API endpoint currently returns no data.",
    },
    {
        "name":   "Drugs Safety Labeling Updates",
        "file":   "Drugs_Safety_Labeling_Updates.csv",
        "url":    "https://www.sfda.gov.sa/en/drugs-safety-labeling",
        "rows":   406,
        "js":     False,
        "cols":   ["Scientific Name", "Trade Name", "Updated Section", "Safety-Related Labeling Changes", "Date"],
        "id":     ["Trade Name", "Updated Section", "Date"],
        "notes":  "Tracks safety-related label changes per drug.",
    },
    {
        "name":   "Drugs Under Studying List",
        "file":   "Drugs_Under_Studying_List.csv",
        "url":    "https://www.sfda.gov.sa/en/underStudyingList",
        "rows":   1830,
        "js":     True,
        "cols":   ["Trade Name", "Dosage Form", "Applicant Company Name", "Details"],
        "id":     ["Trade Name", "Dosage Form", "Applicant Company Name", "Details"],
        "notes":  "Drugs currently under SFDA evaluation. Details column is JSON.",
    },
    {
        "name":   "Hospitals that Reported Side Effects",
        "file":   "Hospitals_that_reported_side_effects.csv",
        "url":    "https://www.sfda.gov.sa/en/list-of-hospitals-that-reported-sides-effects",
        "rows":   0,
        "js":     False,
        "cols":   [],
        "id":     "N/A (empty dataset)",
        "notes":  "Currently shows no results on the SFDA website.",
    },
    {
        "name":   "Incentive Project List",
        "file":   "Incentive_Project_List.csv",
        "url":    "https://www.sfda.gov.sa/en/unregistered-pharmaceuticals-list",
        "rows":   545,
        "js":     True,
        "cols":   ["Scientific Name", "Strength", "Dosage Form", "Purpose", "Date of Update"],
        "id":     "FULL-ROW HASH (no stable unique key)",
        "notes":  "Unregistered pharmaceuticals eligible for the SFDA incentive program.",
    },
    {
        "name":   "International Standards Lists",
        "file":   "International_Standards_Lists.csv",
        "url":    "https://www.sfda.gov.sa/en/international-standards-lists",
        "rows":   29,
        "js":     False,
        "cols":   ["Title", "Code", "Details"],
        "id":     ["Title", "Code", "Details"],
        "notes":  "Details column is a JSON dict with keys: Organization, Code, Organization Number, Link.",
    },
    {
        "name":   "Licensed Pharmacies List",
        "file":   "Licensed_Pharmacies_List.csv",
        "url":    "https://www.sfda.gov.sa/en/list-registered-pharmacies",
        "rows":   198,
        "js":     False,
        "cols":   ["Pharmacy Name", "Contact Number", "Pharmacy Address", "Region"],
        "id":     ["Pharmacy Name", "Contact Number"],
        "notes":  "SFDA-licensed pharmacies with contact details.",
    },
    {
        "name":   "List of Registered Human/Herbal/Veterinary Drugs",
        "file":   "List_of_Registered_HumanHerbalVeterinary_Drugs.csv",
        "url":    "https://www.sfda.gov.sa/en/drugs-list",
        "rows":   8916,
        "js":     True,
        "cols":   ["Scientific Name", "Trade Name", "Strength", "Dosage Form", "Price", "Details"],
        "id":     ["Details"],
        "notes":  "Details column is a JSON dict containing Register Number, Route of Administration, Size, Package Type, Legal Status, etc. Register Number inside Details is the true unique ID.",
    },
    {
        "name":   "List of Available Drugs in Reference Warehouses",
        "file":   "List_of_available_drugs_in_reference_warehouses.csv",
        "url":    "https://www.sfda.gov.sa/en/reference-repositories-drugs-list",
        "rows":   981,
        "js":     False,
        "cols":   ["Warehouse Name", "Scientific Name", "Drug Name", "Registration number", "Details"],
        "id":     ["Warehouse Name", "Registration number"],
        "notes":  "Details column is a JSON dict (keys: Warehouse Distribution Area, contact number, Email). Warehouse Name is excluded from Details to avoid duplication.",
    },
    {
        "name":   "Pharmacovigilance",
        "file":   "Pharmacovigilance.csv",
        "url":    "https://www.sfda.gov.sa/en/pharmacovigilance",
        "rows":   65,
        "js":     False,
        "cols":   ["Product", "Date", "Safety information", "Attachments"],
        "id":     ["Product", "Safety information"],
        "notes":  "Drug safety bulletins. Same product may have multiple entries on different dates.",
    },
    {
        "name":   "Released Vaccines",
        "file":   "Released_Vaccines.csv",
        "url":    "https://www.sfda.gov.sa/en/released-vaccines",
        "rows":   1326,
        "js":     True,
        "cols":   ["Product Name", "Batch No", "Manufacturer", "Result"],
        "id":     ["Batch No"],
        "notes":  "Vaccine batch release results (Pass/Fail). Batch No is the unique lot identifier.",
    },
    {
        "name":   "Results and Studies of Marketed Pharmaceutical Products",
        "file":   "Results_and_studies_of_marketed_pharmaceutical_products.csv",
        "url":    "https://www.sfda.gov.sa/en/marketed-products",
        "rows":   3,
        "js":     False,
        "cols":   ["Register Number", "Trade Name", "Scientific Name", "Dosage Form", "#"],
        "id":     ["Register Number"],
        "notes":  "Post-market surveillance study results.",
    },
    {
        "name":   "Risk Minimization Measures List (RMM)",
        "file":   "Risk_Minimization_Measures_List.csv",
        "url":    "https://www.sfda.gov.sa/en/RMM",
        "rows":   522,
        "js":     False,
        "cols":   ["Scientific Name", "Brand name", "Approval Date", "Last Update", "risk", "materials"],
        "id":     ["Scientific Name", "Brand name"],
        "notes":  "risk = plain-text risk description. materials = JSON list of {name, files[]} objects (educational materials and PDF links). Both are fetched from individual detail pages.",
    },
    {
        "name":   "Safety Alert",
        "file":   "Safety_Alert.csv",
        "url":    "https://www.sfda.gov.sa/en/safety-alert",
        "rows":   259,
        "js":     False,
        "cols":   ["#", "Title", "Type of news", "Date", "Link of news"],
        "id":     ["Link of news"],
        "notes":  "Column '#' is a sequential alert number. Link of news is the unique PDF/page URL per alert.",
    },
    {
        "name":   "Saudi Public Assessment Report (Saudi PAR)",
        "file":   "Saudi_Public_Assessment_Report_Saudi_PAR.csv",
        "url":    "https://www.sfda.gov.sa/en/drug-evaluation-reports",
        "rows":   115,
        "js":     False,
        "cols":   ["Trade name", "Scientific name", "Dosage form", "Marketing company", "Manufacture company", "License status", "Details"],
        "id":     ["Trade name", "Scientific name", "Dosage form"],
        "notes":  "SFDA public drug assessment reports. Details column links to the assessment PDF.",
    },
    {
        "name":   "Saudi Standards Lists",
        "file":   "Saudi_Standards_Lists.csv",
        "url":    "https://www.sfda.gov.sa/en/saudi-standards-lists",
        "rows":   297,
        "js":     False,
        "cols":   ["Standard Name", "Standard Name Eng", "Standard Number"],
        "id":     ["Standard Number"],
        "notes":  "SFDA technical standards catalogue. Standard Number is the unique numeric ID.",
    },
    {
        "name":   "Shortage Drugs List",
        "file":   "Shortage_Drugs_List.csv",
        "url":    "https://www.sfda.gov.sa/en/currentlyInShortageList",
        "rows":   1849,
        "js":     False,
        "cols":   ["SCIENTIFICNAME_EN", "TRADENAME_EN", "REGISTRATION_NO", "Manufacturer_Name", "UPDATE_TIME", "Agent", "SHORTAGE_TYPE_EN", "SHORTAGE_START_DATE", "duration", "SHORTAGE_REASON_EN"],
        "id":     "FULL-ROW HASH (no stable unique key — same drug can have overlapping shortage records)",
        "notes":  "Live drug shortage list from SFDA API. Loaded via JSON endpoint, not HTML table.",
    },
]

for ds in DATASETS:
    id_val = ds["id"]
    id_str = ", ".join(id_val) if isinstance(id_val, list) else id_val
    js_badge = "JS ✓" if ds["js"] else "Static"
    cols_str = "\n".join(ds["cols"]) if ds["cols"] else "(empty)"

    block = [
        KeepTogether([
            h2(f"{'★ ' if ds['js'] else ''}{ds['name']}"),
            tbl(
                [["Property",    "Value"],
                 ["File",        ds["file"]],
                 ["Source URL",  ds["url"]],
                 ["Scrape Mode", js_badge],
                 ["Active Rows", str(ds["rows"])],
                 ["Identifier",  id_str],
                 ["Columns",     ", ".join(ds["cols"]) if ds["cols"] else "(none — empty dataset)"],
                 ["Notes",       ds["notes"]]],
                [3*cm, 12*cm],
            ),
            sp(8),
        ]),
    ]
    story += block

story.append(PageBreak())

# ── 6. Special Column Formats ─────────────────────────────────────────────────
story += [
    h1("6. Special Column Formats"),
    hr(),
    h2("6.1  Details Column — JSON Dict"),
    p("Some datasets store modal popup content as a JSON dictionary in the Details column. "
      "Each key is a field label and each value is the field's text or URL.", BODY),
    sp(4),
    p("<b>Reference Warehouses</b> — example Details value:", BODY),
    code('{"Warehouse Distribution Area": "المنطقة الشرقية", "contact number": "537119153", "Email": "user@example.com"}'),
    p("<b>International Standards</b> — example Details value:", BODY),
    code('{"Organization": "ISO", "Code": "ISO-TC 173", "Organization Number": "1", "Link": "https://..."}'),
    sp(6),
    h2("6.2  Details Column — JSON Dict (Registered Drugs)"),
    p("For the Registered Drugs dataset, Details is a JSON dict parsed from the drug's "
      "modal popup. It includes the Register Number which is the true unique identifier:", BODY),
    code('{"Register Number": "1807245616", "Route of Administration": "Parenteral use", "Size": "10 mL", "Package Type": "Vial", ...}'),
    sp(6),
    h2("6.3  RMM — risk and materials Columns"),
    p("The Risk Minimization Measures dataset fetches each drug's detail page to extract:", BODY),
    b("<b>risk</b>: Plain-text description of the risk (e.g. 'Risk of hepatotoxicity')."),
    b("<b>materials</b>: JSON list of educational material objects, each with a name and list of file URLs."),
    sp(4),
    p("<b>materials</b> column example:", BODY),
    code('[{"name": "Healthcare Professional Guide", "files": ["https://sfda.gov.sa/...pdf"]}, '
         '{"name": "Patient Card", "files": ["https://sfda.gov.sa/...pdf"]}]'),
    PageBreak(),
]

# ── 7. Re-scraping Behaviour ─────────────────────────────────────────────────
story += [
    h1("7. What Happens When You Re-Scrape"),
    hr(),
    p("Re-running the scraper on an already-extracted dataset does NOT overwrite the CSV. "
      "Instead it performs a precise SCD Type 2 merge:", BODY),
    sp(6),
    h2("7.1  Step-by-Step Merge Process"),
    tbl(
        [["Step", "Description"],
         ["1. Load existing CSV",    "Read all rows. Separate active (is_active=true) from expired (is_active=false) history."],
         ["2. Scrape fresh data",    "Fetch all current pages from SFDA and collect new rows."],
         ["3. Deduplicate",          "Remove duplicate rows from the fresh scrape (SFDA sometimes serves the same row multiple times)."],
         ["4. Match by identifier",  "For each new row, compute a hash of its identifier columns and look it up in the active set."],
         ["5. Unchanged rows",       "Identifier matches AND all other columns match → keep the row, preserve its original start_date."],
         ["6. Changed rows",         "Identifier matches BUT a value column differs → expire the old row (end_date=today, is_active=false), insert the new row (start_date=today)."],
         ["7. New rows",             "Identifier not found in existing CSV → insert as new active row (start_date=today)."],
         ["8. Deleted rows",         "Active rows in the old CSV whose identifier is absent from the new scrape → expire (end_date=today, is_active=false)."],
         ["9. Write output",         "Write updated metadata comments, header, all active rows, all history rows to the CSV."]],
        [4*cm, 11*cm],
    ),
    sp(8),
    h2("7.2  Example: Vaccine Batch Result Changes"),
    p("Suppose Released_Vaccines.csv contains:", BODY),
    code("Pfizer COVID, EL0200, PFIZER, Pass, 2026-01-15, , true"),
    p("On re-scrape SFDA now shows Result = Fail for EL0200. The scraper:", BODY),
    b("Finds EL0200 in existing_active (matched by Batch No identifier)."),
    b("Detects that Result changed from Pass to Fail."),
    b("Expires the old row: end_date = today, is_active = false."),
    b("Inserts new row: Pfizer COVID, EL0200, PFIZER, Fail, today, , true."),
    p("Both versions are now in the CSV — the change history is preserved.", BODY),
    PageBreak(),
]

# ── 8. File Reference ────────────────────────────────────────────────────────
story += [
    h1("8. File and Directory Reference"),
    hr(),
    tbl(
        [["Path",                          "Description"],
         ["sfda_scraper.py",               "Main scraper — all logic in one file"],
         ["sfda_data/",                    "Output directory — one CSV per dataset"],
         ["sfda_data/<Dataset_Name>.csv",  "Individual dataset CSV with SCD columns"],
         ["requirements.txt",              "Python package requirements"]],
        [6*cm, 9*cm],
    ),
    sp(8),
    h2("8.1  Key Constants in sfda_scraper.py"),
    tbl(
        [["Constant",           "Purpose"],
         ["BASE_URL",           "Root URL: https://www.sfda.gov.sa"],
         ["JS_DATASETS",        "List of datasets requiring Playwright (title + URL path)"],
         ["DATASET_IDENTIFIERS","Dict mapping CSV filename stem → list of identifier columns"],
         ["SCD_COLS",           "The three SCD columns: start_date, end_date, is_active"],
         ["DEFAULT_DELAY",      "Seconds between HTTP requests (default 1.2s)"],
         ["EXTRACTION_DATE",    "Today's date string used for start_date / end_date values"]],
        [5*cm, 10*cm],
    ),
    sp(8),
    h2("8.2  Key Methods"),
    tbl(
        [["Method",                    "Description"],
         ["scrape_all()",              "Entry point — runs static pass then optional Playwright pass"],
         ["scrape_dataset(title,url)", "Scrapes one static dataset through all pages"],
         ["scrape_playwright_datasets()","Runs Playwright pass over JS_DATASETS"],
         ["write_csv(title,hdrs,rows)","SCD Type 2 merge and CSV write"],
         ["parse_table(soup)",         "Extracts headers and rows from an HTML table"],
         ["_row_hash(row)",            "MD5 of all column values (used for full-row fallback and dedup)"],
         ["_next_playwright_url(soup,url)","Derives next pagination URL from the rendered pager"],
         ["_parse_rmm_detail(soup)",   "Extracts risk + materials from an RMM detail page"],
         ["_cell_text(cell,soup)",     "Extracts cell text, resolves modal popups to JSON"]],
        [5.5*cm, 9.5*cm],
    ),
    PageBreak(),
]

# ── 9. Identifier Summary Table ───────────────────────────────────────────────
story += [
    h1("9. Identifier Column Summary"),
    hr(),
    p("Quick reference: which columns are used to identify each record during re-scraping.", BODY),
    sp(6),
    tbl(
        [["Dataset",                                 "Identifier Column(s)",                          "Mode"],
         ["Clinical Trials List",                    "Trial Title + Device name + Trial Site",         "identifier"],
         ["Drug Approvals",                          "Trade Name + Strength + Dosage Form",            "identifier"],
         ["Drug Clinical Trials List",               "—",                                              "full-row hash"],
         ["Drug Companies List",                     "N/A (empty)",                                    "—"],
         ["Drugs Safety Labeling Updates",           "Trade Name + Updated Section + Date",            "identifier"],
         ["Drugs Under Studying List",               "Trade Name + Dosage Form + Applicant + Details", "identifier"],
         ["Hospitals (side effects)",                "N/A (empty)",                                    "—"],
         ["Incentive Project List",                  "—",                                              "full-row hash"],
         ["International Standards Lists",           "Title + Code + Details",                         "identifier"],
         ["Licensed Pharmacies List",                "Pharmacy Name + Contact Number",                 "identifier"],
         ["List of Registered Drugs",                "Details (JSON containing Register Number)",      "identifier"],
         ["Reference Warehouses",                    "Warehouse Name + Registration number",           "identifier"],
         ["Pharmacovigilance",                       "Product + Safety information",                   "identifier"],
         ["Released Vaccines",                       "Batch No",                                       "identifier"],
         ["Results of Marketed Products",            "Register Number",                                "identifier"],
         ["Risk Minimization Measures (RMM)",        "Scientific Name + Brand name",                   "identifier"],
         ["Safety Alert",                            "Link of news",                                   "identifier"],
         ["Saudi PAR",                               "Trade name + Scientific name + Dosage form",     "identifier"],
         ["Saudi Standards Lists",                   "Standard Number",                                "identifier"],
         ["Shortage Drugs List",                     "—",                                              "full-row hash"]],
        [5.5*cm, 7*cm, 2.5*cm],
        style_extra=[
            ("BACKGROUND", (2,0), (2,0), HEADER_BG),
        ]
    ),
    sp(10),
    note("'full-row hash' means no stable natural key was found in the data. "
         "The MD5 hash of all column values combined is used instead. "
         "Any change to any field creates a new row and expires the old one."),
]

# ── Build ─────────────────────────────────────────────────────────────────────
doc.build(story)
print(f"✓ PDF written to: {OUT}")
