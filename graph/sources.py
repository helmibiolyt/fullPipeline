"""Which CSVs feed the graph, and why the rest do not.

The lake holds 436 CSVs across 49 sources, ~344M rows, 52.5 GB. Most of that
volume is irrelevant to a graph: two PubChem files alone are ~254M rows of
two-column lookups. This module is the explicit decision about what is loaded.

Read it as a document. Every entry names a real file from `csv_profile.txt` and
says which node or edge it builds; every exclusion says why. When a scraper
renames a column the fix belongs here, not in loader code - and `validate()`
fails the build if a declared file has vanished from the profile, so drift is
loud rather than silent.

Terms used in `builds`:
    node:<Label>        rows become nodes of that label
    edge:<TYPE>         rows become relationships
    id:<SCHEME>         rows become Identifier nodes + HAS_IDENTIFIER edges
    resolver            not loaded; read into memory to resolve names to keys
"""
from __future__ import annotations

CATEGORIES = (
    "Clinical_Trials_Pipeline_Intelligence",
    "Drug_Substance_Reference",
    "Literature_Evidence",
    "MENA_GCC_Regulatory_Market",
    "Ontologies_Standards",
    "Regulatory_Approvals",
    "Safety_Pharmacovigilance",
    "Targets_Genomics_Biomarkers",
)

# --------------------------------------------------------------------------
# Phase 1 - the substance/product spine and everything hanging off it.
# Ordered as the loaders run: vocabularies first, then entities, then edges.
# --------------------------------------------------------------------------

