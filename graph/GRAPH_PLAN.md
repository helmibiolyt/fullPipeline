# Knowledge Graph Build Plan

Target schema: 15 entity types, 19 relationship types (see `SCHEMA.md`; `Drug`
is split into `Substance` + `Product`).
Built from the 433 CSVs in `s3://moine-data`, 49 sources, 8 categories.
Derived from an actual column-level profile of every CSV (`graph/csv_profile.txt`),
not from source names.

---

## 1. The central finding: there is no shared Drug key

The assumption "resolve drugs on InChIKey/UNII" does not survive contact with the data.

| key | where it actually exists |
|---|---|
| **InChIKey** | **chembl `chembl_structures.csv` (~2.4M molecules)**, pubchem |
| **UNII** | gsrs only (`gsrs_substances`, 173k rows) |
| **CAS** | gsrs only |
| **RxCUI** | dailymed `master_mapping`, rxnav, openfda |
| **ChEMBL ID** | chembl `chembl_molecules.csv` (~2.4M) — now populated |
| **ATC** | atcddd (full hierarchy), ema (`atc_code_human`), canada (`ther.TC_ATC`) |
| **UniProt** | uniprot (`Entry`), genenames (`uniprot_ids`) |
| **MeSH** | meshb (`descriptor_ui`), **chembl `chembl_indications.csv` (with EFO on the same row)**, ema, openalex |
| **EFO** | opentargets (`disease_id`) |

None of the regulatory sources — ema, mhra, pmda, canada, orangebook — carry a
chemical identifier at all. They identify products by **name** plus a
registry-local code (`pl_number`, `ema_product_number`, `DRUG_CODE`, `Appl_No`,
`id`). So Drug resolution must be **name-anchored**, with UNII/RxCUI/ATC as
supporting anchors. This is the hardest part of the build and it drives the
sequencing below.

## 2. Entity resolution spine

Build in this order; each step depends on the previous.

**2.1 Substance dictionary (the spine).**
`gsrs_substances` (173,081 rows: `unii`, `preferred_name`, `cas_number`,
`synonyms`, `substance_class`) becomes the canonical substance table. Normalise
name + every synonym into a lookup → UNII. This is the only source that offers
a substance authority with synonym coverage at scale.

**2.2 Attach ATC.**
`atc_ddd_full` / `atc_substances` (`atc_code`, `name`, `level`, `parent_code`,
`adm_route`, `ddd`) → match substance names to the spine. Gives `DrugClass`
(the ATC hierarchy via `parent_code`) and a first `Route` signal (`adm_route`).

**2.3 Attach RxNorm.**
`rxnav/rxnorm_drugs` and `dailymed_master_mapping` (`rxcui`, `name`,
`ingredient_rxcui`, `ingredient_name`, `setid`) → bridges US products to
substances and supplies `RxCUI` identifiers.

**2.4 Attach structures.**
chembl `chembl_structures.csv` gives `standard_inchi_key` + SMILES for ~2.4M
molecules keyed on `molregno`, joined to names via `chembl_molecules.csv`;
pubchem adds `CID`. **`chembl_molecule_hierarchy.csv` maps each salt/ester form
to its parent molecule as fact**, which replaces guessing at salt stripping and
is the most reliable merge signal available.

**2.5 Products → substances.**
Each regulatory product row resolves to one or more spine substances by
normalised name:
- ema `active_substance` / `international_non_proprietary_name_inn_common_name`
- mhra `substance_name` (+ `product_name`)
- canada `ingred.INGREDIENT_NAME` (+ `drug.BRAND_NAME`)
- orangebook `Ingredient` (+ `Trade_Name`)
- pmda `generic_name` (+ `brand_name`)

**2.6 Targets.**
`uniprot.Entry` is canonical. `genenames` (`hgnc_id`, `symbol`, `uniprot_ids`)
crosswalks symbol ↔ UniProt. opentargets uses `target_symbol` / `target_id`
(Ensembl) — join through genenames.

