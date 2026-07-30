#!/usr/bin/env python3
"""What each source is for, and what its cryptic columns mean.

Three tables, consumed by make_data_doc.py:

  SOURCE_PURPOSE   what a source is, in one sentence. Fallback for the 336
                   files the graph does not read - for the 96 it does read,
                   sources.py already states the purpose precisely.
  GLOSSARY         column name -> plain explanation. Matched on the folded
                   name, so it applies wherever the column appears rather
                   than being repeated per file.
  IMPORTANT_COLS   for pathologically wide files, which columns to show.

The glossary is keyed by column name rather than by file on purpose: `unii`
means the same thing in all fourteen files that carry it, and a per-file
glossary would be fourteen chances to describe it differently.
"""
from __future__ import annotations

import re

# --------------------------------------------------------------------------
SOURCE_PURPOSE: dict[str, str] = {
    # Clinical trials
    "clinicaltrials.gov": "The US registry, and the largest anywhere. One "
        "full-snapshot CSV of every registered study.",
    "clinicaltrialsregister.eu": "EU Clinical Trials Register (EudraCT). The "
        "European registry, before CTIS replaced it.",
    "euclinicaltrials.eu": "CTIS - the EU's current trials system, which took "
        "over from EudraCT.",
    "isrctn.com": "ISRCTN, the UK-based international registry.",
    "anzctr.org.au": "Australian and New Zealand registry.",
    "ctri.nic.in": "Clinical Trials Registry - India.",
    "chictr.org.cn": "Chinese Clinical Trial Registry.",
    "jrct.mhlw.go.jp": "Japan Registry of Clinical Trials.",
    "trialsearch.who.int": "WHO ICTRP, which mirrors the other registries. "
        "Loaded last so native records win on every property.",
    # Substances
    "gsrs.ncats.nih.gov": "GSRS - the FDA's substance registry, and the "
        "naming authority for this graph. Supplies UNII and the preferred "
        "name and synonyms that the resolver is built from.",
    "ebi.ac.uk-chembl": "ChEMBL. Molecules, structures, synonyms, mechanisms, "
        "targets and indications - the widest single source here.",
    "atcddd.fhi.no": "WHO ATC/DDD. The drug classification tree.",
    "rxnav.nlm.nih.gov": "RxNorm. US clinical drug vocabulary and RXCUI.",
    "dailymed.nlm.nih.gov": "DailyMed. US structured product labels; supplies "
        "SPL setids.",
    "pubchem.ncbi.nlm.nih.gov": "PubChem. Excluded from the graph - 85% of "
        "the lake's rows, and redundant with ChEMBL's InChIKey.",
    # Literature
    "pubmed.ncbi.nlm.nih.gov": "PubMed metadata - titles, journals, dates.",
    "europepmc.org": "Europe PMC. Broader than PubMed, includes preprints.",
    "biorxiv.org": "bioRxiv preprints.",
    "medrxiv.org": "medRxiv preprints - clinical, so closer to this domain.",
    "openalex.org": "OpenAlex. Excluded - the API budget was exhausted.",
    # Regulatory
    "accessdata.fda.gov": "FDA Drugs@FDA. US approvals and product records.",
    "accessdata.fda.gov-orangebook": "Orange Book. Patents and exclusivities "
        "for small molecules - the only source of either. Now IP-blocked, so "
        "this data is frozen.",
    "accessdata.fda.gov-purplebook": "Purple Book. Biologics licensures, "
        "biosimilar relationships and their exclusivities.",
    "ema.europa.eu": "European Medicines Agency. Product register plus EPAR "
        "documents.",
    "mhra.gov.uk": "UK MHRA. Product register plus PAR/SPC/PIL documents; the "
        "filenames embed the PL licence number.",
    "pmda.go.jp": "Japan PMDA. Review reports and package inserts.",
    "hres.ca": "Health Canada Drug Product Database.",
    # Safety
    "open.fda.gov": "openFDA. FAERS adverse event reports, recalls and "
        "shortages.",
    "adrreports.eu": "EudraVigilance ADR reports - the European counterpart "
        "to FAERS.",
    "vigiaccess.org": "WHO VigiAccess. Supplies the MedDRA system organ class "
        "hierarchy that groups reactions.",
    "who.int": "WHO guidance, prequalification and safety communications.",
    # Ontologies
    "meshb.nlm.nih.gov": "MeSH. The disease spine of the graph.",
    "icd.who.int": "ICD-11 classification.",
    "loinc.org": "LOINC lab observation codes. Excluded - LOINC describes "
        "measurements, not drugs.",
    "cdisc.org": "CDISC clinical data standards. Excluded - no consumer.",
    "evs.nci.nih.gov": "NCI thesaurus. Excluded - no consumer.",
    # Targets and genomics
    "uniprot.org": "UniProt. Protein records; the accession is the Target "
        "key wherever one exists.",
    "genenames.org": "HGNC. Authoritative gene symbols, which is what "
        "VARIANT_IN and TARGETS join on.",
    "platform.opentargets.org": "Open Targets. Target-disease associations "
        "with evidence scores.",
    "ncbi.nlm.nih.gov": "ClinVar. Clinically interpreted variants.",
    "cancer.sanger.ac.uk": "COSMIC. Somatic mutations in cancer, by site.",
    "cbioportal.org": "cBioPortal. Cancer genomics study data - clinical "
        "attributes per patient cohort.",
    # MENA / GCC
    "sfda.gov.sa": "Saudi FDA. Registered products, plus circulars and lists.",
    "moh.gov.sa": "Saudi Ministry of Health.",
    "dha.gov.ae": "Dubai Health Authority.",
    "mohap.gov.ae": "UAE Ministry of Health and Prevention.",
    "sahatec.sa": "Saudi health technology listings.",
    "nupco.com": "NUPCO procurement. Excluded - purchasing data, not "
        "regulatory fact.",
    "iso.org": "ISO standards abstracts.",
}

