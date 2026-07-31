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

REAL VALUES — use these exactly, never invent one.
  Region.name    Asia · Central Asia · Europe · Latin America · MENA/GCC ·
                 North America · Oceania · South Asia · Sub-Saharan Africa
                 A COUNTRY IS NOT A REGION. Saudi Arabia is
                 (:Country {{key:'COUNTRY:SA'}}); country keys are ISO-2.
  RegulatoryAgency.code  FDA EMA MHRA PMDA HC SFDA NHRA DHA DOH MOH-OM MOPH-QA
  RegulatoryEvent.type   recall · shortage · orphan_designation · referral ·
                 safety_alert · dhpc · paediatric_investigation_plan
  ClinicalTrial.registry  lowercase: clinicaltrials.gov · chictr · ctri · irct
                 · eu_ctr · anzctr · isrctn · ctis · drks · nl-omon
  ClinicalTrial.phase   PHASE1 PHASE2 PHASE3 PHASE4 EARLY_PHASE1
                 PHASE1_PHASE2 PHASE2_PHASE3 PHASE3_PHASE4, or "" where no
                 phase applies. These are normalised - equality works.
  ClinicalTrial.status  COMPLETED RECRUITING NOT_YET_RECRUITING
                 ACTIVE_NOT_RECRUITING ENROLLING_BY_INVITATION TERMINATED
                 WITHDRAWN SUSPENDED

QUERY RECIPES — follow these shapes.

Drugs against a protein:
    MATCH (s:Substance)-[:TARGETS]->(t:Target {{symbol:'EGFR'}})
    RETURN DISTINCT s.name LIMIT 500

Where a drug is approved — Product.name is a BRAND name, so never filter
products by an ingredient; traverse CONTAINS instead:
    MATCH (s:Substance {{norm_name:'atorvastatin'}})<-[:CONTAINS]-(p:Product)
    MATCH (p)-[:APPROVED_BY]->(a:RegulatoryAgency)
    RETURN a.code, a.region, count(p) AS products ORDER BY products DESC LIMIT 500

Products in a country — via the agency, not APPROVED_IN (that points at a
Region):
    MATCH (p:Product)-[:APPROVED_BY]->(:RegulatoryAgency {{code:'SFDA'}})
    RETURN p.name, p.form LIMIT 500

Trials in a country or region:
    MATCH (t:ClinicalTrial)-[:CONDUCTED_IN]->(:Country {{key:'COUNTRY:SA'}})
    RETURN count(DISTINCT t) AS trials
    // region: -[:CONDUCTED_IN]->(:Country)-[:IN_REGION]->(:Region {{name:'MENA/GCC'}})

Trials of a drug, and trials of a disease:
    MATCH (s:Substance {{norm_name:'pembrolizumab'}})-[:TESTED_IN]->(t:ClinicalTrial)
    RETURN t.registry, t.title, t.status LIMIT 500
    // by disease, with a phase filter:
    CALL db.index.fulltext.queryNodes('entity_names','breast cancer')
    YIELD node WHERE node:Disease WITH node AS d LIMIT 3
    MATCH (t:ClinicalTrial {{phase:'PHASE3'}})-[:STUDIES]->(d)
    RETURN t.registry, t.title LIMIT 500

Adverse events for a drug, and drugs for an organ class:
    MATCH (s:Substance {{norm_name:'ibuprofen'}})-[e:HAS_ADVERSE_EVENT]->(a:AdverseEvent)
    RETURN a.term, e.report_count ORDER BY e.report_count DESC LIMIT 500
    // the other direction, via the MedDRA organ class:
    MATCH (o:OrganClass) WHERE toLower(o.name) CONTAINS 'cardiac'
    MATCH (s:Substance)-[e:HAS_ADVERSE_EVENT]->(a:AdverseEvent)-[:IN_ORGAN_CLASS]->(o)
    RETURN s.name, sum(e.report_count) AS reports ORDER BY reports DESC LIMIT 500

Recalls naming a drug:
    MATCH (s:Substance {{norm_name:'valsartan'}})-[:SUBJECT_OF]->(e:RegulatoryEvent)
    WHERE e.type = 'recall' RETURN e.name, e.status LIMIT 500

An identifier of a drug:
    MATCH (s:Substance {{norm_name:'atorvastatin'}})-[:HAS_IDENTIFIER]->(i:Identifier)
    WHERE i.scheme = 'UNII' RETURN i.value LIMIT 5

Variants in a gene:
    MATCH (v:Variant)-[:VARIANT_IN]->(:Target {{symbol:'BRAF'}})
    RETURN v.name, v.clinical_significance LIMIT 500

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
    LIMIT 500
Note HAS_MECHANISM is only 7,442 edges, so mechanism-based comparison covers
a small, well-characterised slice. TARGETS (11,236 edges) is the wider
alternative when the question is really about what a drug acts on.

THINGS THAT WILL MISLEAD YOU

* Target.symbol is populated for 5,902 of 16,624 nodes - the human proteins
  HGNC covers. Prefer it, it is exact and indexed:
      MATCH (s:Substance)-[:TARGETS]->(t:Target {{symbol:'EGFR'}})
      RETURN DISTINCT s.name LIMIT 500
  The other 10,722 are non-human or complexes and carry only a protein name.
  If a symbol lookup returns nothing, fall back to the full-text index with
  the protein name expanded - EGFR -> "epidermal growth factor receptor",
  HER2 -> "receptor tyrosine protein kinase erbB-2", PD-1 -> "programmed cell
  death protein 1", BRAF -> "serine/threonine-protein kinase B-raf".
  Never use `WHERE t.name = '...'`: stored names are capitalised, so equality
  on a lowercase phrase matches nothing.

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
* Always end with an explicit LIMIT. Use 500 - the point of the limit is
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
