# Graph schema — final specification

**Phase 1: 15 nodes · 19 relationships.  Phase 2: 20 nodes · 27 relationships.**
Derived from a column-level profile of all 436 CSVs in `s3://moine-data`
(52.5 GB, ~344M rows — see `csv_profile.txt`).

Change from `stucture.jpg`: the original `Drug` node is split into
**`Substance`** (the molecule / active ingredient) and **`Product`** (the
marketed, approved item). Everything else is unchanged. Rationale in §4.

---

## 1. Nodes

| # | label | key | key properties | built from |
|---|---|---|---|---|
| 1 | **Substance** | `UNII:<unii>` or `NAME:<norm_name>` | `name`, `norm_name`, `substance_class`, `status`, `max_phase` | gsrs `gsrs_substances` (173k), **chembl `chembl_molecules.csv` (~2.4M) + `chembl_structures.csv` (InChIKey)**, atcddd, pubchem, rxnav |
| 2 | **Product** | `<AGENCY>:<local_id>` e.g. `MHRA:PL12345` | `name`, `agency`, `status`, `status_raw`, `form`, `strength` | ema `ema_medicines`, mhra `raw_metadata`, canada `drug.csv`, orangebook `orange_book_unified`, pmda `pmda_metadata`, dailymed `dailymed_catalog` |
| 3 | **ClinicalTrial** | `<REGISTRY>:<id>` e.g. `NCT:NCT01234567` | `title`, `status`, `phase`, `study_type`, `enrollment`, `start_date` | ctgov, who, eu_ctr, ctri, chictr, anzctr, isrctn, jrct, ctis |
| 4 | **Company** | `norm(name)` | `name`, `raw_names[]` | ctgov `lead_sponsor`, ema MAH, orangebook `Applicant`, canada `comp.COMPANY_NAME`, all registries |
| 5 | **Disease** | `MESH:<descriptor_ui>` (also `EFO:`, `ICD:`) | `name`, `synonyms[]`, `tree_numbers[]` | meshb `mesh_descriptors`, icd `icd11_codes`, opentargets `disease_id`; **chembl `chembl_indications.csv` bridges EFO↔MeSH** |
| 6 | **Target** | `<uniprot_accession>` | `symbol`, `name`, `organism`, `ec_number` | uniprot `Entry`, genenames `uniprot_ids`/`symbol` |
| 7 | **Country** | `<iso2>` | `name` | ISO-3166 vocab; values from trial registries |
| 8 | **Region** | `<name>` | `name` | derived from agency jurisdiction |
| 9 | **RegulatoryAgency** | `<code>` | `name`, `country`, `region` | fixed vocab (11): FDA, EMA, MHRA, PMDA, HC, SFDA, NHRA, DHA, DOH, MOH-OM, MOPH-QA |
| 10 | **Approval** | `<AGENCY>:<appl_no>:<date>` | `date`, `type`, `status` | orangebook, ema, canada `status.csv`, pmda, mhra |
| 11 | **Identifier** | `(scheme, value)` node key | `scheme`, `value` | see §3 |
| 12 | **DrugClass** | `<atc_code>` | `name`, `level` | atcddd `atc_ddd_full` / `atc_classes` |
| 13 | **Mechanism** | `<name>` | `name`, `action_type` | **chembl `chembl_mechanisms.csv` + `chembl_action_types.csv`**, opentargets |
| 14 | **Modality** | `<name>` | `name` | **chembl `chembl_usan_stems.csv` + `molecule_type`**, opentargets |
| 15 | **Route** | `<name>` | `name` | canada `route.csv`, atcddd `adm_route`, orangebook `Dosage_Form_Route` |
| | | | *— Phase 2 below —* | |
| 16 | **AdverseEvent** | `MEDDRA:<norm_pt>` | `term`, `soc` | openfda `faers_*.csv` `reaction` (~2.9M reports), vigiaccess |
| 17 | **Publication** | `DOI:<doi>` or `PMID:<id>` | `title`, `year`, `journal` | europepmc, pubmed, openalex, biorxiv, medrxiv |
| 18 | **Patent** | `US:<patent_no>` | `patent_no`, `expire_date`, `use_code`, `use_definition`, `drug_substance_flag`, `drug_product_flag` | orangebook `patents_enriched.csv` (~16.3k), purplebook `patent_list.csv` (424) |
| 19 | **Exclusivity** | `<appl_no>:<product_no>:<code>` | `code`, `date`, `definition`, `kind` | orangebook `exclusivity_enriched.csv` (2,265), purplebook 4 exclusivity date columns |
| 20 | **RegulatoryEvent** | `<AGENCY>:<type>:<reference>` | `type`, `status`, `reason`, `start_date`, `end_date`, `url` | ema `referrals`/`shortages`/`orphan_designations`/`dhpc`, openfda `drug_recalls` (~17.7k), sfda `Safety_Alert`/`Shortage_Drugs_List`/`Risk_Minimization_Measures_List` |

