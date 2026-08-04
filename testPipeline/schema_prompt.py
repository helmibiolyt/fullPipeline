#!/usr/bin/env python3
"""The graph description handed to the router LLM.

Generated from graph/emit.py and graph/make_tech_doc.py rather than typed, so
the model is never told about a label or relationship the graph does not have.
A router prompt that drifts from the schema produces Cypher that runs and
returns nothing, which looks like "no data" rather than a broken prompt.
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "graph"))

from emit import NODE_COLUMNS                      # noqa: E402
from make_tech_doc import EDGES                    # noqa: E402

# Node counts are read from the live graph, never written down here. They move
# on every rebuild - one sync added 860 trials - so a hardcoded number starts
# lying to the router the first time anything is loaded, and a router told
# there are 6,324 publications when there are 8.7M will write a query with the
# wrong LIMIT and call it complete.
#
# Each `MATCH (n:Label) RETURN count(n)` is answered from Neo4j's count store
# in constant time, so all 22 cost one round trip rather than a scan.
_counts: dict[str, int] | None = None


def live_counts(run_cypher) -> dict[str, int]:
    """label -> node count, fetched once per session."""
    global _counts
    if _counts is not None:
        return _counts
    q = "\nUNION ALL\n".join(
        f"MATCH (n:{l}) RETURN '{l}' AS label, count(n) AS n"
        for l in sorted(NODE_COLUMNS))
    try:
        rows, _ = run_cypher(q)
        _counts = {r["label"]: r["n"] for r in rows}
    except Exception:                                        # noqa: BLE001
        # The router still works without them; it only loses the hint about
        # which labels are big enough to need a tight LIMIT.
        _counts = {}
    return _counts


def graph_schema(counts: dict[str, int] | None = None) -> str:
    counts = counts or {}

    def _row(label, cols):
        props = ", ".join(c for c in cols if c != "key")
        n = counts.get(label)
        size = f"{n:>11,}" if isinstance(n, int) else " " * 11
        return f"  (:{label}){size}   {props}"

    nodes = "\n".join(_row(l, cols)
                      for l, cols in sorted(NODE_COLUMNS.items()))

    edges = "\n".join(
        f"  (:{v[0]})-[:{k}]->(:{v[1]})"
        for k, v in sorted(EDGES.items()))

    return f"""NODE LABELS  (label, live count, properties besides `key`)
{nodes}

RELATIONSHIPS — the arrow direction is exact. Writing one backwards returns
zero rows and no error, which is the most common way a query fails here.
{edges}

DIRECTIONS THAT ARE ROUTINELY GOT BACKWARDS
  (:ClinicalTrial)-[:SPONSORED_BY]->(:Company)   NOT Company -> Trial
  (:Substance)-[:TESTED_IN]->(:ClinicalTrial)    NOT Trial -> Substance
  (:Product)-[:CONTAINS]->(:Substance)           NOT Substance -> Product
  (:Product)-[:PROTECTED_BY]->(:Patent)          patents hang off PRODUCTS
  (:Product)-[:HAS_APPROVAL]->(:Approval)        approvals hang off PRODUCTS
                                                 — Substance has NONE, 0 edges
  (:Substance)-[:SUBJECT_OF]->(:RegulatoryEvent) recalls hang off SUBSTANCES
  (:Publication)-[:MENTIONS]->(:Substance)       NOT Substance -> Publication
  (anything)-[:HAS_IDENTIFIER]->(:Identifier)    NOT Identifier -> entity

HOW TO FIND A STARTING NODE — this is where queries fail, not traversal.

1. Exact identifier, when the question contains one:
     MATCH (e)-[:HAS_IDENTIFIER]->(:Identifier {{value:'NCT01045135'}}) RETURN e
   Identifier.value is indexed. Schemes: UNII, CAS, CHEMBL_ID, INCHIKEY, NCT,
   MESH, ICD11, UNIPROT, RXCUI, SPL_SETID, ATC, CLINVAR, PMID, DOI, CA_DIN,
   MHRA_PL, FDA_APPL_NO, NDC, EMA_PRODUCT.

2. A drug by name — Substance has an indexed lowercase `norm_name`. Use it:
     MATCH (s:Substance {{norm_name:'atorvastatin'}})
   NEVER `WHERE s.name = 'atorvastatin'` — stored names are upper or mixed
   case, so equality on a lowercase string matches nothing.

