#!/usr/bin/env python3
"""Deck content pulled from the repo, so the slides cannot drift from the code.

Source purposes, columns and example values come from graph/lake_sample.json
(read out of S3) and graph/sources.py. Entity and relationship descriptions
come from the same EDGES table the technical PDF is generated from.

Nothing here is transcribed by hand except the narrative prose.
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "graph"))

import sources as S                                    # noqa: E402
from column_notes import GLOSSARY, SOURCE_PURPOSE, fold_col   # noqa: E402
from emit import NODE_COLUMNS, EDGE_COLUMNS            # noqa: E402
from make_tech_doc import EDGES                        # noqa: E402

SAMPLE = json.loads((ROOT / "graph" / "lake_sample.json").read_text("utf-8"))
INCLUDED = {x["file"]: x for x in S.INCLUDED}
PREFIXES = [f for f in INCLUDED if f.endswith("/")]

CATEGORY_ORDER = [
    "Drug_Substance_Reference",
    "Clinical_Trials_Pipeline_Intelligence",
    "Regulatory_Approvals",
    "Safety_Pharmacovigilance",
    "Targets_Genomics_Biomarkers",
    "Ontologies_Standards",
    "Literature_Evidence",
    "MENA_GCC_Regulatory_Market",
]

CATEGORY_LABEL = {
    "Drug_Substance_Reference": "Drug & Substance Reference",
    "Clinical_Trials_Pipeline_Intelligence": "Clinical Trials & Pipeline",
    "Regulatory_Approvals": "Regulatory Approvals",
    "Safety_Pharmacovigilance": "Safety & Pharmacovigilance",
    "Targets_Genomics_Biomarkers": "Targets, Genomics & Biomarkers",
    "Ontologies_Standards": "Ontologies & Standards",
    "Literature_Evidence": "Literature & Evidence",
    "MENA_GCC_Regulatory_Market": "MENA / GCC Regulatory",
}

CATEGORY_WHY = {
    "Drug_Substance_Reference":
        "What a substance IS. This category is why every other source's drug "
        "names can be resolved at all.",
    "Clinical_Trials_Pipeline_Intelligence":
        "Ten registries, deliberately overlapping - the second registration "
        "of a study often carries the field the first one omitted.",
    "Regulatory_Approvals":
        "Agency registers, and the origin of nearly every document in the "
        "corpus.",
    "Safety_Pharmacovigilance":
        "What went wrong in the real world, after approval.",
    "Targets_Genomics_Biomarkers":
        "The biology under the drug: proteins, genes and variants.",
    "Ontologies_Standards":
        "Controlled vocabularies - what lets two sources that never agree on "
        "wording point at the same concept.",
    "Literature_Evidence":
        "Published evidence. Titles and metadata, matched conservatively.",
    "MENA_GCC_Regulatory_Market":
        "Gulf and wider MENA registers - the only coverage of these markets "
        "anywhere in the lake, and a genuine differentiator.",
}

# Columns worth putting on a slide, per source. Chosen for what a reader
# learns from seeing them, not for what comes first in the file.
FEATURED = {
    "gsrs.ncats.nih.gov":      ["unii", "preferred_name", "cas", "synonyms"],
    "ebi.ac.uk-chembl":        ["chembl_id", "pref_name", "max_phase",
                                "molecule_type"],
    "clinicaltrials.gov":      ["nct_id", "brief_title", "overall_status",
                                "phase", "enrollment"],
    "open.fda.gov":            ["safetyreportid", "drug_name", "reaction"],
    "ncbi.nlm.nih.gov":        ["GeneSymbol", "ClinicalSignificance",
                                "PhenotypeList"],
    "meshb.nlm.nih.gov":       ["descriptor_ui", "descriptor_name",
                                "tree_numbers"],
    "platform.opentargets.org": ["targetSymbol", "diseaseLabel", "score"],
    "genenames.org":           ["hgnc_id", "symbol", "name", "uniprot_ids"],
    "atcddd.fhi.no":           ["atc_code", "name", "level"],
    "rxnav.nlm.nih.gov":       ["rxcui", "name", "tty"],
    "dailymed.nlm.nih.gov":    ["setid", "title", "rxcui"],
    "meshb.nlm.nih.gov":       ["descriptor_ui", "descriptor_name"],
    "cancer.sanger.ac.uk":     ["GENE_NAME", "MUTATION_AA", "PRIMARY_SITE"],
    "uniprot.org":             ["Entry", "Protein names", "Gene Names"],
    "europepmc.org":           ["pmid", "title", "journal"],
    "pubmed.ncbi.nlm.nih.gov": ["pmid", "title", "journal"],
    "biorxiv.org":             ["doi", "title", "date"],
    "medrxiv.org":             ["doi", "title", "date"],
    "ema.europa.eu":           ["name_of_medicine", "ema_product_number",
                                "medicine_status"],
    "products.mhra.gov.uk":    ["product_name", "pl_number"],
    "health-products.canada.ca": ["DRUG_IDENTIFICATION_NUMBER", "BRAND_NAME"],
    "purplebooksearch.fda.gov": ["Proprietary Name", "BLA Number"],
    "accessdata.fda.gov-orangebook": ["Trade_Name", "Ingredient",
                                      "Approval_Date"],
    "icd.who.int":             ["code", "title"],
    "vigiaccess.org":          ["search_term", "soc", "reaction"],
    "sfda.gov.sa":             ["product_name", "scientific_name"],
    "trialsearch.who.int":     ["TrialID", "public_title", "Recruitment_Status"],
    "isrctn.com":              ["isrctn", "title", "overall_status"],
    "euclinicaltrials.eu":     ["ctNumber", "title", "trialStatus"],
    "ctri.nic.in":             ["trial_id", "public_title"],
    "jrct.mhlw.go.jp":         ["trial_id", "public_title"],
    "chictr.org.cn":           ["trial_id", "public_title", "date_registration"],
    "anzctr.org.au":           ["TRIAL ID", "STUDY TITLE", "RECRUITMENT STATUS"],
    "clinicaltrialsregister.eu": ["EudraCT Number", "Trial Status",
                                  "National Competent Authority"],
}


def used_sources() -> dict[str, list[dict]]:
    """category -> [source dossier], only sources actually used."""
    out: dict[str, list[dict]] = {}
    for cat, srcs in SAMPLE.items():
        for src, s in sorted(srcs.items()):
            read = [(n, m) for n, m in s["csvs"].items()
                    if m["key"] in INCLUDED
                    or any(m["key"].startswith(p) for p in PREFIXES)]
            pdf = s["docs"].get(".pdf", 0)
            if not read and not pdf:
                continue

            builds, notes = set(), []
            for n, m in read:
                dec = INCLUDED.get(m["key"])
                if dec:
                    builds.update(dec.get("builds", []))
                    if dec.get("note"):
                        notes.append(dec["note"])

            # Pick the file that best illustrates the source, not the
            # biggest one. ChEMBL's largest file is structures - molregno and
            # a SMILES string - which says nothing about what ChEMBL is for.
            want = {w.strip().lower() for w in FEATURED.get(src, [])}
            def _score(item):
                cols = {c.strip().lower() for c in item[1].get("columns", [])}
                return (len(want & cols), item[1].get("size", 0))
            main = max(read, key=_score)[1] if read else None
            out.setdefault(cat, []).append(dict(
                src=src, category=cat,
                role="both" if read and pdf else ("graph" if read else "docs"),
                purpose=SOURCE_PURPOSE.get(src, ""),
                builds=sorted(builds),
                n_csv=len(read), pdf=pdf,
                csv_bytes=s["csv_bytes"], doc_bytes=s["doc_bytes"],
                main=main, note=notes[0] if notes else "",
                columns=(main or {}).get("columns", []),
                rows=(main or {}).get("rows", []),
                main_name=(max(read, key=_score)[0] if read else ""),
            ))
    return out


def featured_columns(d: dict, n=4) -> list[tuple[str, str]]:
    """(column, example value) pairs worth showing for this source."""
    cols, rows = d["columns"], d["rows"]
    if not cols:
        return []
    want = FEATURED.get(d["src"])
    idx = []
    if want:
        low = [c.strip().lower() for c in cols]
        for w in want:
            wl = w.strip().lower()
            if wl in low:
                idx.append(low.index(wl))
    if not idx:
        # Fall back to columns that look like identity or a label.
        import re
        key = re.compile(r"(^|_)(id|name|code|title|status|symbol|date|"
                         r"number|term|significance)($|_)", re.I)
        idx = [i for i, c in enumerate(cols) if key.search(fold_col(c))][:n]
        if not idx:
            idx = list(range(min(n, len(cols))))
    idx = idx[:n]

    out = []
    row = rows[0] if rows else []
    for i in idx:
        val = row[i] if i < len(row) else ""
        val = (val[:44] + "…") if len(val) > 44 else val
        out.append((cols[i].strip()[:26], val or "—"))
    return out


def glossary_for(d: dict, limit=2) -> list[tuple[str, str]]:
    seen, out = set(), []
    for c in d["columns"][:60]:
        f = fold_col(c)
        if f in GLOSSARY and f not in seen:
            seen.add(f)
            out.append((c.strip(), GLOSSARY[f]))
        if len(out) >= limit:
            break
    return out


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return ""


# --------------------------------------------------------------------------
# Entities: what each label is, and where it comes from.
#
# Counts are READ FROM THE LIVE GRAPH, not written here. They move on every
# rebuild - the last one changed Disease by 1,085 as duplicate vocabulary
# entries merged, and the next will drop ClinicalTrial by roughly 33,000 as
# duplicate registry keys collapse. A number typed into a slide is correct
# until the first rebuild and wrong afterwards, silently, in front of an
# investor.
#
# The snapshot below is a fallback for building the deck without database
# access, and it is dated so a stale one is obvious.
FALLBACK_COUNTS = {
    "Substance": 3_070_258, "Identifier": 7_947_581,
    "ClinicalTrial": 1_049_701, "Variant": 937_377, "Product": 210_074,
    "Approval": 201_430, "Company": 159_409, "RegulatoryEvent": 27_069,
    "Disease": 23_403, "Target": 16_624, "Patent": 7_971, "DrugClass": 6_996,
    "AdverseEvent": 6_981, "Publication": 6_324, "Exclusivity": 2_344,
    "Mechanism": 1_967, "Country": 189, "Route": 145, "OrganClass": 27,
    "RegulatoryAgency": 11, "Modality": 11, "Region": 9,
}
FALLBACK_DATE = "2026-07-31"

_live: dict | None = None


def live_counts() -> dict:
    """label -> count, from Neo4j; the dated snapshot if it is unreachable.

    Each `MATCH (n:Label) RETURN count(n)` is answered from the count store in
    constant time, so all 22 plus the totals cost one round trip.
    """
    global _live
    if _live is not None:
        return _live
    try:
        sys.path.insert(0, str(ROOT / "testPipeline"))
        import ask as A                                      # noqa: PLC0415
        q = "\nUNION ALL\n".join(
            f"MATCH (n:{l}) RETURN '{l}' AS label, count(n) AS n"
            for l in sorted(NODE_COLUMNS))
        rows, _ms = A.run_cypher(q)
        _live = {r["label"]: r["n"] for r in rows}
        tot, _ = A.run_cypher("MATCH (n) RETURN count(n) AS n")
        edg, _ = A.run_cypher("MATCH ()-[r]->() RETURN count(r) AS n")
        _live["_nodes"], _live["_edges"] = tot[0]["n"], edg[0]["n"]
        _live["_source"] = "live"
    except Exception as e:                                   # noqa: BLE001
        print(f"  ! graph unreachable ({type(e).__name__}); using the "
              f"{FALLBACK_DATE} snapshot")
        _live = dict(FALLBACK_COUNTS)
        _live["_nodes"] = sum(FALLBACK_COUNTS.values())
        _live["_edges"] = 16_821_596
        _live["_source"] = f"snapshot {FALLBACK_DATE}"
    return _live


def count(label: str) -> str:
    n = live_counts().get(label)
    return f"{n:,}" if isinstance(n, int) else "—"


def millions(n: int) -> str:
    return f"{n/1_000_000:.1f}M" if n >= 1_000_000 else f"{n:,}"


# Kept as a name so existing slide code reads unchanged; now formatted from
# whatever live_counts() returned.
class _Counts(dict):
    def get(self, k, default="—"):
        return count(k) if k in live_counts() else default

    def __getitem__(self, k):
        return count(k)


NODE_COUNTS = _Counts()

NODE_ORIGIN = {
    "Substance": ("gsrs, ChEMBL molecules",
                  "GSRS supplies the UNII and the authoritative name; ChEMBL "
                  "adds 2.9M research molecules. Key is UNII where one exists."),
    "Identifier": ("every source that carries an external id",
                   "One node per external identifier, prefixed ID: so it can "
                   "never collide with the entity it identifies."),
    "ClinicalTrial": ("9 registries + WHO ICTRP",
                      "Keyed by registry id. WHO loads last and merges into "
                      "the native record rather than duplicating it."),
    "Variant": ("ClinVar, COSMIC",
                "Filtered hard: the gene must be a drug target and the "
                "significance an actual call. 21.8M rows in, 937k kept."),
    "Product": ("FDA, EMA, MHRA, Health Canada, PMDA, SFDA, Purple Book",
                "One node per agency product record, keyed by that agency's "
                "own id."),
    "Approval": ("the same agency registers",
                 "Kept separate from Product because one product carries many "
                 "dated authorisations."),
    "Company": ("agency registers, trial registries",
                "Names normalised so Pfizer Inc. and Pfizer Canada ULC do not "
                "become two companies."),
    "Disease": ("MeSH, ICD-11",
                "MeSH is the spine; ICD-11 is a second coding system on the "
                "same nodes. chembl_indications carries the MeSH/EFO "
                "crosswalk."),
    "Target": ("ChEMBL targets, UniProt, HGNC",
               "Keyed by UniProt accession where one exists; HGNC supplies "
               "the authoritative gene symbol."),
    "DrugClass": ("WHO ATC",
                  "The ATC tree, levels 1 to 5, including its own parent "
                  "hierarchy."),
    "AdverseEvent": ("openFDA FAERS, VigiAccess",
                     "MedDRA preferred terms, grouped under organ classes "
                     "supplied by VigiAccess."),
    "Publication": ("Europe PMC, PubMed, bioRxiv, medRxiv",
                    "Titles and metadata. OpenAlex is deferred on disk, not "
                    "excluded on merit."),
    "Patent": ("FDA Orange Book, Purple Book",
               "Essentially all patent data comes from Orange Book, which is "
               "now IP-blocked and therefore frozen."),
    "Mechanism": ("ChEMBL mechanisms + action types",
                  "Mechanism of action as ChEMBL states it. No inference."),
    "RegulatoryEvent": ("openFDA recalls and shortages, EMA",
                        "Recalls, shortages and safety communications."),
    "Country / Region": ("derived during product and trial loading",
                         "189 countries mapped into 9 regions, so a regional "
                         "question is one hop rather than a country list."),
}