# --------------------------------------------------------------------------
# Column name -> what it means. Keys are folded (lowercase, non-alphanumerics
# collapsed to _) before matching.
GLOSSARY: dict[str, str] = {
    "unii": "FDA Unique Ingredient Identifier. The preferred Substance key "
            "in this graph.",
    "molregno": "ChEMBL's internal molecule row id. Not stable across "
                "releases - use chembl_id to refer to a molecule.",
    "chembl_id": "ChEMBL accession, e.g. CHEMBL1487. Stable.",
    "inchikey": "Hashed chemical structure. The strongest merge signal "
                "available, because it is chemistry rather than a name.",
    "standard_inchi_key": "See inchikey.",
    "canonical_smiles": "Structure as a text string.",
    "rxcui": "RxNorm concept id. Beware granularity: DailyMed publishes "
             "product-level (SCD) ids and substance files carry "
             "ingredient-level (IN) ones, so they do not join.",
    "setid": "SPL Set ID - the stable handle for a US product label.",
    "spl_setid": "See setid.",
    "atc_code": "WHO ATC classification code. Levels 1-5, where level 5 is a "
                "specific substance.",
    "tid": "ChEMBL target row id.",
    "accession": "UniProt accession, e.g. P04637. The Target key.",
    "pref_name": "The source's preferred name for the record.",
    "max_phase": "Highest clinical phase reached. 4 = approved. Written as a "
                 "float, and blank when unknown.",
    "first_approval": "Year of first regulatory approval anywhere.",
    "mesh_id": "MeSH descriptor id, e.g. D000544. The Disease key.",
    "mesh_heading": "The MeSH descriptor's official name - usually not how "
                    "anyone writes it (\"Carcinoma, Non-Small-Cell Lung\").",
    "efo_id": "Experimental Factor Ontology id. Crosswalked to MeSH.",
    "mondo_id": "MONDO disease ontology id. Also crosswalked to MeSH.",
    "clinical_significance": "ClinVar's interpretation: Pathogenic, Benign, "
                             "Uncertain significance, and so on. Only "
                             "unambiguous calls are loaded.",
    "variationid": "ClinVar variation id. The Variant key.",
    "genename": "Gene symbol. What variants join to targets on.",
    "genesymbol": "See genename.",
    "hgnc_id": "HGNC gene identifier.",
    "mutationid": "COSMIC mutation id.",
    "mutationaa": "Amino-acid change, e.g. p.V600E.",
    "mutationcds": "Change in coding DNA sequence.",
    "mutationgenomeposition": "Genomic coordinates of the mutation.",
    "primarysite": "Tissue the tumour arose in.",
    "primaryhistology": "Tumour type by microscopic appearance.",
    "safetyreportid": "FAERS report identifier. One adverse event report.",
    "reaction": "MedDRA preferred term for what happened.",
    "meddra_pt": "MedDRA Preferred Term - the standard reaction name.",
    "soc": "MedDRA System Organ Class. The grouping that makes \"any cardiac "
           "event\" one hop instead of an enumeration.",
    "eudract_number": "EU trial registration number.",
    "nct_id": "ClinicalTrials.gov registration number.",
    "application_no": "FDA application number (NDA/ANDA/BLA).",
    "appl_no": "See application_no.",
    "product_no": "Product number within an FDA application.",
    "din": "Health Canada Drug Identification Number.",
    "pl_number": "UK MHRA product licence number. Embedded in MHRA document "
                 "filenames, which is what joins a document to the graph.",
    "patent_no": "US patent number from the Orange Book.",
    "patent_expire_date_text": "Written as free text, including values like "
                               "\"Approved Prior to Jan 1, 1982\". Not "
                               "comparable as a date.",
    "exclusivity_code": "FDA exclusivity type code.",
    "sponsor": "Who is running or paying for the trial.",
    "phase": "Trial phase. Spellings vary by registry.",
    "enrollment": "Participant count. Some registries write prose here "
                  "instead of a number - 131,158 ChiCTR rows do.",
    "overall_status": "Recruiting, Completed, Terminated, and so on.",
    "score": "Open Targets association score, 0-1. Evidence strength, not "
             "probability.",
    "pubmedpmid": "PubMed identifier of the citing paper.",
    "pmid": "PubMed identifier.",
    "doi": "Digital Object Identifier.",
    "loinc_num": "LOINC code for a laboratory observation.",
    "patientid": "Patient identifier within one cBioPortal study cohort. Not "
                 "a real person and not stable across studies.",
    "studyid": "Study identifier within cBioPortal.",
}