3. Anything else by name — the full-text index, ALWAYS with a label filter:
     CALL db.index.fulltext.queryNodes('entity_names','breast cancer')
     YIELD node WHERE node:Disease
     WITH node AS d LIMIT 3
     MATCH ...
   Indexes: entity_names (Substance, Product, Disease, Target, Company,
   Mechanism, DrugClass, OrganClass, Variant — name, synonyms and symbol),
   document_titles (Publication, ClinicalTrial — title), reaction_terms
   (AdverseEvent — term). Without the label filter "lung cancer" returns
   companies. Fuzzy works: 'pembrolizimab~'.

4. A CONDITION IS USUALLY SEVERAL DISEASE NODES, NOT ONE. MeSH files a
   condition under its clinical heading, which is often not the word anyone
   searches with. Eczema is the example that matters:

     Dermatitis, Atopic   1,815 trials   synonyms: Atopic Eczema, Infantile
                                         Eczema, Atopic Neurodermatitis
     Eczema                 251 trials
     Skin Diseases, Eczematous  9
     Eczema, Dyshidrotic        1

   Asking for `d.name = 'Eczema'` returns 251 of 1,962 and looks like a
   complete answer. The full-text index already told you the others — it
   matched them on their SYNONYMS. Use the nodes it returned, do not narrow
   back to the one whose name equals your search word:

     CALL db.index.fulltext.queryNodes('entity_names','eczema')
     YIELD node WHERE node:Disease
     WITH collect(node)[..8] AS ds
     UNWIND ds AS d
     MATCH (t:ClinicalTrial)-[:STUDIES]->(d)
     RETURN count(DISTINCT t) AS trials

   Read the synonyms before you decide which hits belong. Wiskott-Aldrich
   Syndrome matches 'eczema' too and is a different disease.

5. A BROAD CONDITION IS A TREE, AND THE PARENT NODE HOLDS ALMOST NOTHING.
   Trials are tagged with the specific condition, not the category above it.
   "Heart Diseases" is a MeSH category with 221 disease types beneath it:

     the 'Heart Diseases' node alone      1,923 trials
     the whole subtree                   29,379 trials

   A trial on heart failure is tagged Heart Failure, never Heart Diseases. So
   for any question about a category — heart disease, cancer, diabetes,
   infection — walk down with SUBTYPE_OF or you will report 6% of the answer
   as if it were all of it:

     MATCH (d:Disease)-[:SUBTYPE_OF*0..4]->(:Disease {{name:'Heart Diseases'}})
     MATCH (t:ClinicalTrial)-[:STUDIES]->(d)
     RETURN count(DISTINCT t) AS trials

   `*0..4` includes the parent itself. The arrow points UP - child SUBTYPE_OF
   parent - so this reads "every disease that is a kind of Heart Diseases".

   How to tell a category from a specific condition: look at what the
   full-text search returned. If the hits are things like Heart Valve
   Diseases, Heart Failure and Coronary Disease, you searched for a category
   and those are its children. Roll up. If they are spellings of one thing -
   Atopic Eczema, Infantile Eczema - collect the nodes instead (rule 4).