**2.7 Diseases.**
`mesh_descriptors` (`descriptor_ui`, `name`, `tree_numbers`, `synonyms`) is
canonical. opentargets supplies EFO (`disease_id`, `disease_name`); icd supplies
ICD-11. **`chembl_indications.csv` carries `mesh_id` and `efo_id` on the same
row**, so it is the EFO↔MeSH crosswalk — load it before the disease-scoped
opentargets files so their EFO ids attach to existing MeSH nodes.

**2.8 Trials.**
Native registry ID per source; `who_trials` (`TrialID`) is the cross-registry
bridge for de-duplicating the same study across ctgov / eu_ctr / ctri / etc.

**2.9 Companies.**
No registry identifier anywhere. Normalise names (strip Inc/Ltd/GmbH/PLC/SA,
case, punctuation) and cluster. Expect this to be the least accurate node type.

## 3. Relationship → source map (all 19)

| edge | source | notes |
|---|---|---|
| `Company DEVELOPS Drug` | orangebook `Applicant`, ema `marketing_authorisation_developer_applicant_holder`, canada `comp.COMPANY_NAME`+`DRUG_CODE` | mhra has **no** company column |
| `ClinicalTrial SPONSORED_BY Company` | ctgov `lead_sponsor`+`collaborators`, who `Primary_sponsor`, anzctr, ctri, chictr, isrctn, jrct, ctis | broadest edge in the lake |
| `ClinicalTrial STUDIES Disease` | ctgov `conditions`, chictr `hc_freetext`/`hc_code`, ctri, who, anzctr | free text → map to MeSH |
| `ClinicalTrial CONDUCTED_IN Country` | anzctr `RECRUITMENT COUNTRY`, chictr `countries`, ctri `countries_of_recruitment`, isrctn, who, ctgov locations | ISO-3166 normalise |
| `Drug TESTED_IN ClinicalTrial` | ctgov `interventions`, chictr `i_freetext`/`i_code`, anzctr `INTERVENTIONS` | free text → spine |
| `Substance INDICATED_FOR Disease` | **chembl `chembl_indications.csv`** (`mesh_id`+`efo_id`), opentargets `known_drugs`, ema | structured, no text extraction |
| `Substance TARGETS Target` | **chembl `chembl_mechanisms.csv`** (`molregno`→`tid`), opentargets, uniprot | chembl is primary, pan-therapeutic |
| `Target ASSOCIATED_WITH Disease` | **opentargets `Disease_Associations/*.csv`** (`disease_id`,`target_id`,`overall_score`) | 6 files, scored |
| `Disease SUBTYPE_OF Disease` | **meshb `tree_numbers`** | MeSH tree encodes the hierarchy directly |
| `Substance HAS_MECHANISM Mechanism` | **chembl `chembl_mechanisms.csv`** (`mechanism_of_action`, `action_type`), opentargets | |
| `Drug IN_CLASS DrugClass` | atcddd (`atc_code`,`parent_code`), ema `atc_code_human`, canada `ther.TC_ATC` | ATC is the class vocabulary |
| `Substance HAS_MODALITY Modality` | **chembl `chembl_usan_stems.csv`** + `molecule_type`, opentargets | 8 values, derived |
| `Drug HAS_ROUTE Route` | canada `route.csv`, atcddd `adm_route`, orangebook `Dosage_Form_Route` | |
| `Drug HAS_IDENTIFIER Identifier` | gsrs (UNII, CAS), rxnav (RxCUI), pubchem (CID, InChIKey), dailymed (setid, NDC), mhra (`pl_number`), canada (DIN), ema (`ema_product_number`) | one node per id value |
| `Drug HAS_APPROVAL Approval` | orangebook (`Appl_No`,`Approval_Date`), ema (`marketing_authorisation_date`), canada (`status`), pmda (`approval_date`,`approval_type`), mhra | Approval = event node |
| `Drug APPROVED_BY Agency` | derived from the source of each approval | 11 agencies |
| `Approval ISSUED_BY Agency` | same | |
| `Drug APPROVED_IN Region` | derived from agency → jurisdiction | |