## 2. Relationships

| # | pattern | source | method |
|---|---|---|---|
| 1 | `(Product)-[:CONTAINS]->(Substance)` | canada `ingred.csv`, orangebook `Ingredient`, ema `active_substance`, mhra `substance_name`, pmda `generic_name` | dictionary |
| 2 | `(Company)-[:DEVELOPS]->(Product)` | orangebook `Applicant`, ema MAH, canada `comp.csv` | structured |
| 3 | `(ClinicalTrial)-[:SPONSORED_BY]->(Company)` | ctgov `lead_sponsor`+`collaborators`, who, anzctr, ctri, chictr, isrctn, jrct, ctis | structured |
| 4 | `(ClinicalTrial)-[:STUDIES]->(Disease)` | ctgov `conditions`, chictr `hc_freetext`, ctri, who | dictionary |
| 5 | `(ClinicalTrial)-[:CONDUCTED_IN]->(Country)` | anzctr, chictr `countries`, ctri, isrctn, who, ctgov | structured |
| 6 | `(Substance)-[:TESTED_IN]->(ClinicalTrial)` | ctgov `interventions`, chictr `i_freetext`, anzctr | dictionary |
| 7 | `(Substance)-[:INDICATED_FOR]->(Disease)` | **chembl `chembl_indications.csv`** (`mesh_id`+`efo_id`), opentargets `known_drugs`, ema `therapeutic_area_mesh` | structured |
| 8 | `(Substance)-[:TARGETS]->(Target)` | **chembl `chembl_mechanisms.csv`** (`molregno`→`tid`), opentargets, uniprot | structured |
| 9 | `(Target)-[:ASSOCIATED_WITH]->(Disease)` | opentargets `Disease_Associations/*.csv` (+`overall_score`) | structured |
| 10 | `(Disease)-[:SUBTYPE_OF]->(Disease)` | meshb `tree_numbers` | computed |
| 11 | `(Substance)-[:HAS_MECHANISM]->(Mechanism)` | **chembl `chembl_mechanisms.csv`** (`mechanism_of_action` + `action_type`), opentargets | structured |
| 12 | `(Substance)-[:IN_CLASS]->(DrugClass)` | atcddd, ema `atc_code_human`, canada `ther.TC_ATC` | structured |
| 13 | `(Substance)-[:HAS_MODALITY]->(Modality)` | **chembl `chembl_usan_stems.csv`** (-mab/-tide/-ciclib) + `molecule_type`, opentargets `known_drugs.type` | derived |
| 14 | `(Product)-[:HAS_ROUTE]->(Route)` | canada `route.csv`, orangebook `Dosage_Form_Route` | structured |
| 15 | `(Substance\|Product)-[:HAS_IDENTIFIER]->(Identifier)` | see §3 | structured |
| 16 | `(Product)-[:HAS_APPROVAL]->(Approval)` | orangebook, ema, canada, pmda, mhra | structured |
| 17 | `(Product)-[:APPROVED_BY]->(RegulatoryAgency)` | derived from source | derived |
| 18 | `(Approval)-[:ISSUED_BY]->(RegulatoryAgency)` | derived from source | derived |
| 19 | `(Product)-[:APPROVED_IN]->(Region)` | derived from agency | derived |

### Phase 2 relationships

