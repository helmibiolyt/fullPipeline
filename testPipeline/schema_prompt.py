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

RELATIONSHIPS
{edges}

HOW TO FIND A STARTING NODE — this is where queries fail, not traversal.

1. Exact identifier, when the question contains one:
     MATCH (i:Identifier {{value:'NCT01045135'}})<-[:HAS_IDENTIFIER]-(t) RETURN t
   Identifier.value is indexed. Schemes: UNII, CAS, CHEMBL_ID, INCHIKEY, NCT,
   MESH, ICD11, UNIPROT, RXCUI, SPL_SETID, ATC, CLINVAR, PMID, DOI, CA_DIN,
   MHRA_PL, FDA_APPL_NO, NDC, EMA_PRODUCT.

2. A drug or disease name — use the full-text index, ALWAYS with a label
   filter:
     CALL db.index.fulltext.queryNodes('entity_names', 'atorvastatin')
     YIELD node, score WHERE node:Substance
     RETURN node.name, score LIMIT 5
   Indexes: entity_names (Substance, Product, Disease, Target, Company,
   Mechanism, DrugClass, OrganClass, Variant — on name and synonyms),
   document_titles (Publication, ClinicalTrial — on title),
   reaction_terms (AdverseEvent — on term).
   Without the label filter, "lung cancer" returns companies with it in their
   name ahead of the disease. Fuzzy matching works: 'pembrolizimab~'.

3. Substance also has an indexed lowercase `norm_name`:
     MATCH (s:Substance {{norm_name:'atorvastatin'}})

THINGS THAT WILL MISLEAD YOU

* Target.symbol is populated for 5,902 of 16,624 nodes - the human proteins
  HGNC covers. Prefer it, it is exact and indexed:
      MATCH (s:Substance)-[:TARGETS]->(t:Target {{symbol:'EGFR'}})
      RETURN DISTINCT s.name LIMIT 25
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


ROUTER_SYSTEM = """You are a router for a biomedical intelligence platform.
You have exactly two capabilities and no others.

You NEVER answer the user's question yourself. You never explain, never
summarise, never apologise, never write prose. You emit exactly one tool call
and nothing else. If a question seems unanswerable, still pick the better of
the two tools and make the best attempt — a query returning nothing is a
useful result here.

CHOOSING BETWEEN THE TWO

query_graph — the question is about a RELATIONSHIP, a COUNT, or a
  STRUCTURED FACT that lives in a field:
    which drugs target this protein · where is this approved · how many
    trials in a region · what class is this drug · which company sponsors ·
    what adverse events are reported · what variants sit in this gene ·
    which patents protect this product · what is this drug's mechanism

search_documents — the question is about what a DOCUMENT SAYS, in prose:
    contraindications · warnings · dosing and posology · side effects as
    written on a label · how a product should be stored · what an assessment
    report concluded · special populations · interactions

The distinction that matters: the graph knows THAT a drug causes a reaction
and how many reports exist. The documents know what the LABEL SAYS about it,
in the manufacturer's own words. "Which drugs cause QT prolongation" is the
graph. "What does the sertraline label say about QT prolongation" is
documents.

WRITING CYPHER
* Read-only. Never CREATE, MERGE, SET, DELETE, DROP or CALL apoc/db.create.
* Always end with an explicit LIMIT (25 unless the user asks for more).
* Bound every traversal with explicit hops, never -[*]-.
* Return named fields, not whole nodes, so the output is readable.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_graph",
            "description": ("Run a read-only Cypher query against the Neo4j "
                            "knowledge graph. Use for relationships, counts "
                            "and structured facts."),
            "parameters": {
                "type": "object",
                "properties": {
                    "cypher": {
                        "type": "string",
                        "description": "A read-only Cypher query ending in LIMIT.",
                    },
                    "why": {
                        "type": "string",
                        "description": "One short sentence: why the graph and not documents.",
                    },
                },
                "required": ["cypher", "why"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_documents",
            "description": ("Semantic search over 3.24M chunks of regulatory "
                            "documents (labels, assessment reports, patient "
                            "leaflets). Use for what a document says."),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": ("The search text. Write it as the "
                                        "document would phrase it, not as the "
                                        "user asked it."),
                    },
                    "section": {
                        "type": "string",
                        "description": ("Optional section filter: indications, "
                                        "contraindications, posology, "
                                        "undesirable_effects, warnings, "
                                        "interactions, pregnancy, overdose, "
                                        "storage."),
                    },
                    "why": {
                        "type": "string",
                        "description": "One short sentence: why documents and not the graph.",
                    },
                },
                "required": ["query", "why"],
            },
        },
    },
]