**chembl is the pharmacology backbone** (as of 2026-07-26): `chembl_mechanisms`
supplies `TARGETS` + `HAS_MECHANISM`, `chembl_indications` supplies
`INDICATED_FOR` with MeSH ids, `chembl_usan_stems` supplies `Modality` — all
pan-therapeutic.

**opentargets enriches rather than carries.** `Disease_Associations/*.csv` is
the only source for `ASSOCIATED_WITH` and adds scored evidence, but its files
are **scoped to 6 therapeutic areas** (Alzheimer, Cancer, Cardiovascular,
Diabetes, Infectious, Respiratory), so that one edge stays dense inside those
areas and sparse outside.

## 4. Gaps and risks

1. ~~chembl is empty~~ **RESOLVED 2026-07-26.** chembl now holds 14 CSVs /
   493.8 MB: InChIKey ↔ ChEMBL ↔ UniProt ↔ MeSH in one schema, plus
   `drug_mechanism` and `drug_indication`. `TARGETS`, `HAS_MECHANISM`,
   `INDICATED_FOR` and `HAS_MODALITY` are now pan-therapeutic rather than
   limited to opentargets' 6 areas.
2. ~~No EFO ↔ MeSH crosswalk~~ **RESOLVED.** `chembl_indications.csv` carries
   `mesh_id` and `efo_id` on the same row, joining opentargets' EFO diseases to
   meshb's MeSH descriptors directly.
3. **Company resolution has no identifier.** Name clustering only.
4. **Free-text edges.** `STUDIES`, `TESTED_IN`, `INDICATED_FOR` come largely from
   prose (`conditions`, `interventions`, `therapeutic_indication`). Deterministic
   dictionary matching against the spine + MeSH synonyms — no LLM.
5. **purplebook is malformed.** `purplebook_raw_monthly.csv`'s header row is a
   report title, not column names. Needs a parser fix before use.
6. **opentargets/uniprot are disease-scoped**, not exhaustive.
7. **Cross-registry trial duplication.** The same study appears in several
   registries; `who_trials` must drive de-duplication or trial counts inflate.

## 5. Build order

1. **Vocabularies first** (cheap, no resolution): Country (ISO), Region, Agency
   (11), Route, DrugClass (ATC tree), Modality, Mechanism.
2. **Disease** from meshb + `SUBTYPE_OF` from `tree_numbers`; then ICD and EFO
   attach.
3. **Target** from uniprot + genenames crosswalk.
4. **Drug spine** from gsrs, enriched with ATC → RxNorm → pubchem.
5. **ClinicalTrial** per registry, de-duplicated via who.
6. **Company** by name clustering.
7. **Approval / Identifier** event and value nodes from the regulatory sources.
8. **Edges**, cheapest and most reliable first: structured (`ASSOCIATED_WITH`,
   `IN_CLASS`, `HAS_ROUTE`, `HAS_IDENTIFIER`, approval chain) before free-text
   (`STUDIES`, `TESTED_IN`, `INDICATED_FOR`).

All loads idempotent (`MERGE`, never `CREATE`) and stamped with
`source` / `run_id` / `committed_at`, so any source can be reloaded
independently as the lake improves.

## 6. Vector store coupling

Chunk metadata must carry the same canonical IDs the graph mints (UNII for
substances, MeSH for conditions, UniProt for targets, native ID for trials) so a
graph fact can cite a document chunk. Entity resolution must therefore be a
**shared module** used by both loaders — not reimplemented — and the graph must
be built before the 57,278-document corpus is embedded, or the metadata will be
wrong and the embedding has to be redone.