| # | pattern | source | method |
|---|---|---|---|
| 20 | `(Disease)-[:SUBTYPE_OF]->(Disease)` self-loop | meshb tree numbers | computed |
| 21 | `(Substance)-[:HAS_ADVERSE_EVENT]->(AdverseEvent)` | openfda faers — **aggregated** to one edge per (substance, reaction) carrying `report_count`, `serious_count`, `death_count`, `first_report`, `last_report` | structured |
| 22 | `(Publication)-[:ABOUT]->(Disease)` | europepmc/pubmed `mesh_terms` | dictionary |
| 23 | `(Publication)-[:MENTIONS]->(Substance)` | literature abstracts | dictionary |
| 24 | `(Product)-[:PROTECTED_BY]->(Patent)` | orangebook `patents_enriched` on `Appl_No`+`Product_No`; purplebook `patent_numbers` | structured |
| 25 | `(Product)-[:HAS_EXCLUSIVITY]->(Exclusivity)` | orangebook `exclusivity_enriched`; purplebook exclusivity columns | structured |
| 26 | `(Product\|Substance)-[:SUBJECT_OF]->(RegulatoryEvent)` | ema referrals/shortages/orphan designations, openfda recalls, sfda alerts | structured |
| 27 | `(Product)-[:BIOSIMILAR_OF]->(Product)` | purplebook `resolved_reference_bla` + `license_type` (`351(k)` → `351(a)`) | structured |

Utility edge (not part of the counts): `(ClinicalTrial)-[:SAME_STUDY_AS]-(ClinicalTrial)`
from `who_trials` cross-references, so the same study registered in several
registries is linked rather than merged.

Every relationship carries `source`, `run_id`, `committed_at`; dictionary-matched
edges additionally carry `match_method` (`exact` | `synonym`) so precision is
measurable.

## 3. Identifier schemes

| scheme | attaches to | source column |
|---|---|---|
| `UNII` | Substance | gsrs `unii` |
| `CAS` | Substance | gsrs `cas_number` |
| `INCHIKEY` | Substance | **chembl `chembl_structures.csv` (~2.4M)**, pubchem `InChIKey` |
| `CHEMBL_ID` | Substance | chembl `chembl_molecules.csv` |
| `CHEMBL_TARGET` | Target | chembl `chembl_uniprot_mapping.csv` |
| `PUBCHEM_CID` | Substance | pubchem `CID` |
| `RXCUI` | Substance | rxnav `ingredient_rxcui` |
| `ATC` | Substance | atcddd `atc_code` |
| `SPL_SETID` | Product | dailymed `setid` |
| `NDC` | Product | dailymed `ndc_list` |
| `MHRA_PL` | Product | mhra `pl_number` |
| `EMA_PRODUCT` | Product | ema `ema_product_number` |
| `CA_DIN` | Product | canada `DRUG_IDENTIFICATION_NUMBER` |
| `FDA_APPL_NO` | Product | orangebook `Appl_No` |

Shared identifiers are also the merge signal: two `Substance` nodes pointing at
the same `UNII`/`INCHIKEY`/`RXCUI` are the same substance (see `GRAPH_HOW.md` §7).

## 4. Why Drug was split

The sources are themselves two-level, and collapsing them loses information:

- **Canada DPD** is a product table (`drug.csv`: `DRUG_CODE`, `BRAND_NAME`) with
  ingredients in a separate table (`ingred.csv`), plus `comp.csv`, `route.csv`,
  `ther.csv`. The two-level model is literally the source schema.
- **Orange Book** carries `Ingredient`, `Trade_Name` and `Appl_No` on one row.
- **EMA** separates `active_substance` from `name_of_medicine`.

Three things break under a single `Drug` node:

1. **Combination products.** Co-amoxiclav is amoxicillin + clavulanic acid —
   representable only as `Product CONTAINS` two `Substance` nodes.
2. **One molecule, many products.** Atorvastatin is marketed by different
   companies in different countries under different approvals. Hanging
   `DEVELOPS` and `HAS_APPROVAL` off the molecule is factually wrong.
3. **Route, strength and form are product properties**, not molecule properties.

There is also a practical win: **`Product` never needs cross-source resolution**
— its key is the agency's own ID. Only `Substance` needs resolving, and gsrs's
173k UNIIs with synonyms is a sound authority for that.

## 5. Known limits at build time

*Updated 2026-07-26: chembl now holds 14 CSVs / 493.8 MB (was 55.9 KB of sample
queries). Two of the three risks previously listed here are resolved.*