# --------------------------------------------------------------------------
# Wide files: which columns are worth showing. Everything else is hidden and
# counted. Matched by the exact column name where possible, otherwise by
# prefix.
IMPORTANT_COLS: dict[str, list[str]] = {
    "eu_ctr_all_trials.csv": [
        "EudraCT Number",
        "Link",
        "National Competent Authority",
        "Trial Status",
        "A.2 EudraCT number",
        "A.3 Full title of the trial",
        "A.1 Member State Concerned",
        "E.1.1 Medical condition(s) being investigated",
        "E.2.1 Main objective of the trial",
        "IMP 1 - D.1.2 and D.1.3 IMP Role",
    ],
}

MAX_COLS = 26          # beyond this a file is summarised rather than listed

# Files whose shape needs explaining before anyone reads the table.
FILE_NOTE: dict[str, str] = {
    "eu_ctr_all_trials.csv":
        "<b>8,102 columns, and almost none of them are fields.</b> The scraper "
        "flattened the EudraCT application form, which nests: every distinct "
        "free-text eligibility bullet any trial ever wrote became its own "
        "column (\"A. Bone marrow function (without the support of "
        "cytokines...)\"), each form field repeats once per EU language "
        "(\"A.3 Full title of the trial\" appears 23 times), and the "
        "investigational-product block repeats as IMP 1 through IMP 52 with "
        "42 sub-fields each. Only the first four columns are true per-trial "
        "fields; the rest are sparse, and most rows are empty in most of "
        "them. The graph reads this file for the EudraCT number, status and "
        "title, and ignores the pivot."
        "<br><br>The header line alone is over 128 KB. An earlier sample "
        "reported 2,555 columns, which was the header cut mid-line by the "
        "read window rather than the real width - worth knowing, because "
        "anything that reads this file with a fixed buffer will see a "
        "plausible wrong answer instead of an error.",
}


def fold_col(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").strip().lower()).strip("_")


def notes_for(columns: list[str]) -> list[tuple[str, str]]:
    """(column, explanation) for the columns of this file that need one."""
    seen, out = set(), []
    for c in columns:
        f = fold_col(c)
        if f in GLOSSARY and f not in seen:
            seen.add(f)
            out.append((c.strip(), GLOSSARY[f]))
    return out


def choose_columns(name: str, columns: list[str]) -> tuple[list[int], int]:
    """Indices to show, and how many were hidden."""
    if len(columns) <= MAX_COLS:
        return list(range(len(columns))), 0

    want = IMPORTANT_COLS.get(name)
    if want:
        idx = []
        for w in want:
            for i, c in enumerate(columns):
                if c.strip() == w and i not in idx:
                    idx.append(i)
                    break
        if idx:
            return idx, len(columns) - len(idx)

    # No curated list: keep the ones that look like identity, then fill from
    # the front. Sources put keys first far more often than not.
    key = re.compile(r"(^|_)(id|no|number|name|code|date|status|title|phase|"
                     r"type|symbol|accession|key)($|_)", re.I)
    idx = [i for i, c in enumerate(columns) if key.search(fold_col(c))]
    idx = sorted(set(idx[:MAX_COLS] + list(range(min(6, len(columns))))))
    return idx[:MAX_COLS], len(columns) - len(idx[:MAX_COLS])