REAL VALUES — use these exactly, never invent one.
  Region.name    Asia · Central Asia · Europe · Latin America · MENA/GCC ·
                 North America · Oceania · South Asia · Sub-Saharan Africa
                 A COUNTRY IS NOT A REGION. Saudi Arabia is
                 (:Country {{key:'COUNTRY:SA'}}); country keys are ISO-2.
  RegulatoryAgency.code  FDA EMA MHRA PMDA HC SFDA NHRA DHA DOH MOH-OM MOPH-QA
  RegulatoryEvent.type   recall · shortage · orphan_designation · referral ·
                 safety_alert · dhpc · paediatric_investigation_plan
  Product.status  MARKETED APPROVED TENTATIVE_APPROVAL DISCONTINUED
                 WITHDRAWN SUSPENDED REFUSED EXPIRED UNDER_REVIEW NA
                 On every product. MARKETED covers the Orange Book's Rx and
                 OTC, which say how a product is sold, not whether it is.
                 APPROVED means authorised but not necessarily on a shelf.
                 All 38,914 MHRA products are NA - that agency's column is a
                 row flag, not a status. The agency's own word is in
                 status_raw (free text, use CONTAINS).

  ClinicalTrial.study_type  INTERVENTIONAL OBSERVATIONAL EXPANDED_ACCESS NA
                 Exactly these four, on every trial. The registries' own
                 wording - a purpose like "Screening", a modality like
                 "Ayurveda" - is in study_type_raw, which is free text and
                 needs CONTAINS, not equality.
  ClinicalTrial.registry  lowercase: clinicaltrials.gov · chictr · ctri · irct
                 · eu_ctr · anzctr · isrctn · ctis · drks · nl-omon
  ClinicalTrial.phase   PHASE1 PHASE2 PHASE3 PHASE4 EARLY_PHASE1 PHASE0
                 PHASE1_PHASE2 PHASE2_PHASE3 PHASE3_PHASE4, or NA. Every
                 trial has this property - NA means no phase applies (most
                 observational studies) or the registry never stated one.
                 Normalised, so equality works. For trials with a real phase
                 filter `t.phase <> 'NA'`, NOT `t.phase IS NOT NULL` - that
                 is true for every trial in the graph and filters nothing.
  ClinicalTrial.status  COMPLETED RECRUITING NOT_YET_RECRUITING
                 ACTIVE_NOT_RECRUITING ENROLLING_BY_INVITATION TERMINATED
                 WITHDRAWN SUSPENDED

QUERY RECIPES — follow these shapes.

Drugs against a protein:
    MATCH (s:Substance)-[:TARGETS]->(t:Target {{symbol:'EGFR'}})
    RETURN DISTINCT s.name LIMIT 5000

Where a drug is approved — Product.name is a BRAND name, so never filter
products by an ingredient; traverse CONTAINS instead:
    MATCH (s:Substance {{norm_name:'atorvastatin'}})<-[:CONTAINS]-(p:Product)
    MATCH (p)-[:APPROVED_BY]->(a:RegulatoryAgency)
    RETURN a.code, a.region, count(p) AS products ORDER BY products DESC LIMIT 5000

Products in a country — via the agency, not APPROVED_IN (that points at a
Region):
    MATCH (p:Product)-[:APPROVED_BY]->(:RegulatoryAgency {{code:'SFDA'}})
    RETURN p.name, p.form LIMIT 5000

Trials in a country or region:
    MATCH (t:ClinicalTrial)-[:CONDUCTED_IN]->(:Country {{key:'COUNTRY:SA'}})
    RETURN count(DISTINCT t) AS trials
    // region: -[:CONDUCTED_IN]->(:Country)-[:IN_REGION]->(:Region {{name:'MENA/GCC'}})

Trials of a drug, and trials of a disease:
    MATCH (s:Substance {{norm_name:'pembrolizumab'}})-[:TESTED_IN]->(t:ClinicalTrial)
    RETURN t.registry, t.title, t.status LIMIT 5000
    // by disease, with a phase filter:
    CALL db.index.fulltext.queryNodes('entity_names','breast cancer')
    YIELD node WHERE node:Disease WITH node AS d LIMIT 3
    MATCH (t:ClinicalTrial {{phase:'PHASE3'}})-[:STUDIES]->(d)
    RETURN t.registry, t.title LIMIT 5000

Adverse events for a drug — MATCH THE SALT FORMS TOO. Safety reports name
whatever the reporter wrote, which is usually the salt: plain 'metformin' has
ZERO adverse events while 'metformin hydrochloride' has 1,370. Always widen
the match by name prefix and sum across the forms:
    MATCH (s:Substance)-[e:HAS_ADVERSE_EVENT]->(a:AdverseEvent)
    WHERE s.norm_name = 'metformin' OR s.norm_name STARTS WITH 'metformin '
    RETURN a.term AS reaction, sum(e.report_count) AS reports
    ORDER BY reports DESC LIMIT 5000
Do NOT rely on IS_SALT_OF to find the forms - that hierarchy comes from
ChEMBL and is wrong in places (metformin hydrochloride points at METFORMIN
C-11, a radiolabelled tracer). The name prefix is the dependable route.

The same widening applies to any question about a specific drug where the
first attempt returns nothing: try the prefix before concluding the graph has
no data.

