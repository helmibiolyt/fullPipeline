# Querying the graph

For the research agent. The companion to
[`vector_store/AGENT_QUERY_GUIDE.md`](../vector_store/AGENT_QUERY_GUIDE.md) —
that one covers documents, this one covers the graph.

Everything here was measured against the live database, including the things
that do not work.

---

## The one thing to internalise

**The graph answers questions about relationships between things. It is not a
search engine.** Almost every useful query has the same shape:

```
1. find a starting node    (this is the hard part)
2. traverse from it        (this is fast and reliable)
```

Step 2 is 13–38 ms. Step 1 is where queries fail, so most of this guide is
about step 1.

---

## Step 1: finding your starting node

Three mechanisms, in order of how much you should trust them.

### a. Exact identifier — use it whenever you have one

```cypher
MATCH (i:Identifier {value: 'NCT01045135'})<-[:HAS_IDENTIFIER]-(t)  RETURN t
MATCH (s:Substance {key: 'UNII:36TN91XZ0V'})                        RETURN s
```

`Identifier.value` is indexed. Schemes available: `UNII`, `CAS`, `CHEMBL_ID`,
`INCHIKEY`, `NCT`, `MESH`, `ICD11`, `UNIPROT`, `RXCUI`, `SPL_SETID`, `ATC`,
`CLINVAR`, `PMID`, `DOI`, `CA_DIN`, `MHRA_PL`, `FDA_APPL_NO`, `NDC`,
`EMA_PRODUCT`.

### b. Full-text — for names and near-names

```cypher
CALL db.index.fulltext.queryNodes('entity_names', 'atorvastatin')
YIELD node, score
WHERE node:Substance
RETURN node.name, score LIMIT 5
```

Three indexes, because the labels do not share a property:

| index | covers | on |
|---|---|---|
| `entity_names` | Substance, Product, Disease, Target, Company, Mechanism, DrugClass, OrganClass, Variant | `name`, `synonyms` |
| `document_titles` | Publication, ClinicalTrial | `title` |
| `reaction_terms` | AdverseEvent | `term` |

**Always filter by label.** Without `WHERE node:Disease`, a query for
`"lung cancer"` returns companies called "Lung Cancer Mutation Consortium"
ahead of the disease. Measured, not hypothetical.

Fuzzy matching with `~` handles typos: `'pembrolizimab~'` finds
`PEMBROLIZUMAB`.

### c. Embeddings — for abbreviations and paraphrase

Full-text **cannot** bridge these, because there is no shared text:

| query | full-text | embedding |
|---|---|---|
| `NSCLC` | nothing | 0.832 → Non-small cell lung cancer |
| `statins` | nothing | 0.737 → HMG-CoA reductase inhibitor |
| `COPD` | nothing | 0.803 → Chronic obstructive pulmonary disease |
| `heart problems` | nothing | 0.873 → heart disorder |

40,432 nodes are embedded with **SapBERT** — Disease, DrugClass, AdverseEvent,
Mechanism. Those are the labels where a question's wording is far from the
stored name. Vectors live beside the build:

```bash
python graph/embed_entities.py --dir ~/graph-runs/<ts> --query "NSCLC"
```

**Reject matches below 0.60.** Correct answers measured 0.74–0.87 and the best
wrong answer sat well under 0.6, so the gap is real — use it rather than always
taking the top hit.

### Which to reach for

```
have an identifier?          -> (a), always
a name you believe is exact? -> (b), with a label filter
an acronym or a phrasing?    -> (c), then verify with (b)
```

---

## Step 2: traversal patterns that work

### A drug, end to end

```cypher
MATCH (s:Substance {norm_name: 'atorvastatin'})
OPTIONAL MATCH (s)-[:IN_CLASS]->(c:DrugClass)
OPTIONAL MATCH (s)-[:TARGETS]->(t:Target)
OPTIONAL MATCH (s)-[:HAS_MECHANISM]->(m:Mechanism)
OPTIONAL MATCH (p:Product)-[:CONTAINS]->(s)
RETURN s.name, c.atc_code, collect(DISTINCT t.symbol),
       m.name, count(DISTINCT p) AS products
```

### Where is it approved, and by whom

```cypher
MATCH (p:Product)-[:CONTAINS]->(s:Substance {norm_name: 'pembrolizumab'})
MATCH (p)-[:APPROVED_BY]->(a:RegulatoryAgency)
RETURN a.code, a.region, count(p) ORDER BY count(p) DESC
```

### Trials in a region — traverse, do not list countries

```cypher
MATCH (t:ClinicalTrial)-[:CONDUCTED_IN]->(:Country)-[:IN_REGION]->(r:Region {name:'MENA/GCC'})
RETURN count(DISTINCT t)
```

⚠️ **`MENA/GCC` is the whole region** — Israel, Iran, Egypt, North Africa —
89,328 trials. The six Gulf states alone are 3,793. If you mean the Gulf, name
the countries.

### Safety, grouped