INCLUDED: list[dict] = [

    # ---- vocabularies: small, no resolution, load first --------------------
    dict(file="Drug_Substance_Reference/atcddd.fhi.no/atc_ddd_data/atc_classes.csv",
         rows=1_318, builds=["node:DrugClass"],
         key="atc_code",
         note="WHO ATC tree. parent_code gives the hierarchy directly."),

    dict(file="Drug_Substance_Reference/atcddd.fhi.no/atc_ddd_data/atc_substances.csv",
         rows=5_678, builds=["edge:IN_CLASS", "id:ATC"],
         note="substance name -> atc_code. Dictionary-matched to Substance."),

    dict(file="Regulatory_Approvals/health-products.canada.ca/canada_dpd_data/route.csv",
         rows=68_207, builds=["node:Route", "edge:HAS_ROUTE"],
         note="Route vocabulary and the product->route edge in one file."),

    # ---- Substance spine ---------------------------------------------------
    dict(file="Drug_Substance_Reference/gsrs.ncats.nih.gov/gsrs_data/gsrs_substances.csv",
         rows=173_356, builds=["node:Substance", "id:UNII", "id:CAS", "resolver"],
         key="UNII:{unii}",
         note="The naming authority. preferred_name + synonyms build the "
              "name->UNII lookup every other loader resolves through."),

    dict(file="Drug_Substance_Reference/ebi.ac.uk-chembl/chembl_data/chembl_molecules.csv",
         rows=2_877_005, builds=["node:Substance", "id:CHEMBL_ID", "node:Modality"],
         note="Pan-therapeutic molecule table. molecule_type feeds Modality."),

    dict(file="Drug_Substance_Reference/ebi.ac.uk-chembl/chembl_data/chembl_structures.csv",
         rows=3_096_691, builds=["id:INCHIKEY"],
         note="InChIKey is the strongest merge signal there is - it is the "
              "chemistry, not a name."),

    dict(file="Drug_Substance_Reference/ebi.ac.uk-chembl/chembl_data/chembl_synonyms.csv",
         rows=139_044, builds=["resolver"],
         note="Extends the name lookup beyond gsrs, notably brand names."),

    dict(file="Drug_Substance_Reference/ebi.ac.uk-chembl/chembl_data/chembl_molecule_hierarchy.csv",
         rows=3_508_134, builds=["resolver"],
         note="molregno -> parent_molregno. Salt forms as fact: atorvastatin "
              "calcium links to atorvastatin without guessing which trailing "
              "tokens are salts."),

    dict(file="Drug_Substance_Reference/rxnav.nlm.nih.gov/rxnorm_data/rxnorm_drugs.csv",
         rows=28_920, builds=["id:RXCUI"],
         note="RXCUI is a merge signal and the join to dailymed."),

    # ---- Target ------------------------------------------------------------
    dict(file="Drug_Substance_Reference/ebi.ac.uk-chembl/chembl_data/chembl_targets.csv",
         rows=18_552, builds=["node:Target"],
         note="The Target base. uniprot.org in this lake is disease-scoped "
              "(2,782 proteins in themed files), so it cannot be the spine."),

    dict(file="Drug_Substance_Reference/ebi.ac.uk-chembl/chembl_data/chembl_uniprot_mapping.csv",
         rows=17_257, builds=["id:UNIPROT"],
         note="chembl tid -> UniProt accession, which is the Target key."),

    dict(file="Targets_Genomics_Biomarkers/genenames.org/data/complete_set/hgnc_complete_set.csv",
         rows=42_397, builds=["node:Target"],
         note="Enrichment: symbol, name, gene family. uniprot_ids joins to the "
              "Target key."),

    # ---- Mechanism ---------------------------------------------------------
    dict(file="Drug_Substance_Reference/ebi.ac.uk-chembl/chembl_data/chembl_mechanisms.csv",
         rows=7_561, builds=["node:Mechanism", "edge:TARGETS", "edge:HAS_MECHANISM"],
         note="Carries molregno -> tid AND mechanism_of_action + action_type, "
              "so both edges come from one structured file. No inference."),

    dict(file="Drug_Substance_Reference/ebi.ac.uk-chembl/chembl_data/chembl_action_types.csv",
         rows=35, builds=["node:Mechanism"],
         note="agonist / antagonist / inhibitor vocabulary."),

    # ---- Disease -----------------------------------------------------------
    dict(file="Ontologies_Standards/meshb.nlm.nih.gov/mesh_data/mesh_descriptors.csv",
         rows=34_154, builds=["node:Disease", "edge:SUBTYPE_OF"],
         key="MESH:{descriptor_ui}",
         note="tree_numbers give the hierarchy: C04.588.322's parent is the "
              "descriptor owning C04.588."),

    dict(file="Ontologies_Standards/icd.who.int/icd_data/icd11_codes.csv",
         rows=None, builds=["node:Disease", "id:ICD11"],
         note="Second coding system on the same Disease nodes."),

    dict(file="Drug_Substance_Reference/ebi.ac.uk-chembl/chembl_data/chembl_indications.csv",
         rows=60_504, builds=["edge:INDICATED_FOR"],
         note="Carries mesh_id AND efo_id on the same row - the crosswalk that "
              "stops opentargets' EFO diseases and MeSH becoming two "
              "disconnected populations."),

    # ---- Product -----------------------------------------------------------
    dict(file="Regulatory_Approvals/health-products.canada.ca/canada_dpd_data/drug.csv",
         rows=59_339, builds=["node:Product"], key="CA:{DRUG_IDENTIFICATION_NUMBER}",
         note="Canada DPD is already a relational product schema - drug, "
              "ingred, comp, route, ther as separate tables. Cleanest source."),
    dict(file="Regulatory_Approvals/health-products.canada.ca/canada_dpd_data/ingred.csv",
         rows=125_922, builds=["edge:CONTAINS"], note="product -> active ingredients."),
    dict(file="Regulatory_Approvals/health-products.canada.ca/canada_dpd_data/comp.csv",
         rows=59_384, builds=["node:Company", "edge:DEVELOPS"], note=""),
    dict(file="Regulatory_Approvals/health-products.canada.ca/canada_dpd_data/ther.csv",
         rows=58_174, builds=["edge:IN_CLASS"], note="TC_ATC -> DrugClass."),
    dict(file="Regulatory_Approvals/health-products.canada.ca/canada_dpd_data/status.csv",
         rows=200_119, builds=["node:Approval"], note=""),

    dict(file="Regulatory_Approvals/products.mhra.gov.uk/mhra_data/raw_metadata.csv",
         rows=78_215, builds=["node:Product", "id:MHRA_PL"], key="MHRA:{pl_number}",
         note="The UK product population - the same documents the vector store "
              "indexes, joined here by licence number."),

    dict(file="Regulatory_Approvals/ema.europa.eu/ema_data/ema_medicines.csv",
         rows=2_666, builds=["node:Product", "id:EMA_PRODUCT", "edge:CONTAINS",
                             "edge:IN_CLASS", "node:Company"],
         key="EMA:{ema_product_number}",
         note="39 columns: active_substance, MAH, atc_code, therapeutic area. "
              "Small but dense - centrally authorised EU products."),

    dict(file="Regulatory_Approvals/accessdata.fda.gov-orangebook/orangebook_data/orange_book_unified.csv",
         rows=47_497, builds=["node:Product", "id:FDA_APPL_NO", "edge:CONTAINS",
                              "node:Company", "node:Approval"],
         key="FDA:{Appl_No}:{Product_No}",
         note="Chosen over products.csv: same rows, 18 columns instead of 14, "
              "including Applicant_Full_Name and TE_Code."),

    dict(file="Regulatory_Approvals/purplebooksearch.fda.gov/purplebook_data/purplebook_enriched_products.csv",
         rows=2_205, builds=["node:Product", "node:Approval", "edge:BIOSIMILAR_OF"],
         key="FDA:BLA{bla_number}",
         note="license_type 351(a)/351(k) plus resolved_reference_bla gives the "
              "biosimilar->originator edge already resolved."),

    dict(file="Regulatory_Approvals/open.fda.gov/openfda_data/openfda_drugs.csv",
         rows=42_173, builds=["node:Product", "id:NDC"], note=""),

    dict(file="Regulatory_Approvals/pmda.go.jp/pmda_data/pmda_metadata.csv",
         rows=547, builds=["node:Product"], key="PMDA:{...}", note="Japan."),

    dict(file="MENA_GCC_Regulatory_Market/sfda.gov.sa/List_of_Registered_HumanHerbalVeterinary_Drugs.csv",
         rows=9_170, builds=["node:Product", "edge:CONTAINS", "edge:IN_CLASS"],
         note="36 columns incl. scientificName, atcCode1/2, registerNumber. "
              "Headers carry a UTF-8 BOM - strip it."),

    dict(file="Drug_Substance_Reference/dailymed.nlm.nih.gov/dailymed_data/dailymed_master_mapping.csv",
         rows=319_885, builds=["id:SPL_SETID", "id:NDC"], note=""),

    # ---- Patent / Exclusivity ---------------------------------------------
    dict(file="Regulatory_Approvals/accessdata.fda.gov-orangebook/orangebook_data/patents_enriched.csv",
         rows=16_344, builds=["node:Patent", "edge:PROTECTED_BY"],
         key="US:{Patent_No}",
         note="Joins to Product on Appl_No + Product_No. Patent_Use_Code "
              "distinguishes a substance patent from a formulation one."),

    dict(file="Regulatory_Approvals/accessdata.fda.gov-orangebook/orangebook_data/exclusivity_enriched.csv",
         rows=2_265, builds=["node:Exclusivity", "edge:HAS_EXCLUSIVITY"], note=""),

    dict(file="Regulatory_Approvals/purplebooksearch.fda.gov/purplebook_data/patent_list.csv",
         rows=424, builds=["node:Patent", "edge:PROTECTED_BY"],
         note="Biologics patents, joined on BLA number."),

    # ---- RegulatoryEvent (one node, many sources) --------------------------
    dict(file="Regulatory_Approvals/ema.europa.eu/ema_data/additional_tables/referrals.csv",
         rows=591, builds=["node:RegulatoryEvent", "edge:SUBJECT_OF"], note="type=referral"),
    dict(file="Regulatory_Approvals/ema.europa.eu/ema_data/additional_tables/shortages.csv",
         rows=82, builds=["node:RegulatoryEvent", "edge:SUBJECT_OF"], note="type=shortage"),
    dict(file="Regulatory_Approvals/ema.europa.eu/ema_data/additional_tables/orphan_designations.csv",
         rows=3_279, builds=["node:RegulatoryEvent", "edge:SUBJECT_OF"], note="type=orphan_designation"),
    dict(file="Regulatory_Approvals/ema.europa.eu/ema_data/additional_tables/dhpc.csv",
         rows=171, builds=["node:RegulatoryEvent", "edge:SUBJECT_OF"], note="type=dhpc"),
    dict(file="Regulatory_Approvals/ema.europa.eu/ema_data/additional_tables/paediatric_investigation_plans.csv",
         rows=3_373, builds=["node:RegulatoryEvent", "edge:SUBJECT_OF"], note="type=pip"),
    dict(file="Safety_Pharmacovigilance/open.fda.gov/Drug_Recalls/drug_recalls.csv",
         rows=17_718, builds=["node:RegulatoryEvent", "edge:SUBJECT_OF"],
         note="type=recall. classification I/II/III is the severity."),
    dict(file="MENA_GCC_Regulatory_Market/sfda.gov.sa/Safety_Alert.csv",
         rows=260, builds=["node:RegulatoryEvent"], note="type=safety_alert"),
    dict(file="MENA_GCC_Regulatory_Market/sfda.gov.sa/Shortage_Drugs_List.csv",
         rows=1_840, builds=["node:RegulatoryEvent"], note="type=shortage"),

    # ---- AdverseEvent (aggregated, not per report) -------------------------
    *[dict(file=f"Safety_Pharmacovigilance/open.fda.gov/Adverse_Events/faers_{p}.csv",
           rows=None, builds=["node:AdverseEvent", "edge:HAS_ADVERSE_EVENT"],
           note="AGGREGATE at load: one edge per (drug_substance, reaction) "
                "with report/serious/death counts. Never one node per report.")
      for p in ("2020", "2021", "2022", "2023", "2024Q1", "2024Q2", "2024Q3",
                "2024Q4", "2025Q1", "2025Q2", "2025Q3_Q4", "2026Q1")],

    # ---- ClinicalTrial -----------------------------------------------------
    dict(file="Clinical_Trials_Pipeline_Intelligence/clinicaltrials.gov/clinicaltrials_data/clinicaltrials_all.csv",
         rows=606_658, builds=["node:ClinicalTrial", "node:Company", "edge:SPONSORED_BY",
                               "edge:STUDIES", "edge:TESTED_IN", "edge:CONDUCTED_IN"],
         key="NCT:{nct_id}",
         note="2.7 GB - stream it. conditions and interventions are prose and "
              "go through the dictionary matcher, not a direct join."),

    dict(file="Clinical_Trials_Pipeline_Intelligence/trialsearch.who.int/who_trials_csv/who_trials.csv",
         rows=1_011_870, builds=["node:ClinicalTrial", "edge:SAME_STUDY_AS"],
         note="4.6 GB. Carries cross-registry ids, so it drives de-duplication. "
              "Without it the same study counts once per registry."),

    dict(file="Clinical_Trials_Pipeline_Intelligence/clinicaltrialsregister.eu/eu_ctr_trials/eu_ctr_all_trials.csv",
         rows=97_822, builds=["node:ClinicalTrial"], note=""),
    dict(file="Clinical_Trials_Pipeline_Intelligence/chictr.org.cn/chictr_trails2/chictr_detailed.csv",
         rows=217_497, builds=["node:ClinicalTrial"], note=""),
    dict(file="Clinical_Trials_Pipeline_Intelligence/anzctr.org.au/anzctr_trials/anzctr_trials.csv",
         rows=40_957, builds=["node:ClinicalTrial"], note=""),
    dict(file="Clinical_Trials_Pipeline_Intelligence/isrctn.com/isrctn_trials/ISRCTN_search_results.csv",
         rows=31_373, builds=["node:ClinicalTrial"], note=""),
    dict(file="Clinical_Trials_Pipeline_Intelligence/euclinicaltrials.eu/ctis_data/CTIS_trials_20260622.csv",
         rows=10_158, builds=["node:ClinicalTrial"], note=""),
    dict(file="Clinical_Trials_Pipeline_Intelligence/ctri.nic.in/ctri_trials/ctri_trials.csv",
         rows=8_197, builds=["node:ClinicalTrial"], note=""),
    dict(file="Clinical_Trials_Pipeline_Intelligence/jrct.mhlw.go.jp/jrct_trials/jrct_list.csv",
         rows=476, builds=["node:ClinicalTrial"], note=""),

    # ---- Target/Disease associations --------------------------------------
    *[dict(file=f"Targets_Genomics_Biomarkers/platform.opentargets.org/Disease_Associations/{d}_targets.csv",
           rows=None, builds=["edge:ASSOCIATED_WITH"],
           note="overall_score becomes an edge property. Disease-scoped, so "
                "enrichment on top of chembl rather than the base.")
      for d in ("Alzheimer", "Cancer", "Cardiovascular", "Diabetes",
                "Infectious_Disease", "Respiratory")],

    # ---- Publication -------------------------------------------------------
    dict(file="Literature_Evidence/europepmc.org/europe_pmc/europe_pmc_metadata.csv",
         rows=None, builds=["node:Publication", "id:PMID", "id:DOI",
                            "edge:ABOUT", "edge:MENTIONS"],
         note="66 MB and the largest of the four remaining literature sources. "
              "Same 23 columns as pubmed_metadata.csv."),
    dict(file="Literature_Evidence/pubmed.ncbi.nlm.nih.gov/pubmed/pubmed_metadata.csv",
         rows=1_032, builds=["node:Publication"], note="Same shape as Europe PMC."),
    dict(file="Literature_Evidence/biorxiv.org/biorxiv/biorxiv_metadata.csv",
         rows=2_576, builds=["node:Publication"],
         note="Preprints: keyed by DOI, no PMID and no journal."),
    dict(file="Literature_Evidence/medrxiv.org/medrxiv/medrxiv_metadata.csv",
         rows=1_741, builds=["node:Publication"], note="As biorxiv."),

    # ---- Variant -----------------------------------------------------------
    dict(file="Targets_Genomics_Biomarkers/ncbi.nlm.nih.gov/Variants/variant_summary.csv",
         rows=None, builds=["node:Variant", "id:CLINVAR", "edge:VARIANT_IN",
                            "edge:IMPLICATED_IN"],
         note="~21.8M rows and NO HEADER ROW - the first line is data, so "
              "columns are read by position. Filtered hard: the gene must be a "
              "known drug target and the significance must be an actual call."),

    # A trailing slash declares a PREFIX, not a file. These loaders discover
    # their inputs from S3 rather than naming them, so a new cancer site or
    # disease-protein file is picked up automatically - and listing today's
    # filenames here would go stale the moment one is added.
    dict(file="Targets_Genomics_Biomarkers/cancer.sanger.ac.uk/Cancer_Site_Mutations/",
         rows=None, builds=["node:Variant", "edge:VARIANT_IN"],
         note="COSMIC somatic mutations, one file per cancer site. Kept only "
              "where the gene is a known drug target."),

    dict(file="Targets_Genomics_Biomarkers/uniprot.org/",
         rows=None, builds=["id:UNIPROT_ENTRY"],
         note="Enrichment only, on targets ChEMBL already established. "
              "Disease-scoped, so it cannot be the source of Target nodes - "
              "see the EXCLUDED note that used to cover this."),

    dict(file="Safety_Pharmacovigilance/vigiaccess.org/VigiAccess/vigiaccess_adr.csv",
         rows=None, builds=["node:OrganClass", "edge:IN_ORGAN_CLASS"],
         note="The MedDRA System Organ Class per reaction - the hierarchy "
              "meddra.org itself does not provide. Counts are VigiBase, not "
              "FAERS, and are deliberately not merged with FAERS counts."),

    dict(file="Targets_Genomics_Biomarkers/platform.opentargets.org/Drugs/known_drugs.csv",
         rows=203_100, builds=["edge:INDICATED_FOR", "edge:TARGETS"],
         note="Second source for both; chembl is the base."),
]