Drugs for an organ class:
    // the other direction, via the MedDRA organ class:
    MATCH (o:OrganClass) WHERE toLower(o.name) CONTAINS 'cardiac'
    MATCH (s:Substance)-[e:HAS_ADVERSE_EVENT]->(a:AdverseEvent)-[:IN_ORGAN_CLASS]->(o)
    RETURN s.name, sum(e.report_count) AS reports ORDER BY reports DESC LIMIT 5000

Recalls naming a drug:
    MATCH (s:Substance {{norm_name:'valsartan'}})-[:SUBJECT_OF]->(e:RegulatoryEvent)
    WHERE e.type = 'recall' RETURN e.name, e.status LIMIT 5000

An identifier of a drug:
    MATCH (s:Substance {{norm_name:'atorvastatin'}})-[:HAS_IDENTIFIER]->(i:Identifier)
    WHERE i.scheme = 'UNII' RETURN i.value LIMIT 5

Variants in a gene:
    MATCH (v:Variant)-[:VARIANT_IN]->(:Target {{symbol:'BRAF'}})
    RETURN v.name, v.clinical_significance LIMIT 5000

"IS THIS DRUG APPROVED" — there is no approved flag on Substance, and
Substance has NO HAS_APPROVAL edge at all. A substance is approved when some
agency lists a product containing it:
    WHERE EXISTS {{ (:Product)-[:CONTAINS]->(s) }}
Use that as a filter, never a join to Approval from a Substance.

Comparing investigational compounds with approved ones — the shape behind
"which approved drugs work like something in trials":
    MATCH (inv:Substance)-[:TESTED_IN]->(:ClinicalTrial {{phase:'PHASE2'}})
    MATCH (inv)-[:HAS_MECHANISM]->(m:Mechanism)
    MATCH (appr:Substance)-[:HAS_MECHANISM]->(m)
    WHERE appr <> inv AND EXISTS {{ (:Product)-[:CONTAINS]->(appr) }}
    RETURN m.name AS mechanism, appr.name AS approved_drug,
           collect(DISTINCT inv.name)[..3] AS investigational
    LIMIT 5000
Note HAS_MECHANISM is only 7,442 edges, so mechanism-based comparison covers
a small, well-characterised slice. TARGETS (11,236 edges) is the wider
alternative when the question is really about what a drug acts on.

THINGS THAT WILL MISLEAD YOU

* Target.symbol is populated for 5,902 of 16,624 nodes - the human proteins
  HGNC covers. Prefer it, it is exact and indexed:
      MATCH (s:Substance)-[:TARGETS]->(t:Target {{symbol:'EGFR'}})
      RETURN DISTINCT s.name LIMIT 5000
  The other 10,722 are non-human or complexes and carry only a protein name.
  If a symbol lookup returns nothing, fall back to the full-text index with
  the protein name expanded - EGFR -> "epidermal growth factor receptor",
  HER2 -> "receptor tyrosine protein kinase erbB-2", PD-1 -> "programmed cell
  death protein 1", BRAF -> "serine/threonine-protein kinase B-raf".
  Never use `WHERE t.name = '...'`: stored names are capitalised, so equality
  on a lowercase phrase matches nothing.

* A broad condition needs a rollup, and the tree now crosses vocabularies.
  4,190 edges join an ICD concept to the MeSH disease it specialises, so
  `COVID-19, virus identified` is reachable from `COVID-19`:
      MATCH (d:Disease {{name:'COVID-19', vocabulary:'MeSH'}})
      MATCH (t:ClinicalTrial)-[:STUDIES]->(x:Disease)
      WHERE x = d OR (x)-[:SUBTYPE_OF*1..3]->(d)
      RETURN count(DISTINCT t)
  Bound the depth. `*` unbounded over 31,000 disease nodes is slow.
* ONE DRUG HAS MANY PRODUCT RECORDS, and they disagree. Each agency files
  its own, and the same substance appears under several with different
  statuses - rimegepant is filed once as TENTATIVE_APPROVAL and again as
  NURTEC ODT with status MARKETED. Answering from the first row you see gets
  this wrong, and it reads as authoritative because the row is real.
      MATCH (s:Substance)<-[:CONTAINS]-(p:Product)
      WHERE s.norm_name = 'rimegepant'
      RETURN p.name, p.agency, p.status, p.status_raw
  Look at ALL of them before saying whether a drug is approved.
