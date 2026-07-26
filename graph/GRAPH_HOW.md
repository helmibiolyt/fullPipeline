# How to build the graph — mechanics

Companion to `GRAPH_PLAN.md`. That document says *what connects to what*; this
one says *how to actually do it*.

Different sources use different drug IDs. That is not a problem to solve — it is
what the `Identifier` node type is for. **One `Substance` node per molecule, one
`Product` node per marketed item, and every source's ID hangs off them as an
`Identifier`.** Nothing has to agree on a single key; things only have to agree
on *which substance they mean*.

`Product` never needs cross-source resolution at all — its key is the agency's
own id (`MHRA:PL12345`, `CA:DIN02241497`). Only `Substance` is resolved.

---

## 0. Layout

```
graph/
  schema.cypher          constraints + indexes, run once
  resolve.py             normalisation + lookup tables (shared with vector store)
  load.py                driver: order, batching, provenance
  loaders/
    vocab.py             Country, Region, Agency, Route, DrugClass, Modality
    disease.py           meshb, icd, opentargets EFO
    target.py            uniprot, genenames
    substance.py         gsrs spine, chembl, atc, rxnav, pubchem
    product.py           ema, mhra, canada, orangebook, pmda, dailymed
    trial.py             ctgov, who, eu_ctr, ctri, chictr, anzctr, isrctn, jrct, ctis
    company.py           name clustering
    approval.py          orangebook, ema, canada, pmda, mhra
    edges.py             the 19 relationships
```

## 1. Schema first — constraints before any load

Uniqueness constraints make `MERGE` fast and idempotent. Without them every
`MERGE` is a full scan and the load will crawl.

```cypher
CREATE CONSTRAINT subst_key     IF NOT EXISTS FOR (s:Substance)       REQUIRE s.key         IS UNIQUE;
CREATE CONSTRAINT prod_key      IF NOT EXISTS FOR (p:Product)         REQUIRE p.key         IS UNIQUE;
CREATE CONSTRAINT ident_key     IF NOT EXISTS FOR (i:Identifier)      REQUIRE (i.scheme, i.value) IS NODE KEY;
CREATE CONSTRAINT target_key    IF NOT EXISTS FOR (t:Target)          REQUIRE t.uniprot     IS UNIQUE;
CREATE CONSTRAINT disease_key   IF NOT EXISTS FOR (d:Disease)         REQUIRE d.key         IS UNIQUE;
CREATE CONSTRAINT trial_key     IF NOT EXISTS FOR (t:ClinicalTrial)   REQUIRE t.key         IS UNIQUE;
CREATE CONSTRAINT company_key   IF NOT EXISTS FOR (c:Company)         REQUIRE c.key         IS UNIQUE;
CREATE CONSTRAINT approval_key  IF NOT EXISTS FOR (a:Approval)        REQUIRE a.key         IS UNIQUE;
CREATE CONSTRAINT agency_key    IF NOT EXISTS FOR (a:RegulatoryAgency)REQUIRE a.code        IS UNIQUE;
CREATE CONSTRAINT country_key   IF NOT EXISTS FOR (c:Country)         REQUIRE c.iso2        IS UNIQUE;
CREATE CONSTRAINT class_key     IF NOT EXISTS FOR (c:DrugClass)       REQUIRE c.atc_code    IS UNIQUE;
CREATE CONSTRAINT route_key     IF NOT EXISTS FOR (r:Route)           REQUIRE r.name        IS UNIQUE;
CREATE CONSTRAINT modality_key  IF NOT EXISTS FOR (m:Modality)        REQUIRE m.name        IS UNIQUE;
CREATE CONSTRAINT mech_key      IF NOT EXISTS FOR (m:Mechanism)       REQUIRE m.name        IS UNIQUE;
CREATE CONSTRAINT region_key    IF NOT EXISTS FOR (r:Region)          REQUIRE r.name        IS UNIQUE;
```

## 2. Name normalisation — the one function everything depends on

```python
SALTS = {"hydrochloride","hcl","sodium","potassium","calcium","magnesium",
         "sulfate","sulphate","mesylate","maleate","tartrate","citrate",
         "acetate","phosphate","succinate","fumarate","besylate","tosylate",
         "bromide","chloride","nitrate","oxalate","dihydrate","monohydrate",
         "anhydrous","trihydrate"}
STEREO = re.compile(r"^\(?[rsd|l|dl|rac]{1,3}\)?[- ]", re.I)

def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii","ignore").decode()
    s = s.lower().strip()
    s = re.sub(r"\[.*?\]|\(.*?\)", " ", s)      # drop bracketed qualifiers
    s = STEREO.sub("", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    toks = [t for t in s.split() if t and t not in SALTS]
    return " ".join(toks)
```