```cypher
MATCH (s:Substance {norm_name:'pembrolizumab'})-[e:HAS_ADVERSE_EVENT]->(a:AdverseEvent)
MATCH (a)-[:IN_ORGAN_CLASS]->(o:OrganClass)
RETURN o.name, sum(e.report_count) AS reports
ORDER BY reports DESC LIMIT 10
```

`OrganClass` is what makes "any cardiac event" one hop instead of enumerating
every cardiac term.

### Mutation → protein → drug

```cypher
MATCH (v:Variant)-[:VARIANT_IN]->(t:Target)<-[:TARGETS]-(s:Substance)
MATCH (v)-[:IMPLICATED_IN]->(d:Disease)
WHERE v.clinical_significance CONTAINS 'Pathogenic'
RETURN d.name, t.symbol, collect(DISTINCT s.name)[..5] LIMIT 20
```

---

## What will mislead you

### `match_method` — check it before trusting an edge

Every edge records how it was established.

| value | meaning | trust |
|---|---|---|
| `structured` | the source stated it | high |
| `unii`, `salt`, `stereo` | resolved via identifier tiers | high |
| `symbol` | matched on a gene symbol | good |
| `name` | the condition as written IS a MeSH heading or entry term | **treat as a hint** |
| `name_variant` | a rewriting reached MeSH - a stage qualifier stripped, a plural, "cancer" for "neoplasms" | weaker |
| `vocab_alias` | NCIt or CDISC lists the phrase as a synonym of a concept that reaches MeSH | weaker |
| `name_squashed` | same letters, separators in different places — `Sars-CoV2` for `SARS-CoV 2` | weaker |
| `icd_name` | no MeSH form matched, an ICD title did | **weakest** |
| `icd_code` | nothing matched the words, so the ICD-10 **code** the registry typed was used - `C692- Malignant neoplasm of retina` | good |
| `ncit_oncology` | on `TESTED_IN` — matched via an NCIt antineoplastic synonym | good |
| `inn_usan` | on `TESTED_IN` — the international spelling of a US-named drug | good |
| `provisional` | the name never resolved | **weak** |

`CONDUCTED_IN` and almost every `STUDIES` edge are **name-matched, not
structured** - the registry wrote prose and the loader recognised it. They are
useful for aggregate questions and should not be cited as fact about one
specific trial.

The one exception is `icd_code`. There the registry typed an ICD-10 code
itself, so the diagnosis is stated in a controlled vocabulary rather than
inferred from wording - which is why it outranks `icd_name` despite both
coming from ICD. It is a last resort only in ORDER, not in trust: it fires
when the words failed, and the words failing says nothing about the code.

The tiers above are how a `STUDIES` edge was made. If an answer rests on a
handful of trials, check the tier; if it rests on thousands, the mix matters
less. Roughly 84% are `name`.

### Drug linkage: 19% is the wrong denominator

19.1% of trials have a `TESTED_IN` edge. That number counts every behavioural,
device and surgical trial as a drug-linkage failure, and they are not - those
trials name no drug and should resolve to nothing.

ClinicalTrials.gov labels its interventions by type, which gives the honest
split:

| | trials | |
|---|---|---|
| name a Drug or Biological | 232,988 | 39.1% — the only ones that should resolve |
| non-drug interventions only | 271,800 | 45.6% — behavioural, device, procedure |
| no intervention label | 91,702 | 15.4% |

Of the 232,988 that name a drug, **149,468 resolve — 64%**. That is the figure
to improve and the figure to quote.

So: "how many trials tested drug X" is answerable. "What fraction of trials
test a drug" is not a question this graph should be asked, because more than
half of all trials are not drug trials at all.

### How complete is the disease linkage, really

60% of trials carry a disease link. Measured against ClinicalTrials.gov
itself, for the trials this graph took from that registry:

| condition | ClinicalTrials.gov | this graph | recall |
|---|---|---|---|
| Eczema | 1,726 | 1,409 | 82% |
| Non-small cell lung cancer | 7,969 | 5,741 | 72% |
| Type 2 diabetes | 11,302 | 8,890 | 79% |

So a count from this graph is a **floor, not a total**. Say so. ct.gov's own
search also matches title and description text while the graph links only on
the condition field, so true recall is somewhat better than these numbers.

Two things are deliberately never linked and should not be chased:
`Healthy` (9,333 trials - healthy-volunteer studies have no disease), and any
term MeSH files outside its disease trees.

### A broad condition needs the rollup, and it now crosses vocabularies

```cypher
MATCH (d:Disease {name:'COVID-19', vocabulary:'MeSH'})
MATCH (t:ClinicalTrial)-[:STUDIES]->(x:Disease)
WHERE x = d OR (x)-[:SUBTYPE_OF*1..3]->(d)
RETURN count(DISTINCT t)
```

Without the rollup that question returns the trials linked to the MeSH node
alone. MeSH and ICD used to be unconnected trees, so an ICD-11 node like
`COVID-19, virus identified` was unreachable from `COVID-19` however you
traversed. 4,190 `SUBTYPE_OF` edges now join an ICD concept to the MeSH
disease it specialises, and that is what the `*1..3` above walks.