**Resolved**

- `TARGETS`, `HAS_MECHANISM`, `INDICATED_FOR`, `HAS_MODALITY` are no longer
  opentargets-only. chembl supplies them pan-therapeutically:
  `chembl_mechanisms.csv` (drug → target + mechanism + action type),
  `chembl_indications.csv` (drug → MeSH), `chembl_usan_stems.csv` (Modality).
  opentargets remains a second source with association scores.
- **EFO ↔ MeSH crosswalk.** `chembl_indications.csv` carries `mesh_id` **and**
  `efo_id` on the same row, so opentargets' EFO diseases and meshb's MeSH
  descriptors join directly instead of splitting into two populations.

**Still open**

- `Company` is name-clustered — no registry identifier exists in any source.
  Expect this to be the least accurate node type.
- opentargets and uniprot remain disease-scoped; chembl does not have that
  limitation, so use chembl as the base and opentargets as enrichment.
- `purplebook_raw_monthly.csv` has a report title as its header row; excluded
  until its parser is fixed.
- Cross-registry trial duplication: the same study appears in several
  registries. `who_trials` must drive `SAME_STUDY_AS` or trial counts inflate.


---

## 6. Why the Phase-2 additions

Added after re-profiling the lake on 2026-07-28, which surfaced four FDA sources
the earlier profile had missed. They are absent from the vector store only
because it indexes documents and all of this is CSV.

### Patent and Exclusivity — the change of purpose

Orange Book carries ~16,344 patents with expiry dates, use codes and
substance/product flags; Purple Book adds 424 for biologics plus four kinds of
exclusivity per product. Together they answer a question nothing else in the
platform can:

> *when does this product lose protection, and which patents block a generic?*

The vector store cannot answer it — an SPC does not discuss patents. The graph
can, in one traversal, and patent-cliff timing is the sort of question the
platform exists to serve. This is the highest-value addition of the five.

`Exclusivity` is a node rather than a property so that "everything expiring in
2027" is a traversal rather than a scan of every product.

### BIOSIMILAR_OF

Purple Book resolves it for us: `license_type` distinguishes `351(a)`
originators from `351(k)` biosimilars, and `resolved_reference_bla` names the
reference product directly. No inference needed, and the competitive
relationship between an originator and its biosimilars is not derivable from
anything else in the lake.

### AdverseEvent — aggregate at load

FAERS carries `reaction` (MedDRA preferred term) alongside `drug_substance`,
seriousness flags and outcome, across ~2.9M reports spanning 2020-2026Q1.

**Load as counts, not reports.** One edge per (substance, reaction) pair with
`report_count`, `serious_count` and `death_count` answers every question the
individual reports would, without adding 2.9M nodes that dominate the graph.

This complements the vector store rather than duplicating it: an SPC says a
reaction is "rare"; FAERS says how often it was actually reported.

### RegulatoryEvent — one node, not seven

Referrals, shortages, orphan designations, DHPCs, recalls, safety alerts and
risk-minimisation measures are the same concept — *something an agency did about
a product* — recorded by different authorities in different tables. Seven node
types would fragment it and make cross-agency queries awkward; one node with a
`type` discriminator keeps "what regulatory actions has this product attracted"
a single pattern.

### Not new nodes

`openfda drug_labels.csv` carries `pharm_class_epc` and `pharm_class_moa` —
FDA's established pharmacologic class. Feed these into the existing `DrugClass`
and `Mechanism` nodes as a third authority alongside ATC and ChEMBL.

### Deferred

`purplebook_raw_monthly.csv` (report title occupies the header row), and the EMA
herbal / maximum-residue-limit / opinions-outside-EU tables — low volume, no
clear graph role yet.

### A cross-layer note

`drug_labels.csv` (~22,914 rows) holds `indications_and_usage`, `warnings`,
`adverse_reactions`, `contraindications` and `boxed_warning` as **full text**.
That is FDA label prose sitting in a CSV. Chunked into the vector store with
`section` mapped from the column name, it would give retrieval genuine FDA
document coverage — which it currently lacks entirely. It belongs to the vector
store rather than the graph, but it was found during this profiling and should
not be lost.