Rules: apply to **every** name before comparing, store `norm_name` on the node
for debugging, never throw the raw name away (keep it as `name`).

## 3. Building the resolver

One pass over gsrs, held in memory (~173k substances, a few hundred MB):

```python
# unii_by_name: norm_name -> unii
for row in csv(gsrs_substances):
    unii, pref = row["unii"], row["preferred_name"]
    unii_by_name[norm(pref)] = unii
    for syn in split_synonyms(row["synonyms"]):
        unii_by_name.setdefault(norm(syn), unii)   # first wins, do not overwrite
```

Then resolution is one function used by every loader **and by the vector store**:

```python
def drug_key(name: str) -> str:
    n = norm(name)
    u = unii_by_name.get(n)
    return f"UNII:{u}" if u else f"NAME:{n}"     # provisional key on miss
```

Provisional `NAME:` keys are normal and expected — a later source that carries
both the name and a UNII will let them be merged (§7). **Never drop a row
because it did not resolve.**

**Salt forms: use chembl, not string stripping.** The `SALTS` list above is a
fallback. `chembl_molecule_hierarchy.csv` states `molregno → parent_molregno`
directly, so atorvastatin calcium is linked to atorvastatin as fact rather than
by guessing which trailing tokens are salts. Load it and prefer it; fall back to
the token list only for substances chembl does not cover.

## 4. Batched MERGE — the loading pattern

Every loader uses the same shape. Never one query per row.

```python
BATCH = 5_000

def run_batch(tx, cypher, rows):
    tx.run(cypher, rows=rows)

CY_SUBSTANCE = """
UNWIND $rows AS r
MERGE (s:Substance {key: r.key})
  ON CREATE SET s.name = r.name, s.norm_name = r.norm_name, s.created_at = r.ts
SET s.source = r.source, s.run_id = r.run_id, s.committed_at = r.ts
"""
```

Read the CSV from S3 **streamed**, never fully into memory — several are
multi-GB (ctgov 2.7 GB, who 4.6 GB, openalex 2.2 GB) and the box has 7 GB RAM:

```python
body = s3.get_object(Bucket=B, Key=k)["Body"]
reader = csv.DictReader(io.TextIOWrapper(body, encoding="utf-8", errors="replace"))
for chunk in chunked(reader, BATCH):
    session.execute_write(run_batch, CY_SUBSTANCE, [to_row(r) for r in chunk])
```

## 5. Identifiers — how multi-source IDs are handled

This is the answer to "drug IDs change per source". Mint one `Identifier` node
per (scheme, value) and attach it:

```cypher
UNWIND $rows AS r
MATCH (n) WHERE n.key = r.node_key AND (n:Substance OR n:Product)
MERGE (i:Identifier {scheme: r.scheme, value: r.value})
MERGE (n)-[:HAS_IDENTIFIER]->(i)
```

| scheme | source column |
|---|---|
| `UNII` | gsrs `unii` |
| `CAS` | gsrs `cas_number` |
| `RXCUI` | rxnav / dailymed `rxcui` |
| `INCHIKEY` | chembl `chembl_structures.csv`, pubchem |
| `CHEMBL_ID` | chembl `chembl_molecules.csv` |
| `PUBCHEM_CID` | pubchem `CID` |
| `SPL_SETID` | dailymed `setid` |
| `NDC` | dailymed `ndc_list` (explode the list) |
| `MHRA_PL` | mhra `pl_number` |
| `EMA_PRODUCT` | ema `ema_product_number` |
| `CA_DIN` | canada `DRUG_IDENTIFICATION_NUMBER` |
| `FDA_APPL_NO` | orangebook `Appl_No` |
| `ATC` | atcddd / ema `atc_code_human` |

An Identifier is shared: two `Substance` nodes pointing at the same `RXCUI` is
the signal that they are the same substance and should be merged (§7). The
first six schemes attach to `Substance`, the rest to `Product`.

## 6. Loading each entity — concretely

**Vocabularies** (no resolution, load first):
```cypher
UNWIND $rows AS r MERGE (c:Country {iso2: r.iso2}) SET c.name = r.name
UNWIND $rows AS r MERGE (a:RegulatoryAgency {code: r.code}) SET a.name = r.name, a.region = r.region
UNWIND $rows AS r MERGE (c:DrugClass {atc_code: r.atc_code}) SET c.name = r.name, c.level = r.level
```
`DrugClass` hierarchy from `atc_ddd_full.parent_code`:
```cypher
UNWIND $rows AS r
MATCH (c:DrugClass {atc_code: r.atc_code}), (p:DrugClass {atc_code: r.parent_code})
MERGE (c)-[:IN_CLASS]->(p)
```