Always bound the depth. There is a gate check asserting no disease is its own
ancestor, but an unbounded `*` over 31,000 nodes is slow even when the tree is
sound.

### Dates do not compare

Stored as strings in six formats, one of which is the literal text
`"Approved Prior to Jan 1, 1982"`. `WHERE p.expire_date < '2027'` is
meaningless. Parse in your own code.

### 93% of Substances have no name

2.87M ChEMBL research compounds carry only an identifier. `MATCH (s:Substance)`
mostly returns things with no name, no target and no product. Anchor on
something real instead.

### Three sources are frozen

Orange Book, ChiCTR and jRCT are blocked at IP level. Their data is in the
graph and cannot be refreshed. **Orange Book supplies essentially all Patent
and Exclusivity nodes**, so patent-expiry answers are as of the last successful
scrape.

### Quote a phrase in a full-text query

The index is Lucene. An unquoted phrase is an **OR of its words**, not the
phrase:

```cypher
queryNodes('document_titles','gene therapy')       // 57,443 trials
queryNodes('document_titles','"gene therapy"')     //    583 trials
```

The first counts every trial with "therapy" in its title — 54,876 on that
word alone. A 98x overcount, and it reads as a finding rather than an error.

This is the most dangerous query mistake available in this graph, because
nothing about the result looks wrong: a big number from a real index.

### `NA` is a value, not a gap

`ClinicalTrial.phase`, `ClinicalTrial.study_type` and `Product.status` are on
**every** node, carrying `NA` where the source said nothing usable.

```cypher
WHERE t.phase <> 'NA'          // correct
WHERE t.phase IS NOT NULL      // matches everything, filters nothing
```

This was deliberate: an absent property and a stated "not applicable" used to
be indistinguishable, and an observational study genuinely HAS no phase.
Beware the reverse error too - `Country.iso2 = 'NA'` is **Namibia**.

### Absence of data is not absence of the thing

Five of the eleven agencies hold **no products at all**: NHRA (Bahrain), DHA
(Dubai), DOH (Abu Dhabi), MOH-OM (Oman), MOPH-QA (Qatar). The nodes exist so
region queries work; nothing was ever published for them.

So "how many approvals in Qatar" returns 0 because the data is missing, not
because the market is empty. Say which you mean.

Same shape: all 38,914 MHRA products carry `status = 'NA'`, because that
agency's column is a row flag rather than a status. A UK product whose status
says nothing is not an unapproved product.

### `Product.status` is ten values, and `status_raw` is free text

`MARKETED APPROVED TENTATIVE_APPROVAL DISCONTINUED WITHDRAWN SUSPENDED
REFUSED EXPIRED UNDER_REVIEW NA`.

`MARKETED` covers the Orange Book's `Rx` and `OTC`, which describe how a
product is **sold**, not whether it still is. `APPROVED` means authorised but
not necessarily on a shelf. The agency's own wording survives in
`status_raw` - free text, so `CONTAINS`, never equality.

### `Target` is not only proteins

2,782 are organisms, 1,999 cell lines, 293 tissues, and 5,210 of 16,624 have
no relationship at all. "How many targets" is a misleading count. For
druggable proteins use `(t:Target {target_type:'SINGLE PROTEIN'})`.

### Some properties hold a list in one cell

`Disease.synonyms`, `Disease.tree_numbers`, `Substance.synonyms` and
`RegulatoryEvent.name` are semicolon-separated. Equality matches only a row
whose **entire** list is that one value.

```cypher
WHERE d.synonyms CONTAINS 'Atopic Eczema'      // correct
WHERE d.synonyms = 'Atopic Eczema'             // matches almost nothing
```

### Trial keys look doubled

`NCT:NCT01045135` is correct — 19 of 22 registries embed their prefix in the
id. The clean value is on the Identifier node.

---

## Performance

Measured on the live database (2 vCPU, whole store in page cache):

| query | time |
|---|---|
| indexed point lookup | 21 ms |
| 1 hop | 23 ms |
| 2 hops | 13 ms |
| products by agency | 38 ms |
| global aggregation over 1.3M edges | 1.27 s |

**Always bound your traversals.** `-[*1..5]->` over 16.8M relationships will
not finish; there is a 120 s transaction timeout, so an unbounded query costs
two minutes and returns nothing. Use explicit hops.

Two vCPU means ~2 queries at full speed. Sequential is fine; heavy parallelism
queues.

---

## Combining with the vector store

They are separate stores joined by identifiers, not by shared storage.

**Graph first when** the question is about relationships: what targets this,
where is it approved, which trials tested it, what else hits this protein.

**Vector store first when** the question is about what a document *says*:
contraindications, warnings, dosing, wording of a label.

Bridging: MHRA filenames embed the licence number
(`..._PL347710263_par.pdf`), and the graph holds 39,002 `MHRA_PL` identifiers.
So graph → identifier → filtered document search is a real path.

⚠️ **SPL setids do not reach the vector store.** DailyMed publishes only CSVs
to S3 — no documents — so its 393,581 setids point at labels that were never
indexed.