# --------------------------------------------------------------------------
# Deliberately not loaded. Each entry says why, so the decision can be
# revisited rather than rediscovered.
# --------------------------------------------------------------------------

EXCLUDED: dict[str, str] = {
    "Drug_Substance_Reference/ebi.ac.uk-chembl/chembl_data/chembl_usan_stems.csv":
        "834 USAN stems. Loaded briefly as Modality and withdrawn: the "
        "annotations are not one concept - '-mab' is 'monoclonal antibodies' "
        "(modality), '-kinra' is 'interleukin receptor antagonists' "
        "(mechanism), '-prazole' is a pharmacologic class - so any single "
        "label misfiles most of them. As Modality it put 397 class "
        "descriptions beside 9 real modalities. A stem also has several rows "
        "with sub-variants ('-mab' as 'monoclonal antibodies', '...: "
        "chimeric', '...: humanized') that a name suffix cannot tell apart, "
        "so first-match labelled humanized antibodies chimeric by list order. "
        "Everything it approximated is stated elsewhere: Modality from ChEMBL "
        "molecule_type, DrugClass from ATC, Mechanism from chembl_mechanisms. "
        "Revisit only with the stem table hand-classified into those three - "
        "that is curation, not a load.",

    "Drug_Substance_Reference/pubchem.ncbi.nlm.nih.gov":
        "~291M rows across 5 files, but the two big ones are 2-column lookups "
        "(CID->SMILES 133.9M, CID->parent 119.1M). They are 85% of the lake's "
        "rows and would dominate the graph. chembl_structures already supplies "
        "InChIKey, which is the identifier that matters. Revisit only if a "
        "PubChem CID crosswalk is specifically needed.",

    "Targets_Genomics_Biomarkers/ncbi.nlm.nih.gov":
        "ClinVar variants, ~21.8M rows. There is no Variant node in the schema. "
        "Adding one is a real extension (variant -> gene -> target -> disease), "
        "not a load - so it is a schema decision first.",

    "Ontologies_Standards/loinc.org":
        "4.4M rows of laboratory observation codes across 56 files. Nothing in "
        "the schema consumes them; LOINC describes measurements, not drugs.",

    "Ontologies_Standards/evs.nci.nih.gov":
        "NCI Thesaurus, 413k rows. A third disease vocabulary after MeSH and "
        "ICD-11. Two coding systems already crosswalk via chembl_indications; a "
        "third adds mapping burden without new coverage.",

    "Targets_Genomics_Biomarkers/cancer.sanger.ac.uk":
        "COSMIC gene lists, 40 files / 20k rows. Gene-level cancer census - "
        "belongs with a Variant/Gene extension, not the current schema.",

    "Targets_Genomics_Biomarkers/cbioportal.org":
        "Planned as a third variant source and is not one. The 23 files are "
        "study and clinical metadata - per-patient TCGA cohorts - plus "
        "cancer_types.csv, a cancer-type tree keyed by cbioportal's own ids "
        "with a `parent` column that references those ids and nothing else. "
        "Loading it would add a fourth disease vocabulary that crosswalks to "
        "neither MeSH nor MONDO, so it would sit beside Disease unconnected. "
        "COSMIC and ClinVar supply the variants.",

    "Literature_Evidence/openalex.org":
        "openalex 7.9M works, europepmc, pubmed, biorxiv, medrxiv. These build "
        "the Publication node, which is Phase 2. Deferred deliberately, not "
        "excluded - the openalex file alone is 17.5 GB and would dominate a "
        "first build.",

    "Safety_Pharmacovigilance/adrreports.eu, meddra.org":
        "meddra.org holds no terminology. The scrape reached only public pages "
        "- meddra_timeline.csv is news announcements and meddra_versions.csv is "
        "release history - because MedDRA itself is licensed. It was planned as "
        "the source of the reaction hierarchy and cannot be; vigiaccess "
        "publishes reactions already grouped by System Organ Class and is "
        "loaded instead. adrreports.eu is an index, not data: "
        "adrreports_substances.csv is name + EMA code + a report URL, with no "
        "reaction or count in it. Worth revisiting only to add EudraVigilance "
        "substance codes as identifiers.",

    "Regulatory_Approvals/ema.europa.eu/.../herbal_medicines.csv, "
    "maximum_residue_limits.csv, opinions_outside_eu.csv":
        "799 rows. Herbal monographs and veterinary residue limits sit outside "
        "the Substance/Product model.",

    "Regulatory_Approvals/purplebooksearch.fda.gov/.../purplebook_raw_monthly.csv":
        "The header row is a report title ('Purple Book Monthly Historical Data "
        "Changes Report - June 2026'), so the file has no usable column names. "
        "purplebook_enriched_products.csv carries the same content parsed.",

    "Regulatory_Approvals/health-products.canada.ca/.../vet.csv":
        "6,982 veterinary products. Out of scope for a human-medicines graph.",

    "Ontologies_Standards/cdisc.org, MENA_GCC_Regulatory_Market/nupco.com":
        "198 rows combined. Standards lists and procurement catalogues with no "
        "node to attach to.",

    "*/Index/*_documents.csv, mhra_documents.csv, ema_documents.csv":
        "Document indexes - filename, URL, product name. These describe the "
        "PDFs the vector store already indexes. The graph joins to those "
        "documents by product key, so a second copy of the index adds nothing.",
}