* Five of the eleven agencies hold NO products: NHRA (Bahrain), DHA (Dubai),
  DOH (Abu Dhabi), MOH-OM (Oman), MOPH-QA (Qatar). The nodes exist so the
  region query works, but nothing was ever published for them. A count of
  approvals in Qatar returns 0 because the data is absent, NOT because
  nothing is approved there - say which of the two you mean, never report 0
  as a finding about the market.
* Some properties hold a semicolon-separated LIST in one cell, so equality
  matches only a row whose whole list is that one value. Use CONTAINS:
  Disease.synonyms, Disease.tree_numbers, Substance.synonyms,
  RegulatoryEvent.name.
      MATCH (d:Disease) WHERE d.synonyms CONTAINS 'Atopic Eczema'
* Target is not only proteins. 2,782 are organisms, 1,999 cell lines, 293
  tissues, and 5,210 of the 16,624 have no relationship at all. "How many
  targets" is a misleading count - for druggable proteins say
  (t:Target {{target_type:'SINGLE PROTEIN'}}).
* The *_raw properties - Product.status_raw, ClinicalTrial.study_type_raw -
  keep the source's own wording, in every case and spelling it used. They are
  free text: CONTAINS, never equality, and never GROUP BY them.
* Trial keys look doubled — `NCT:NCT01045135` is correct. 19 of 22 registries
  embed their prefix in the id. Filter registry with
  (t:ClinicalTrial {{registry:'clinicaltrials.gov'}}).
* Region 'MENA/GCC' is the WHOLE region — Israel, Iran, Egypt, North Africa —
  89,328 trials. The six Gulf states alone are 3,793. If the user means the
  Gulf, name the countries.
* Dates are STRINGS in six formats, one of them the literal text
  "Approved Prior to Jan 1, 1982". Never compare them with < or >.
* 93% of Substance nodes have no name — 2.87M ChEMBL research compounds with
  only an identifier. Never start a query with a bare MATCH (s:Substance).
* Every edge carries `match_method`. 'structured' means the source stated it;
  'name' means free prose was matched against a dictionary and is a hint only.
  CONDUCTED_IN, STUDIES and ABOUT are entirely name-matched.

PERFORMANCE — you MUST bound traversals. There is a 120s timeout and
16.8M relationships; `-[*1..5]->` will never finish. Use explicit hops and
always end with LIMIT.

AGGREGATE EARLY. Transaction memory is capped at 512 MB, and chaining several
MATCH clauses before any aggregation builds every combination in memory first.
This exhausts it:

    MATCH (s:Substance)-[:TESTED_IN]->(t:ClinicalTrial)
    MATCH (p:Product)-[:CONTAINS]->(s)
    MATCH (p)-[:HAS_APPROVAL]->(a:Approval)
    WITH t, count(DISTINCT a) AS n ORDER BY n DESC LIMIT 5000
    RETURN t.title, n

The same question, aggregating at each step so the row count falls before the
next join, returns 2,188 rows in 1.2 seconds:

    MATCH (p:Product)-[:HAS_APPROVAL]->(:Approval)
    WITH p, count(*) AS approvals
    MATCH (p)-[:CONTAINS]->(s:Substance)
    WITH s, sum(approvals) AS approvals
    ORDER BY approvals DESC LIMIT 25
    MATCH (s)-[:TESTED_IN]->(t:ClinicalTrial)
    RETURN s.name, approvals, t.title LIMIT 5000

Put ORDER BY and LIMIT on the aggregate, not only at the end: narrowing to the
top 25 substances before expanding to their trials is what keeps it small.
Anchor on something specific wherever the question allows it - an unanchored
three-way join over this graph is hundreds of millions of paths.
"""




# --------------------------------------------------------------------------
# Stage 1: plan. Both stores are queried on every question, never one.
#
# An earlier version made the model choose between them. That was wrong twice
# over: it forced a judgement call the model often got wrong, and it threw
# away half the evidence even when it chose correctly. The graph knows THAT a
# drug causes a reaction and in how many reports; the label says what the
# manufacturer WROTE about it. A good answer usually wants both.
PLANNER_SYSTEM = """You write the two queries that will answer a question
about drugs, trials, regulation and safety.

You NEVER answer the question here. You emit one tool call containing both
queries and nothing else.

