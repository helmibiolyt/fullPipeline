"""Variant, and the link from a mutation to a druggable target.

Variant attaches to the existing Target rather than introducing a Gene node.
HGNC already gives symbol -> UniProt accession, and Target is keyed by that
accession, so `variant -> gene -> target` collapses to `variant -> target` with
no new label and no second join to keep correct.

**Two filters, both necessary.** ClinVar's variant_summary is ~21.8M rows.
Loading it whole would roughly double an 11.8M-node graph with variants in
genes no drug touches, which is not what this graph is for:

  1. the gene must be a known drug target - otherwise VARIANT_IN has nothing to
     point at and the node is an island
  2. the clinical significance must be a real call - "Uncertain significance"
     is the single largest class in ClinVar and asserts nothing

Both counts are recorded in the manifest, so the cut is visible rather than
silently applied.

ClinVar's file has no header row: the first line is data. The standard column
order is supplied by position here. It currently carries 43 columns where the
documented schema has 34 - newer releases appended fields - so positions are
read from the front, where the order is stable, and never from the end.
"""
from __future__ import annotations

import re

import lake
from normalise import fold

CLINVAR = "Targets_Genomics_Biomarkers/ncbi.nlm.nih.gov/Variants/variant_summary.csv"
COSMIC_PREFIX = "Targets_Genomics_Biomarkers/cancer.sanger.ac.uk/Cancer_Site_Mutations/"

# ClinVar variant_summary, by position. Only the columns actually used are
# named; the file has 43 and the documented schema 34, so anything beyond
# VariationID is deliberately not relied on.
C_ALLELE_ID, C_TYPE, C_NAME = 0, 1, 2
C_GENE_SYMBOL, C_HGNC = 4, 5
C_SIGNIFICANCE = 6
C_PHENOTYPE_IDS, C_PHENOTYPE_LIST = 12, 13
C_VARIATION_ID = 30
C_MIN_COLUMNS = 31          # everything above must exist

# A significance that asserts something. "Uncertain significance" and
# "not provided" are the two largest classes and mean the submitters could not
# agree - carrying them would inflate the graph with non-statements.
MEANINGFUL = ("pathogenic", "likely pathogenic", "benign", "likely benign",
              "risk factor", "drug response", "association", "protective")

_MONDO = re.compile(r"MONDO:(MONDO:\d+)")
_MEDGEN = re.compile(r"MedGen:(C\w+)")


def _significant(raw: str) -> bool:
    s = (raw or "").strip().lower()
    if not s:
        return False
    return any(m in s for m in MEANINGFUL)


def load_clinvar(b):
    t0 = b._step("clinvar")
    n = kept = 0
    no_target = no_call = 0
    for row in lake.stream_rows(CLINVAR, limit=b.limit):
        n += 1
        if len(row) < C_MIN_COLUMNS:
            continue
        symbol = row[C_GENE_SYMBOL].strip().upper()
        tkey = b.symbol_target.get(symbol)
        if not tkey:
            no_target += 1
            continue
        sig = row[C_SIGNIFICANCE].strip()
        if not _significant(sig):
            no_call += 1
            continue

        vid = row[C_VARIATION_ID].strip() or row[C_ALLELE_ID].strip()
        if not vid:
            continue
        kept += 1
        vkey = f"CLINVAR:{vid}"
        b.w.node("Variant", vkey, source=CLINVAR, name=row[C_NAME].strip()[:300],
                 variant_type=row[C_TYPE].strip(), gene_symbol=symbol,
                 clinical_significance=sig, catalogue="clinvar", consequence="")
        b.w.identifier(vkey, "CLINVAR", vid, source=CLINVAR)
        b.w.edge("VARIANT_IN", vkey, tkey, match_method="symbol", source=CLINVAR)

        # PhenotypeIDS carries MONDO and MedGen ids; efo_mesh folds MONDO onto
        # MeSH where chembl_indications provided the crosswalk. Falling back to
        # the phenotype NAME would be a prose match against disease text and is
        # not worth the false links.
        ids = row[C_PHENOTYPE_IDS]
        for m in _MONDO.finditer(ids):
            raw = m.group(1).replace("_", ":").upper()
            dkey = b.efo_mesh.get(raw)
            if dkey:
                b.w.edge("IMPLICATED_IN", vkey, dkey, match_method="structured",
                         source=CLINVAR, significance=sig)
    b.stats["clinvar_rows"] = n
    b.stats["clinvar_kept"] = kept
    b.stats["clinvar_skipped_gene_not_a_target"] = no_target
    b.stats["clinvar_skipped_no_clinical_call"] = no_call
    b._done("clinvar", t0, kept)


def load_cosmic(b):
    """COSMIC somatic mutations, per cancer site.

    Discovered from S3 rather than listed: the lake holds 40 files today and a
    hardcoded tuple would silently ignore a 41st.
    """
    t0 = b._step("cosmic")
    kept = n = 0
    for key in lake.list_keys(COSMIC_PREFIX, ".csv"):
        for row in lake.stream_csv(key, limit=b.limit):
            n += 1
            symbol = (row.get("GeneName") or "").strip().upper()
            mid = (row.get("MutationID") or "").strip()
            tkey = b.symbol_target.get(symbol)
            if not tkey or not mid:
                continue
            kept += 1
            vkey = f"COSMIC:{mid}"
            b.w.node("Variant", vkey, source=key,
                     name=(row.get("MutationAA") or "").strip()[:120],
                     variant_type="somatic", gene_symbol=symbol,
                     clinical_significance="", catalogue="cosmic",
                     consequence=(row.get("MutationDescription") or "").strip())
            b.w.edge("VARIANT_IN", vkey, tkey, match_method="symbol", source=key)
    b.stats["cosmic_rows"] = n
    b.stats["cosmic_kept"] = kept
    b._done("cosmic", t0, kept)


def load_uniprot(b):
    """Enrichment only, on targets that already exist.

    uniprot in this lake is disease-scoped - 2,782 proteins across themed files
    - so it cannot be the source of targets, only extra properties on ones
    ChEMBL already established. Adding nodes from here would cap the graph at
    whichever diseases happened to be scraped.
    """
    t0 = b._step("uniprot")
    n = 0
    seen = b.w._seen.get(("nodes", "Target"), set())
    for key in lake.list_keys("Targets_Genomics_Biomarkers/uniprot.org/", ".csv"):
        if "/_runs/" in key:
            continue
        for row in lake.stream_csv(key, limit=b.limit):
            acc = (row.get("Entry") or "").strip()
            if not acc:
                continue
            tkey = f"UNIPROT:{acc}"
            if tkey not in seen:
                continue
            n += 1
            b.w.identifier(tkey, "UNIPROT_ENTRY", acc, source=key)
    b.stats["uniprot_enriched_targets"] = n
    b._done("uniprot", t0, n)


ALL = [load_clinvar, load_cosmic, load_uniprot]
