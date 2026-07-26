# Knowledge Graph Build Plan

Target schema: 14 entity types, 18 relationship types (`graph/stucture.jpg`).
Built from the 433 CSVs in `s3://moine-data`, 49 sources, 8 categories.
Derived from an actual column-level profile of every CSV (`graph/csv_profile.txt`),
not from source names.

---

## 1. The central finding: there is no shared Drug key

The assumption "resolve drugs on InChIKey/UNII" does not survive contact with the data.

| key | where it actually exists |
|---|---|
| **InChIKey** | pubchem only (`pubchem_properties`, `pubchem_search_results`) |
| **UNII** | gsrs only (`gsrs_substances`, 173k rows) |
| **CAS** | gsrs only |
| **RxCUI** | dailymed `master_mapping`, rxnav, openfda |
| **ChEMBL ID** | chembl only — and chembl holds 55.9 KB of sample queries |
| **ATC** | atcddd (full hierarchy), ema (`atc_code_human`), canada (`ther.TC_ATC`) |
| **UniProt** | uniprot (`Entry`), genenames (`uniprot_ids`) |
| **MeSH** | meshb (`descriptor_ui`), ema (`therapeutic_area_mesh`), openalex (`mesh_terms`) |
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
pubchem (`CID`, `InChIKey`, `InChI`) by name → adds InChIKey where available.
Coverage will be partial; treat InChIKey as an enrichment, not the primary key.

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
ICD-11. EFO↔MeSH has **no crosswalk in the lake** — see gaps.

**2.8 Trials.**
Native registry ID per source; `who_trials` (`TrialID`) is the cross-registry
bridge for de-duplicating the same study across ctgov / eu_ctr / ctri / etc.

**2.9 Companies.**
No registry identifier anywhere. Normalise names (strip Inc/Ltd/GmbH/PLC/SA,
case, punctuation) and cluster. Expect this to be the least accurate node type.

## 3. Relationship → source map (all 18)

| edge | source | notes |
|---|---|---|
| `Company DEVELOPS Drug` | orangebook `Applicant`, ema `marketing_authorisation_developer_applicant_holder`, canada `comp.COMPANY_NAME`+`DRUG_CODE` | mhra has **no** company column |
| `ClinicalTrial SPONSORED_BY Company` | ctgov `lead_sponsor`+`collaborators`, who `Primary_sponsor`, anzctr, ctri, chictr, isrctn, jrct, ctis | broadest edge in the lake |
| `ClinicalTrial STUDIES Disease` | ctgov `conditions`, chictr `hc_freetext`/`hc_code`, ctri, who, anzctr | free text → map to MeSH |
| `ClinicalTrial CONDUCTED_IN Country` | anzctr `RECRUITMENT COUNTRY`, chictr `countries`, ctri `countries_of_recruitment`, isrctn, who, ctgov locations | ISO-3166 normalise |
| `Drug TESTED_IN ClinicalTrial` | ctgov `interventions`, chictr `i_freetext`/`i_code`, anzctr `INTERVENTIONS` | free text → spine |
| `Drug INDICATED_FOR Disease` | **opentargets `known_drugs`** (`indication_id`,`indication_name`), ema `therapeutic_area_mesh` + `therapeutic_indication` | see gaps |
| `Drug TARGETS Target` | **opentargets `known_drugs`** (`target_symbol`), uniprot `drug_target_proteins` | chembl would have been primary |
| `Target ASSOCIATED_WITH Disease` | **opentargets `Disease_Associations/*.csv`** (`disease_id`,`target_id`,`overall_score`) | 6 files, scored |
| `Disease SUBTYPE_OF Disease` | **meshb `tree_numbers`** | MeSH tree encodes the hierarchy directly |
| `Drug HAS_MECHANISM Mechanism` | **opentargets `known_drugs.mechanism`** | |
| `Drug IN_CLASS DrugClass` | atcddd (`atc_code`,`parent_code`), ema `atc_code_human`, canada `ther.TC_ATC` | ATC is the class vocabulary |
| `Drug HAS_MODALITY Modality` | opentargets `known_drugs.type` + `target_tractability.modality`, ema `biosimilar`/`advanced_therapy` flags | 8 values, derived |
| `Drug HAS_ROUTE Route` | canada `route.csv`, atcddd `adm_route`, orangebook `Dosage_Form_Route` | |
| `Drug HAS_IDENTIFIER Identifier` | gsrs (UNII, CAS), rxnav (RxCUI), pubchem (CID, InChIKey), dailymed (setid, NDC), mhra (`pl_number`), canada (DIN), ema (`ema_product_number`) | one node per id value |
| `Drug HAS_APPROVAL Approval` | orangebook (`Appl_No`,`Approval_Date`), ema (`marketing_authorisation_date`), canada (`status`), pmda (`approval_date`,`approval_type`), mhra | Approval = event node |
| `Drug APPROVED_BY Agency` | derived from the source of each approval | 11 agencies |
| `Approval ISSUED_BY Agency` | same | |
| `Drug APPROVED_IN Region` | derived from agency → jurisdiction | |

**opentargets is the linchpin.** `known_drugs.csv` alone supplies `TARGETS`,
`HAS_MECHANISM`, `INDICATED_FOR` and `Modality`; `Disease_Associations/` supplies
`ASSOCIATED_WITH`. It substantially compensates for the missing chembl — but it
is **scoped to 6 therapeutic areas** (Alzheimer, Cancer, Cardiovascular,
Diabetes, Infectious, Respiratory), so those four edges will be dense inside
those areas and sparse outside.

## 4. Gaps and risks

1. **chembl is empty (55.9 KB).** It was the one source with InChIKey ↔ ChEMBL ↔
   UniProt ↔ MeSH in a single schema, plus a `drug_indication` table. Its absence
   is why `TARGETS`/`HAS_MECHANISM`/`INDICATED_FOR` now rest entirely on
   opentargets. The export path exists (`run_all.py`) if it is ever run.
2. **No EFO ↔ MeSH crosswalk in the lake.** opentargets diseases are EFO, meshb
   is MeSH. Joining them needs name matching or an external crosswalk file.
   Until then `Disease` risks splitting into two disconnected populations.
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
