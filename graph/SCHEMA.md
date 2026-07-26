# Graph schema — final specification

15 node types · 19 relationship types.
Derived from a column-level profile of all 433 CSVs in `s3://moine-data`.

Change from `stucture.jpg`: the original `Drug` node is split into
**`Substance`** (the molecule / active ingredient) and **`Product`** (the
marketed, approved item). Everything else is unchanged. Rationale in §4.

---

## 1. Nodes

| # | label | key | key properties | built from |
|---|---|---|---|---|
| 1 | **Substance** | `UNII:<unii>` or `NAME:<norm_name>` | `name`, `norm_name`, `substance_class`, `status` | gsrs `gsrs_substances` (173k), atcddd `atc_substances`, pubchem, rxnav |
| 2 | **Product** | `<AGENCY>:<local_id>` e.g. `MHRA:PL12345` | `name`, `brand_name`, `agency`, `status`, `form`, `strength` | ema `ema_medicines`, mhra `raw_metadata`, canada `drug.csv`, orangebook `orange_book_unified`, pmda `pmda_metadata`, dailymed `dailymed_catalog` |
| 3 | **ClinicalTrial** | `<REGISTRY>:<id>` e.g. `NCT:NCT01234567` | `title`, `status`, `phase`, `study_type`, `enrollment`, `start_date` | ctgov, who, eu_ctr, ctri, chictr, anzctr, isrctn, jrct, ctis |
| 4 | **Company** | `norm(name)` | `name`, `raw_names[]` | ctgov `lead_sponsor`, ema MAH, orangebook `Applicant`, canada `comp.COMPANY_NAME`, all registries |
| 5 | **Disease** | `MESH:<descriptor_ui>` (also `EFO:`, `ICD:`) | `name`, `synonyms[]`, `tree_numbers[]` | meshb `mesh_descriptors`, icd `icd11_codes`, opentargets `disease_id` |
| 6 | **Target** | `<uniprot_accession>` | `symbol`, `name`, `organism`, `ec_number` | uniprot `Entry`, genenames `uniprot_ids`/`symbol` |
| 7 | **Country** | `<iso2>` | `name` | ISO-3166 vocab; values from trial registries |
| 8 | **Region** | `<name>` | `name` | derived from agency jurisdiction |
| 9 | **RegulatoryAgency** | `<code>` | `name`, `country`, `region` | fixed vocab (11): FDA, EMA, MHRA, PMDA, HC, SFDA, NHRA, DHA, DOH, MOH-OM, MOPH-QA |
| 10 | **Approval** | `<AGENCY>:<appl_no>:<date>` | `date`, `type`, `status` | orangebook, ema, canada `status.csv`, pmda, mhra |
| 11 | **Identifier** | `(scheme, value)` node key | `scheme`, `value` | see §3 |
| 12 | **DrugClass** | `<atc_code>` | `name`, `level` | atcddd `atc_ddd_full` / `atc_classes` |
| 13 | **Mechanism** | `<name>` | `name` | opentargets `known_drugs.mechanism` |
| 14 | **Modality** | `<name>` | `name` | opentargets `known_drugs.type`, `target_tractability.modality` |
| 15 | **Route** | `<name>` | `name` | canada `route.csv`, atcddd `adm_route`, orangebook `Dosage_Form_Route` |

## 2. Relationships

| # | pattern | source | method |
|---|---|---|---|
| 1 | `(Product)-[:CONTAINS]->(Substance)` | canada `ingred.csv`, orangebook `Ingredient`, ema `active_substance`, mhra `substance_name`, pmda `generic_name` | dictionary |
| 2 | `(Company)-[:DEVELOPS]->(Product)` | orangebook `Applicant`, ema MAH, canada `comp.csv` | structured |
| 3 | `(ClinicalTrial)-[:SPONSORED_BY]->(Company)` | ctgov `lead_sponsor`+`collaborators`, who, anzctr, ctri, chictr, isrctn, jrct, ctis | structured |
| 4 | `(ClinicalTrial)-[:STUDIES]->(Disease)` | ctgov `conditions`, chictr `hc_freetext`, ctri, who | dictionary |
| 5 | `(ClinicalTrial)-[:CONDUCTED_IN]->(Country)` | anzctr, chictr `countries`, ctri, isrctn, who, ctgov | structured |
| 6 | `(Substance)-[:TESTED_IN]->(ClinicalTrial)` | ctgov `interventions`, chictr `i_freetext`, anzctr | dictionary |
| 7 | `(Substance)-[:INDICATED_FOR]->(Disease)` | opentargets `known_drugs.indication_id/name`, ema `therapeutic_area_mesh` | structured + dictionary |
| 8 | `(Substance)-[:TARGETS]->(Target)` | opentargets `known_drugs.target_symbol`, uniprot `drug_target_proteins` | structured |
| 9 | `(Target)-[:ASSOCIATED_WITH]->(Disease)` | opentargets `Disease_Associations/*.csv` (+`overall_score`) | structured |
| 10 | `(Disease)-[:SUBTYPE_OF]->(Disease)` | meshb `tree_numbers` | computed |
| 11 | `(Substance)-[:HAS_MECHANISM]->(Mechanism)` | opentargets `known_drugs.mechanism` | structured |
| 12 | `(Substance)-[:IN_CLASS]->(DrugClass)` | atcddd, ema `atc_code_human`, canada `ther.TC_ATC` | structured |
| 13 | `(Substance)-[:HAS_MODALITY]->(Modality)` | opentargets `known_drugs.type` | structured |
| 14 | `(Product)-[:HAS_ROUTE]->(Route)` | canada `route.csv`, orangebook `Dosage_Form_Route` | structured |
| 15 | `(Substance\|Product)-[:HAS_IDENTIFIER]->(Identifier)` | see §3 | structured |
| 16 | `(Product)-[:HAS_APPROVAL]->(Approval)` | orangebook, ema, canada, pmda, mhra | structured |
| 17 | `(Product)-[:APPROVED_BY]->(RegulatoryAgency)` | derived from source | derived |
| 18 | `(Approval)-[:ISSUED_BY]->(RegulatoryAgency)` | derived from source | derived |
| 19 | `(Product)-[:APPROVED_IN]->(Region)` | derived from agency | derived |

Utility edge (not part of the 19): `(ClinicalTrial)-[:SAME_STUDY_AS]-(ClinicalTrial)`
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
| `INCHIKEY` | Substance | pubchem `InChIKey` |
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

- `TARGETS`, `HAS_MECHANISM`, `INDICATED_FOR`, `HAS_MODALITY` come from
  opentargets, which covers 6 therapeutic areas (Alzheimer, Cancer,
  Cardiovascular, Diabetes, Infectious, Respiratory). Dense inside those,
  sparse outside. chembl would have been the general source; it holds 55.9 KB.
- `Company` is name-clustered — no registry identifier exists in any source.
- `Disease` may split into MeSH and EFO populations; no crosswalk in the lake.
- `purplebook_raw_monthly.csv` has a report title as its header row; excluded
  until its parser is fixed.