You must always produce BOTH:

  cypher          a read-only Cypher query against the knowledge graph, for
                  relationships, counts and structured facts
  document_query  a search phrase for 3.24M chunks of regulatory documents
                  (labels, assessment reports, patient leaflets), for what a
                  document actually says

Write each one as if it were the only source you had. If a question leans
towards one side, still write the other - a query that returns nothing costs
one round trip and is itself informative. Phrase document_query the way a
regulatory document would phrase it, not the way the user asked.

WRITING CYPHER
* Read-only. Never CREATE, MERGE, SET, DELETE, DROP or CALL apoc/db.create.
* Always end with an explicit LIMIT. Use 5000 - the point of the limit is
  to stop a runaway scan, NOT to shorten the answer. A low limit silently
  truncates: 25 rows back from a question with 132 real matches reads as
  '25 drugs do this', which is wrong.
* A count or aggregate needs no LIMIT at all. Prefer count() when the
  question asks how many - it is exact and costs one row.
* Bound every traversal with explicit hops, never -[*]-.
* Return named fields, not whole nodes.
"""

PLAN_TOOL = [{
    "type": "function",
    "function": {
        "name": "gather",
        "description": ("Query both stores. Always supply both a Cypher "
                        "query and a document search phrase."),
        "parameters": {
            "type": "object",
            "properties": {
                "cypher": {
                    "type": "string",
                    "description": "Read-only Cypher ending in LIMIT.",
                },
                "document_query": {
                    "type": "string",
                    "description": ("Search phrase for the document corpus, "
                                    "worded as a regulatory document would."),
                },
                "section": {
                    "type": "string",
                    "description": ("Optional document section filter: "
                                    "indications, contraindications, posology, "
                                    "undesirable_effects, warnings, "
                                    "interactions, pregnancy, overdose, "
                                    "storage."),
                },
            },
            "required": ["cypher", "document_query"],
        },
    },
}]

# --------------------------------------------------------------------------
# Stage 2: answer. The model sees only what the two stores returned.
ANSWER_SYSTEM = """You answer using ONLY the two result sets provided.

You have no other knowledge. If the results do not contain the answer, say so
plainly - do not fill the gap from memory. An honest "the graph returned no
trials for this" is worth more than a fluent guess, because the whole point of
this system is that every claim is traceable to a row or a document.

WORK THROUGH BOTH SETS BEFORE YOU WRITE ANYTHING

1. The graph rows are structured fact. Read the column names, then the values.
   A count in a column is exact - use it as given. Where a header says how
   many rows matched in total, that total is the answer to "how many", not
   the number of rows printed underneath it.

2. Go through the document chunks ONE AT A TIME. For each chunk ask: does
   this contain anything that answers the question? Take the specific detail
   if it does - the dose, the contraindication, the wording of the warning,
   the number - and note which agency it came from. Skip it entirely if it
   does not. Chunks are retrieved by similarity, so some will be about the
   right drug and the wrong topic; a chunk being present is not evidence it
   is relevant.

3. Then combine. The two sets answer different halves of most questions: the
   graph knows THAT a relationship exists and how often, the document says
   what the manufacturer actually WROTE about it. Use the graph for the
   structure - which drugs, how many, what class, which agency - and the
   documents for the wording, the caveats and the clinical detail. An answer
   built from both is the point of this system; an answer built from one is
   acceptable only when the other genuinely returned nothing useful.

HOW TO WRITE IT
* Lead with the answer in one or two sentences. No preamble.
* Then the detail that supports it. Use a short list where there are several
  items; prose where there is one thing to say.
* Attribute inline as you go: (graph) for a fact from the knowledge graph,
  (MHRA), (EMA), (PMDA) and so on for a document, naming the agency the chunk
  came from.
* Give numbers exactly as they appear. Never round a count, never invent one.
* Where the two sources disagree, say so and give both. That is a finding, not
  a problem to smooth over.
* Where one source returned nothing, say which - it tells the reader where the
  answer's limits are.
* No markdown headings. Plain paragraphs and simple dashes for lists.
* Be brief. Six sentences is usually plenty.

Finish with one line beginning exactly "SOURCES: " listing what you actually
used - the graph, and the agencies whose documents you quoted. If you used
nothing, write "SOURCES: none".
"""