**Disease** — key is `MESH:<descriptor_ui>`; hierarchy straight out of the tree
numbers (a descriptor whose tree number is the parent prefix is the parent):
```cypher
UNWIND $rows AS r
MATCH (c:Disease {key: r.child}), (p:Disease {key: r.parent})
MERGE (c)-[:SUBTYPE_OF]->(p)
```
Compute pairs in Python: for tree number `C04.588.322`, the parent is
`C04.588` — look up which descriptor owns it.

**Target** — key is UniProt `Entry`; `genenames.uniprot_ids` gives symbol ↔
accession so opentargets' `target_symbol` can join.

**ClinicalTrial** — key is `<REGISTRY>:<id>`, e.g. `NCT:NCT01234567`. Then use
`who_trials` to link duplicates:
```cypher
UNWIND $rows AS r
MATCH (a:ClinicalTrial {key: r.a}), (b:ClinicalTrial {key: r.b})
MERGE (a)-[:SAME_STUDY_AS]-(b)
```
(keep them as separate nodes joined by an edge — safer than merging, and
reversible.)

**Company** — key is `norm(name)` after stripping legal suffixes
(`inc|ltd|llc|gmbh|plc|sa|nv|bv|ag|co|corp|corporation|limited|pharmaceuticals?`).

## 7. Merging provisional drugs (the second pass)

After all sources load, any two `Substance` nodes sharing a strong Identifier
are the same substance:

```cypher
MATCH (a:Substance)-[:HAS_IDENTIFIER]->(i:Identifier)<-[:HAS_IDENTIFIER]-(b:Substance)
WHERE i.scheme IN ['UNII','INCHIKEY','RXCUI','CHEMBL_ID']
  AND a.key STARTS WITH 'NAME:' AND a <> b
RETURN a.key, b.key, i.scheme, i.value
```

Resolve by rewriting relationships from the `NAME:` node onto the `UNII:` node,
then deleting the provisional. Run it as an explicit, reviewable step — not
silently during load — so a bad merge can be undone.

## 8. Free-text edges — deterministic, no LLM

`conditions`, `interventions`, `therapeutic_indication` are prose. Match against
dictionaries built in §3:

```python
def match_disease(text):                      # ctgov `conditions`
    out = set()
    for part in re.split(r"[;|,]", text or ""):
        k = mesh_by_name.get(norm(part))      # MeSH name + synonyms lookup
        if k: out.add(k)
    return out
```
Same shape for `TESTED_IN` (intervention text → `drug_key`). Longest-match on
multi-word terms, and record `match_method` on the edge (`exact` / `synonym`)
so precision can be measured later.

## 9. Provenance and idempotency

Every node and edge carries `source`, `run_id`, `committed_at` (from the
source's `_LATEST.json`). All writes are `MERGE`. Consequences:

- rerunning a source converges instead of duplicating
- a source can be reloaded alone when the lake improves (chembl, later)
- `MATCH (n) WHERE n.source = 'x' DETACH DELETE n` cleanly removes one source

## 10. Execution order

```
1  schema.cypher                       constraints/indexes
2  vocab                               Country, Region, Agency, Route, DrugClass, Modality
3  disease                             meshb -> SUBTYPE_OF -> icd, EFO
4  target                              uniprot + genenames
5  substance spine                     gsrs -> chembl (+ molecule_hierarchy) -> atc -> rxnav -> pubchem
5b product                             ema, mhra, canada, orangebook, pmda -> CONTAINS
6  identifiers                         all schemes
7  trials                              registries -> who de-dup
8  companies                           name clustering
9  approvals                           orangebook, ema, canada, pmda, mhra
10 structured edges                    ASSOCIATED_WITH, IN_CLASS, HAS_ROUTE, TARGETS,
                                       HAS_MECHANISM, approval chain
11 free-text edges                     STUDIES, TESTED_IN, INDICATED_FOR
12 merge pass                          provisional NAME: -> UNII:, via shared Identifiers
                                       and chembl_molecule_hierarchy parents
```

Steps 2-9 are independent per source and can be re-run individually. Step 12 is
the only one that mutates existing structure, so it runs last and alone.

## 11. Sizing note

On a 2-CPU / 7 GB box: stream CSVs, `BATCH = 5_000`, one write session at a
time. The large trial CSVs (who 4.6 GB, ctgov 2.7 GB) dominate wall-clock;
everything else is minutes. Neo4j's page cache wants ~2 GB — leave headroom.