def validate(profile_path: str = "graph/csv_profile.txt") -> list[str]:
    """Fail loudly when a declared file is no longer in the lake.

    Scrapers rename things - SFDA renamed two files this week. Without this
    check a renamed file silently produces zero rows and the graph quietly
    loses a source, which is far worse than a build that stops.
    """
    import re
    txt = open(profile_path, encoding="utf-8", errors="replace").read()
    present = set()
    src = None
    for line in txt.splitlines():
        if line.startswith("## "):
            src = line[3:].strip()
        elif line.startswith("-- ") and src:
            m = re.match(r"-- (\S+)", line)
            if m:
                present.add(f"{src}/{m.group(1)}")
    return [e["file"] for e in INCLUDED if e["file"] not in present]


def summary() -> str:
    from collections import Counter
    builds = Counter()
    for e in INCLUDED:
        for b in e["builds"]:
            builds[b] += 1
    known = sum(e["rows"] for e in INCLUDED if e.get("rows"))
    lines = [f"{len(INCLUDED)} files included, {len(EXCLUDED)} source groups excluded",
             f"~{known:,} rows declared (files with a counted estimate)", ""]
    for b, n in sorted(builds.items()):
        lines.append(f"  {n:>2} file(s) -> {b}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(summary())
    missing = validate()
    print("\nfiles declared but absent from the profile:")
    print("\n".join(f"  MISSING  {m}" for m in missing) or "  none")
