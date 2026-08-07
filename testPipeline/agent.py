#!/usr/bin/env python3
"""Let the model decide what to look up, in what order, and when to stop.

The previous design fired exactly one Cypher query and one document search, in
parallel, for every question. That is fine when the two halves are
independent, and wrong whenever one depends on the other:

  · "what does the label say about the drugs that target EGFR" needs the
    graph FIRST, because the drug names are the search terms
  · "which of the drugs mentioned in this warning are approved in the Gulf"
    needs the documents first
  · "how many trials for X" needs the graph and nothing else, and the
    document search is a wasted round trip
  · a query that returns nothing deserves a second attempt with a different
    starting node, which a fixed plan can never make

So this is a loop instead. The model holds two tools, sees the result of each
call, and decides what to do next - including stopping. It cannot answer from
its own knowledge: the only content it ever sees is tool output, and the
system prompt says every claim must trace to a row or a chunk.

Bounded on three axes, because an agent that can loop is an agent that can
loop forever: MAX_STEPS tool calls, MAX_SECONDS wall clock, and a token
budget. Whichever binds first ends the loop and the model answers from what it
has.
"""
from __future__ import annotations

import json
import re
import time

import ask as A
import pipeline as P
from schema_prompt import graph_schema, live_counts

# Four independent bounds. A loop that can call tools is a loop that can call
# them forever, and each of these stops a different runaway:
#
#   MAX_STEPS    total calls - the overall ceiling
#   MAX_GRAPH    Cypher calls - stops it grinding on one query it cannot fix
#   MAX_DOCS     searches - stops it fishing through the corpus a drug at a
#                time when the question wanted a summary
#   MAX_SECONDS  wall clock - covers a slow tool rather than a chatty model
#
# When a tool's budget is spent it is REMOVED from the list offered, so the
# model cannot call it and be refused - it simply sees one tool, or none, and
# writes the answer.
#: Budgets. Raised from 8/4/4 for the persona catalog, which is a different
#: shape of question from the bank these were tuned on.
#:
#: The old sweep found MAX_GRAPH=4 optimal and raising it to 10 changed
#: nothing. That was measured on 116 questions that were mostly ONE lookup -
#: "how many trials in the Gulf" - where a bigger budget has nothing to spend
#: itself on. The catalog is a third synthesis work: map a pipeline by phase,
#: mechanism and sponsor; draft a clinical overview with source traceability;
#: compare two products across efficacy and safety. Those need the graph to fix
#: a set and the documents to say what it found, several times over.
#:
#: So the old measurement does not transfer, and it would have been wrong to
#: cite it as if it did.
#:
#: MAX_DOCS is the larger of the two on purpose: the document half of the
#: catalog skews complex - 10 of its 18 questions - because prose is where
#: synthesis comes from.
MAX_STEPS = 20
MAX_GRAPH = 10
MAX_DOCS = 12
#: Raised with the budget. A 90s ceiling would have cut off any question that
#: actually used the new headroom, and reported it as a timeout rather than as
#: the deeper answer it was in the middle of building.
MAX_SECONDS = 300
STEP_TOKENS = 4000

# How many times to re-ask when the provider ignores tool_choice="required"
# on the first step. One was measured as enough twice and not enough once.
FIRST_RETRIES = 3

#: Consecutive calls to one store before it is withheld for a single call.
#:
#: This was a mechanical fix for the gggg failure - four graph queries, no
#: document search, then "no data found" for something sitting in the corpus.
#: It is kept only as a backstop, and switch_on_miss now defaults to False,
#: because the cause was the PROMPT: its only advice on an empty result was to
#: rewrite the Cypher, so nothing ever suggested the fact might be written down
#: rather than tabulated. With the prompt fixed, all four questions that used
#: to fail answer with this rule turned off.
#:
#: Its benefit was never reproducible either - measured twice, it helped once
#: and hurt once, both at n=22. A rule that cannot be shown to work is worse
#: than no rule: it reads as protection nobody has verified.
RUN_BEFORE_SWITCH = 3

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_graph",
            "description": (
                "Run one read-only Cypher query against the knowledge graph. "
                "Use for relationships, counts and structured facts: which "
                "drugs target a protein, where something is approved, how "
                "many trials, what class a drug is in, which variants sit in "
                "a gene. Returns rows. Call it again with a different query "
                "if the first returns nothing."),
            "parameters": {
                "type": "object",
                "properties": {
                    "cypher": {"type": "string",
                               "description": "Read-only Cypher ending in LIMIT."},
                    "why": {"type": "string",
                            "description": "One short line: what this is for."},
                },
                "required": ["cypher"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_documents",
            "description": (
                "Semantic search over 3.24M chunks of regulatory documents - "
                "labels, assessment reports, patient leaflets. Use for what a "
                "document SAYS: contraindications, warnings, dosing, "
                "interactions, what an assessment concluded. If the graph has "
                "already given you exact drug or product names, search with "
                "those rather than the user's wording."),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": ("Search text, phrased the way a "
                                              "regulatory document would.")},
                    "section": {"type": "string",
                                "description": ("Optional: indications, "
                                                "contraindications, posology, "
                                                "undesirable_effects, warnings, "
                                                "interactions, pregnancy, "
                                                "overdose, storage.")},
                    "why": {"type": "string",
                            "description": "One short line: what this is for."},
                },
                "required": ["query"],
            },
        },
    },
]

TOOLS.append({
    "type": "function",
    "function": {
        "name": "resolve_condition",
        "description": (
            "Find the Disease nodes for a condition, BEFORE querying about it. "
            "Returns every candidate with its trial count, how many disease "
            "types sit beneath it, and its synonyms - so you can tell a "
            "category from a specific condition and spot near-misses. Use this "
            "first for any question about a disease; matching on name equality "
            "silently returns a fraction of the answer."),
        "parameters": {
            "type": "object",
            "properties": {
                "condition": {"type": "string",
                              "description": "The condition as the user said it."},
                "why": {"type": "string",
                        "description": "One short line: what this is for."},
            },
            "required": ["condition"],
        },
    },
})

SYSTEM = """You answer questions about drugs, trials, regulation and safety
using three tools and nothing else.

You have NO knowledge of your own. Every fact in your answer must come from a
row or a document chunk a tool returned in this conversation. If the tools do
not have it, say so - an honest "the graph returned no trials for this" is
worth more than a fluent guess.

RESOLVE A CONDITION BEFORE YOU QUERY IT

For any question about a disease, call resolve_condition FIRST. It returns
every Disease node that mentions the condition, with how many trials each
holds, how many disease types sit beneath it, and its synonyms.

You need it because a condition is almost never one node. MeSH files eczema
under "Dermatitis, Atopic", so `d.name = 'Eczema'` returns 301 trials of
2,214. And "Heart Diseases" is a category whose own node holds 1,980 trials
while its 204 subtypes hold 31,141 - a trial on heart failure is tagged Heart
Failure, never its parent.

Read what comes back before choosing:
  many children  -> a CATEGORY. Roll it up with SUBTYPE_OF.
  no children, synonyms that are the same illness -> collect those keys.
  a synonym match that is a DIFFERENT disease -> leave it out. Wiskott-Aldrich
    Syndrome matches 'eczema' and is not eczema.

WHAT EACH STORE ACTUALLY HOLDS

The graph holds STRUCTURE - things that were tabulated. Substances, products,
approvals, trials, companies, targets, mechanisms, drug classes, adverse event
counts, identifiers. It can count, group and traverse. It holds no prose: there
is no sentence anywhere in it.

The documents hold PROSE - what a regulator actually wrote. Labels, assessment
reports, patient leaflets, safety alerts. Contraindications, warnings, dosing,
interactions, what an assessment concluded, how a risk is worded. It cannot
count, and it has no notion of "all" - it returns the passages closest to your
search text.

So the graph is where a number, a list or a relationship lives, and the
documents are where the wording lives. That is a difference in what they hold.
It is NOT an instruction to pick one.

HOW TO WORK

Use both stores. That is the default, not the exception.

Almost every real question is better answered with a count AND the prose around
it: how many trials, and what the label actually warns about. The graph gives
you the shape of the answer; the documents give you the substance, the caveats
and the wording a person can quote.

* Start wherever the answer most obviously lives, then check the other one.
* A passage does not have to answer the question to be worth having. Documents
  are prose - a chunk that is only partly on topic still tells you how a
  regulator words a risk, what a study concluded, what the surrounding context
  is. Take it as context and say what it is. Only an exact match counts as an
  exact answer, but partial relevance is still information.
* IN ORDER when the second lookup needs a name the first produces: ask the
  graph which drugs target EGFR, then search the documents using those names.
  This is the most valuable pattern here and it is worth several calls.
* Coming back with only structured rows, on a question where a document could
  have added the reasoning, is a thin answer even when the rows are right.

You have a generous budget. Use it. Spending calls to be more complete is the
behaviour that is wanted; stopping early with a bare number is not.

Call one tool at a time and look at what comes back.

WHEN A LOOKUP RETURNS NOTHING

Empty is information, and what it means depends on why.

If you think the graph HAS this and your query missed it, rewrite it once -
the full-text index instead of an exact name, a name prefix instead of
equality, a different starting node. Once.

If a second graph query also comes back empty, stop rewriting Cypher. Two
misses usually mean the fact is not tabulated - it is written in a document.
Search the documents before you conclude anything is absent. The corpus is
3.24 million passages; "the graph has no rows for this" is not the same as
"this is unknown", and saying the second when you have not looked is the worst
thing you can do here.

The same applies the other way: if two document searches return nothing useful,
the answer may be a structural fact the graph can give you directly.

Stop when you have enough to answer WELL - not at the first row that is
technically responsive. If a document could add the wording, the caveat or the
reasoning behind a number you already have, go and get it.

WRITING THE ANSWER

Lead with the answer in one or two sentences, then the supporting detail.
Attribute inline: (graph) for a fact from the knowledge graph, (MHRA), (EMA),
(PMDA) and so on for a document, naming the agency the chunk came from. Give
numbers exactly as returned - never round a count, never invent one. Where the
two sources disagree, say so and give both. Where one returned nothing, say
which. Plain paragraphs, no markdown headings, and be brief.

Finish with one line beginning exactly "SOURCES: " naming what you used.
"""


_TRAILING_LIMIT = re.compile(r"\bLIMIT\s+\d+\s*;?\s*$", re.I)

#: Counting stops here. A mechanism class with 1.6M rows is "more than we can
#: show" whether the number is 1.6M or 100k, and the count must not cost more
#: than the query it is explaining.
COUNT_CEILING = 200_000


_FULLTEXT_TERM = re.compile(
    r"queryNodes\(\s*'[^']+'\s*,\s*'([^']*)'", re.I)


def _phrase_check(cypher: str) -> str:
    """When a full-text search passes an UNQUOTED multi-word phrase, count
    what the quoted phrase would have matched and hand both numbers over.

    The index is Lucene, so an unquoted phrase is an OR of its words. Asked
    what trials investigate gene therapy, the agent searched 'gene therapy',
    got 57,443 and reported it. The phrase matches 583; the word "therapy"
    alone matches 54,876. A 98x overcount, stated as a finding, and the
    benchmark scored the answer OK because nothing about it looks wrong.

    15 of the benchmark's full-text calls were written this way, so it is the
    normal mistake rather than an unlucky one.

    Computing the other number rather than warning, for the reason
    _true_total exists: a warning is advice and gets weighed against the rows
    in front of it; a number is evidence.
    """
    m = _FULLTEXT_TERM.search(cypher)
    if not m:
        return ""
    term = m.group(1).strip()
    bare = term.replace('\\"', '"').strip()
    if len(bare.split()) < 2 or bare.startswith('"'):
        return ""
    quoted = cypher.replace(f"'{term}'", "'\\\"" + term + "\\\"'", 1)
    body = _TRAILING_LIMIT.sub("", quoted.strip()).strip()
    if not body:
        return ""
    try:
        rows, _ = A.run_cypher(f"CALL () {{ {body} }} WITH 1 AS x "
                               f"LIMIT {COUNT_CEILING} RETURN count(x) AS total")
        n = rows[0]["total"] if rows else None
    except Exception:                                        # noqa: BLE001
        return ""
    if n is None:
        return ""
    return (f"PHRASE NOT QUOTED - you searched {bare!r} unquoted, and this "
            f"index is Lucene, so that is an OR of its words. The QUOTED "
            f"phrase matches {n:,} rows. Use {n:,} if you meant the phrase, "
            f"and re-run with '\\\"{bare}\\\"' to see them. ")


def _true_total(cypher: str) -> int | None:
    """How many rows the query would have returned without its LIMIT.

    Telling the model "TRUNCATED" was not enough - it truncated on 14 of 22
    benchmark questions and then wrote conclusions about what was absent. A
    warning is advice; a number is evidence. Asked which trials share a
    mechanism with epilepsy drugs it saw 50 rows and reported that seven of
    eight mechanisms had none. The real figure was 5,017, and this is how it
    finds that out without reading 5,017 rows.

    Returns None when the count itself will not run, which is fine: the
    warning still stands, it just has no number attached.
    """
    body = _TRAILING_LIMIT.sub("", cypher.strip()).strip()
    if not body:
        return None
    # An explicit variable scope: CALL {...} without one is deprecated in 5.26
    # and prints a notification per call. Nothing is imported, so the scope is
    # empty. The inner ORDER BY is left alone - it is legal inside a subquery
    # and stripping it would need to know where the clause ends.
    counted = (f"CALL () {{ {body} }} WITH 1 AS x LIMIT {COUNT_CEILING} "
               f"RETURN count(x) AS total")
    # Checked AFTER wrapping, not before: the bare body has had its LIMIT
    # removed, so check_cypher would refuse it as "no LIMIT and not an
    # aggregate" and every count would silently return None.
    if A.check_cypher(counted):
        return None
    try:
        rows, _ = A.run_cypher(counted)
    except Exception:                                          # noqa: BLE001
        return None
    return rows[0]["total"] if rows else None


def _breakdown(cypher: str, col: str, cap: int = 30) -> list[dict] | None:
    """Group the FULL result by one column, ignoring the LIMIT.

    The warning was not enough on its own. Told that 50 rows were 50 furosemide
    out of 2,866, the model still answered from the 50 - a warning is something
    to weigh, and rows in front of it are something to read. So the missing
    distribution is computed and handed over as data, at which point there is
    nothing to weigh: the eight mechanisms and their real counts are simply
    there, next to the rows that only showed one of them.
    """
    body = _TRAILING_LIMIT.sub("", cypher.strip()).strip()
    if not body or not col.isidentifier():
        return None
    q = (f"CALL () {{ {body} }} RETURN `{col}` AS value, count(*) AS n "
         f"ORDER BY n DESC LIMIT {cap}")
    if A.check_cypher(q):
        return None
    try:
        rows, _ = A.run_cypher(q)
    except Exception:                                          # noqa: BLE001
        return None
    return rows or None


#: Added to a query that has no LIMIT, rather than refusing it.
#:
#: The guard exists so an unbounded MATCH cannot exhaust Neo4j's heap, and
#: refusing was the blunt way to get that. On the 116-question benchmark it
#: rejected 14 queries and every one was safe - "MATCH (m:Modality) RETURN
#: m.name" returns a dozen rows. Each refusal cost a lookup and taught the
#: model nothing, because the query was fine.
#:
#: Bounding it keeps the protection and spends the call on an answer. Large
#: enough that a real result is rarely cut, and the truncation machinery
#: reports the true total when it is.
DEFAULT_LIMIT = 300

# Written with explicit character classes rather than \s, \d and \b. Both of
# these were first written with the shorthand and both arrived with a literal
# backspace byte where \b should be: the pattern then silently matched nothing,
# every query looked unbounded, and the file looked correct in an editor. The
# same accident cost this project a day once already.
_ENDS_LIMIT = re.compile(r"LIMIT[ \t]+[0-9]+[ \t]*$", re.I)
_AGGREGATE = re.compile(r"(count|collect|sum|avg|min|max)[ \t]*[(]", re.I)


def _bound(cypher: str) -> tuple[str, bool]:
    """Append a LIMIT if the query has none. Returns (cypher, was_added).

    An aggregate is left alone: RETURN count(*) produces one row whatever the
    match size, and a LIMIT on it says nothing while making the result read as
    though it might have been cut.
    """
    q = cypher.strip().rstrip(";").rstrip()
    if not q or _ENDS_LIMIT.search(q) or _AGGREGATE.search(q):
        return cypher, False
    return f"{q} LIMIT {DEFAULT_LIMIT}", True


#: Which counter a tool spends from. resolve_condition is a graph read.
_BUDGET_OF = {"resolve_condition": "query_graph"}

#: Tool names the provider actually emits, mapped to the ones declared.
#:
#: MiniMax returns "graph" and "functions.graph" as often as "query_graph".
#: Unmapped, those fell through to the unknown-tool branch: the lookup never
#: ran, and the record written for it said tool="graph" - so a call that did
#: nothing was counted as a graph query in every metric built on these steps.
#: The docs-only arm showed sequences like "gggd" while touching the graph
#: exactly zero times.
_TOOL_ALIASES = {
    "resolve_condition": "resolve_condition", "resolve": "resolve_condition",
    "resolve_disease": "resolve_condition",
    "graph": "query_graph", "query_graph": "query_graph",
    "cypher": "query_graph", "search_graph": "query_graph",
    "documents": "search_documents", "search_documents": "search_documents",
    "docs": "search_documents", "document_search": "search_documents",
}


def _canonical_tool(name: str) -> str:
    """Resolve a provider's spelling of a tool name to the declared one."""
    n = (name or "").strip()
    if "." in n:                       # functions.graph, tools.query_graph
        n = n.rsplit(".", 1)[-1]
    return _TOOL_ALIASES.get(n.lower(), n)


def _run_graph(args: dict) -> tuple[str, dict]:
    """Execute Cypher, returning (text for the model, record for the page)."""
    cypher = (args.get("cypher") or "").strip()
    cypher, bounded = _bound(cypher)
    rec = {"tool": "graph", "why": args.get("why", ""), "query": cypher,
           "rows": [], "columns": [], "total": 0, "ms": 0, "error": "",
           "bounded": bounded}

    bad = A.check_cypher(cypher)
    if bad:
        rec["error"] = f"refused: {bad}"
        return f"REFUSED: {bad}. Rewrite the query.", rec

    wrong = P.check_directions(cypher)
    if wrong:
        # Caught before execution: a reversed arrow returns zero rows and no
        # error, which the model would read as "no such data".
        rec["error"] = "; ".join(wrong)
        return ("That query will match nothing. " + " ".join(wrong)
                + " Rewrite it with the arrows as the schema states them."), rec

    try:
        rows, ms = A.run_cypher(cypher)
    except Exception as e:                                   # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        hint = P._MEMORY_ADVICE if "Memory" in rec["error"] else ""
        return f"ERROR: {rec['error']}{hint}", rec

    rec["ms"] = ms
    rec["total"] = len(rows)
    rec["columns"] = list(rows[0].keys()) if rows else []
    rec["rows"] = [{k: A._fmt(v) for k, v in r.items()}
                   for r in rows[:P.PAGE_ROWS or len(rows)]]

    if not rows:
        return ("0 rows. The query ran and matched nothing - most often the "
                "starting node, not the traversal. Try the full-text index "
                "with a label filter, or a name prefix instead of equality."), rec

    shown = rows[:P.EVIDENCE_ROWS]
    body = "\n".join("  " + json.dumps({k: A._fmt(v) for k, v in r.items()},
                                       ensure_ascii=False) for r in shown)
    truncated = P._hit_limit(cypher, len(rows))
    head = f"{len(rows)} rows matched"
    if len(rows) > len(shown):
        # Only claim len(rows) is the total when it actually is one. On a
        # truncated result the real figure comes from the count below, and two
        # different totals in the same message is worse than none.
        head += (f"; first {len(shown)} shown" if truncated else
                 f"; first {len(shown)} shown - use {len(rows)} as the total, "
                 f"not the number of lines here")

    # A truncated result and a complete one look identical, and the model reads
    # the gap as absence. Asked which trials share a mechanism with epilepsy
    # drugs, it got LIMIT 50 rows that were 50/50 furosemide, and reported that
    # seven of the eight mechanisms had no trials at all. They had 5,017.
    #
    # So the warning leads instead of trailing, and it names the collapse: when
    # few distinct values fill the whole limit, the rows that did not fit are
    # the answer. Aggregating is the fix, not a bigger LIMIT - the model cannot
    # read 5,000 rows either.
    warn = _phrase_check(cypher)
    if truncated:
        total = _true_total(cypher)
        rec["true_total"] = total
        if total is None:
            warn = ("TRUNCATED - this is exactly the LIMIT, so rows were cut "
                    "and you cannot tell what is missing. ")
        else:
            more = f"{total:,}" + ("+" if total >= COUNT_CEILING else "")
            warn = (f"TRUNCATED - you saw {len(rows)} of {more} matching rows. "
                    f"Use {more} as the total. Do NOT describe anything as "
                    f"absent on the strength of these {len(rows)} rows. ")
        cols = list(rows[0].keys())
        skewed = [c for c in cols
                  if 0 < len({str(r.get(c)) for r in rows}) <= max(2, len(rows) // 10)]
        for c in skewed[:2]:
            got = _breakdown(cypher, c)
            if not got:
                continue
            rec.setdefault("breakdowns", {})[c] = got
            seen_here = len({str(r.get(c)) for r in rows})
            warn += (f"The {len(rows)} rows above show only {seen_here} "
                     f"distinct '{c}' - a few values ate the whole limit. "
                     f"Here is '{c}' over ALL {more if total else 'matching'} "
                     f"rows, which is what you must answer from:\n"
                     + "\n".join(f"    {A._fmt(g['value'])}: {g['n']}"
                                 for g in got) + "\n")
    return f"{warn}{head}:\n{body}", rec


#: How many candidates to hand back. Enough to cover a condition's spellings
#: and its near-misses, few enough that the model reads them all.
RESOLVE_LIMIT = 12


def _run_resolve(args: dict) -> tuple[str, dict]:
    """Disease nodes for a condition, with what is needed to choose between them.

    This exists because the same failure kept happening and documenting it did
    not stop it. Asked how many trials studied eczema, the model ran the
    full-text index, was handed "Dermatitis, Atopic" matched on its synonym
    "Atopic Eczema", and then queried d.name = 'Eczema' - 301 trials of 2,214.
    Asked about Heart Diseases it queried the category node itself, which holds
    1,923 of the 31,141 trials in its subtree, because a trial on heart failure
    is tagged Heart Failure and never its parent.

    Both are the same mistake: treating a condition as one node with one name.
    A rule in the prompt asks the caller to get this right; a tool makes it
    hard to get wrong, which is the difference that matters once a research
    agent nobody is watching is the caller.

    `children` is what separates the two cases. A node with 221 of them is a
    category and the question almost certainly means the subtree; a node with
    none is a leaf. `direct` is trials on that node alone, never the rollup -
    reporting a subtree total as if it were one node would be the same class
    of error in the other direction.
    """
    cond = (args.get("condition") or "").strip()
    rec = {"tool": "graph", "why": args.get("why", "") or f"resolve {cond}",
           "query": f"resolve_condition({cond!r})", "rows": [], "columns": [],
           "total": 0, "ms": 0, "error": ""}
    if not cond:
        rec["error"] = "no condition given"
        return "resolve_condition needs a condition.", rec

    cypher = """
    CALL db.index.fulltext.queryNodes('entity_names', $q) YIELD node, score
    WHERE node:Disease
    WITH node, score ORDER BY score DESC LIMIT $lim
    OPTIONAL MATCH (node)<-[:STUDIES]-(t:ClinicalTrial)
    WITH node, score, count(DISTINCT t) AS direct
    OPTIONAL MATCH (kid:Disease)-[:SUBTYPE_OF*1..3]->(node)
    RETURN node.name AS name, node.key AS key, direct,
           count(DISTINCT kid) AS children,
           left(coalesce(node.synonyms, ''), 160) AS synonyms
    ORDER BY direct DESC
    """
    try:
        rows, ms = A.run_cypher_params(
            cypher, {"q": cond, "lim": RESOLVE_LIMIT})
    except Exception as e:                                     # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        return f"ERROR: {rec['error']}", rec

    rec["ms"] = ms
    rec["total"] = len(rows)
    rec["columns"] = list(rows[0].keys()) if rows else []
    rec["rows"] = [{k: A._fmt(v) for k, v in r.items()} for r in rows]
    if not rows:
        return (f"No Disease node matches {cond!r}. Try a different wording, or "
                f"the condition may only exist as prose in the documents."), rec

    lines = [f"{len(rows)} Disease nodes match {cond!r}. Decide which belong - "
             f"a high `children` count means it is a CATEGORY and the question "
             f"probably means its whole subtree:"]
    for r in rows:
        lines.append(
            f"  {r['name']}  [{r['key']}]  direct_trials={r['direct']}  "
            f"children={r['children']}"
            + (f"  synonyms: {r['synonyms']}" if r.get("synonyms") else ""))
    lines.append("")
    lines.append("Then query with the keys you chose:")
    lines.append("  MATCH (t:ClinicalTrial)-[:STUDIES]->(d:Disease) "
                 "WHERE d.key IN ['KEY1','KEY2'] RETURN count(DISTINCT t)")
    lines.append("or roll a category up:")
    lines.append("  MATCH (d:Disease)-[:SUBTYPE_OF*0..4]->(:Disease {key:'KEY'}) "
                 "MATCH (t:ClinicalTrial)-[:STUDIES]->(d) RETURN count(DISTINCT t)")
    return "\n".join(lines), rec


def _run_docs(args: dict, k: int) -> tuple[str, dict]:
    query = args.get("query", "")
    section = args.get("section") or None
    rec = {"tool": "documents", "why": args.get("why", ""), "query": query,
           "section": section or "", "chunks": [], "total": 0, "ms": 0,
           "error": ""}
    try:
        res, ms = A.run_search(query, section, k)
    except Exception as e:                                   # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        return f"ERROR: {rec['error']}", rec

    hits = res.get("results", [])
    rec["ms"] = ms
    rec["total"] = len(hits)
    rec["chunks"] = [{
        "score": c.get("score") or c.get("rerank_score") or 0,
        "source": c.get("source", ""),
        "file": (c.get("s3_key") or "").split("/")[-1],
        "heading": c.get("heading") or c.get("section") or "",
        "text": " ".join((c.get("text") or "").split())[:1200],
    } for c in hits[:k]]

    if not hits:
        return ("0 chunks above the 0.6 relevance floor. The corpus has "
                "nothing on this wording - try the terms a label would use, "
                "or an exact product name."), rec

    out = [f"{len(hits)} chunks. Take only what answers the question:"]
    for i, c in enumerate(rec["chunks"], 1):
        agency = c["source"].split("/")[-1] or "?"
        out.append(f"\n[{i}] {agency}  score {c['score']:.3f}  {c['heading']}")
        out.append(f"    {c['text'][:1100]}")
    return "\n".join(out), rec


#: A tool call the provider emitted as TEXT instead of as a tool call. It
#: happens when the model wants another lookup and the tool has been withdrawn:
#: with nothing to call it writes the call out, and the loop - which only looks
#: at tool_calls - stored that markup as the answer. Three of five approval
#: questions came back reading "<minimax:tool_call> <invoke name=...".
_LEAKED_CALL = re.compile(
    r"<\s*(minimax:)?tool_call|<\s*invoke\s+name=|<\s*parameter\s+name=", re.I)


#: The model narrating its own plan before the answer - "Now I have enough for
#: a comprehensive comparison. Let me compile the answer." Harmless with a
#: 4-call budget because there was rarely anything to narrate; conspicuous with
#: twenty, where it thinks out loud about how much it has gathered.
#:
#: Only leading lines, and only ones that are plainly narration. A sentence in
#: the middle of an answer is prose, and a first line that merely starts with
#: "Now" may well be the answer itself.
#: Tight on purpose. The first version used "i have" and "based on the
#: results", and deleted "I have reviewed the label: it warns about
#: pneumonitis" - a real answer - down to an empty string. A cleaner that
#: silently eats answers is far worse than one that leaves a stray sentence,
#: so this matches only phrasings that cannot be part of an answer.
_PREAMBLE = re.compile(
    r"^\s*(?:now\s+)?(?:"
    r"i\s+(?:now\s+)?have\s+(?:enough|all\s+the|everything|what\s+i\s+need)|"
    r"let\s+me\s+(?:compile|write|summari[sz]e|put|assemble|now)|"
    r"here\s+is\s+the\s+(?:answer|comparison|summary)|"
    r"with\s+(?:this|these)\s+(?:results?|data)\s+i\s+can"
    r")\b[^\n]*(?:\n|$)", re.I)
#: A horizontal rule the model writes between its narration and the answer.
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*(?:\n|$)")


def _strip_preamble(text: str) -> str:
    """Drop the model's leading commentary about its own process.

    Returns the ORIGINAL if stripping would leave nothing. That guard is not
    theoretical - the first version hit it on a one-line answer.
    """
    if not text:
        return text
    out, prev = text, None
    while prev != out:
        prev = out
        out = _PREAMBLE.sub("", out, count=1)
        out = _RULE.sub("", out, count=1)
        out = out.lstrip("\n")
    out = out.strip()
    return out if out else text.strip()


def _clean_answer(text: str, messages: list, out: dict) -> str:
    """Re-ask for prose when the model wrote a tool call instead of an answer.

    Asked once, with no tools offered at all, so there is nothing to call and
    the only thing it can produce is the answer. If that still comes back as
    markup the original is returned rather than an empty string - a visible
    wrong answer can be diagnosed, a blank one cannot.
    """
    text = _strip_preamble(text)
    if not text or not _LEAKED_CALL.search(text):
        return text
    try:
        data = P._chat(
            messages + [{"role": "user", "content":
                         ("Do not call any tool. Write the answer as prose, "
                          "from the results already returned.")}],
            max_tokens=STEP_TOKENS)
    except Exception:                                          # noqa: BLE001
        return text
    out["tokens"] += data.get("usage", {}).get("total_tokens", 0)
    m = data["choices"][0]["message"]
    again = ((m.get("content") or "") or (m.get("reasoning_content") or "")).strip()
    out["repaired_answer"] = True
    return again if again and not _LEAKED_CALL.search(again) else text


def run(question: str, k: int = 6, allow: tuple[str, ...] | None = None,
        max_graph: int | None = None, max_docs: int | None = None,
        switch_on_miss: bool = False) -> dict:
    """Answer a question, deciding the lookups as it goes.

    allow/max_* exist so the same loop can be run with one store removed. That
    is how strategy.py measures whether a route is worth taking: if the
    graph-only arm says "no trials found" and the both-stores arm returns
    forty, the first arm is wrong, and no judge had to be asked.
    """
    t0 = time.time()
    out = {"question": question, "answer": "", "sources": [], "steps": [],
           "error": "", "tokens": 0, "model": A.GROQ_MODEL,
           "provider": A.PROVIDER}

    messages = [
        {"role": "system",
         "content": SYSTEM + "\n\nGRAPH SCHEMA\n"
                    + graph_schema(live_counts(A.run_cypher))},
        {"role": "user", "content": question},
    ]

    used = {"query_graph": 0, "search_documents": 0}
    limits = {"query_graph": MAX_GRAPH if max_graph is None else max_graph,
              "search_documents": MAX_DOCS if max_docs is None else max_docs}
    # resolve_condition draws on the graph budget rather than its own.
    limits["resolve_condition"] = limits["query_graph"]
    _NAME = {"graph": "query_graph", "documents": "search_documents"}
    usable = TOOLS if allow is None else [
        t for t in TOOLS
        if t["function"]["name"] in {_NAME.get(a, a) for a in allow}]
    usable_names = {t["function"]["name"] for t in usable}

    run_of = {"query_graph": 0, "search_documents": 0}
    retried_first = 0

    try:
        for step in range(MAX_STEPS):
            over = time.time() - t0 > MAX_SECONDS
            # Both sides go through _BUDGET_OF. Only the used[] side did, and
            # limits["resolve_condition"] does not exist - that tool spends
            # the graph budget - so offering it raised KeyError and cost a
            # whole question its answer.
            def _bud(t):
                n = t["function"]["name"]
                return _BUDGET_OF.get(n, n)

            offered = [] if over else [
                t for t in usable
                if used.get(_bud(t), 0) < limits[_bud(t)]]

            # After a run of calls to one store, that store is withheld for a
            # single step, so the next lookup has to go to the other one. Not
            # a ban and not a reordering - the model still chooses, it just
            # cannot spend the whole budget in one place before the other has
            # been tried once. Withholding for one step is enough: the run
            # counter resets, and it is free to go back.
            if switch_on_miss and len(offered) > 1:
                stuck = [n for n, c in run_of.items() if c >= RUN_BEFORE_SWITCH]
                if stuck:
                    kept = [t for t in offered
                            if t["function"]["name"] not in stuck]
                    if kept:                      # never withhold everything
                        offered = kept
                        # The counter is NOT cleared here. Withholding a tool
                        # from the offered list is a hint, not a guarantee -
                        # the model calls it anyway often enough - and clearing
                        # the run on the hint meant the check at dispatch saw
                        # zero and let the call through. That is why the rule
                        # never once fired on a `gggg` question.
            if not offered:
                out["steps"].append({"step": len(out["steps"]) + 1,
                                     "tool": "budget",
                                     "why": "lookups exhausted - answering now",
                                     "query": "", "total": 0, "ms": 0,
                                     "error": ""})
            # The FIRST step must call a tool. Left on "auto" the model
            # occasionally answered a question outright - "which companies
            # develop therapies against the same pathway" came back fluent,
            # sourced from nothing, with zero lookups recorded. That is the one
            # behaviour this pipeline exists to rule out, so it is forced
            # rather than requested. Later steps stay "auto" so it can stop.
            data = P._chat(
                messages, tools=offered or None,
                tool_choice=("required" if step == 0 and offered
                             else "auto" if offered else None),
                max_tokens=STEP_TOKENS)
            out["tokens"] += data.get("usage", {}).get("total_tokens", 0)
            msg = data["choices"][0]["message"]
            calls = msg.get("tool_calls") or []

            # tool_choice="required" is sent on step 0 and this provider does
            # not always honour it: it returns a PLAN instead - "Let me start
            # by querying the graph for antibody products with EMA approval" -
            # fluent, sourced in tone, with zero lookups recorded. That is the
            # one behaviour this pipeline exists to rule out. Asking again is
            # the only enforcement available from this side of the API.
            #
            # One retry was measured as enough on two questions and NOT enough
            # on a third, so it retries FIRST_RETRIES times. The later attempts
            # quote the model's own plan back at it, because a refusal that
            # already names the query it wants to run answers a pointed nudge
            # where it ignored the generic one.
            if step == 0 and offered and not calls and retried_first < FIRST_RETRIES:
                retried_first += 1
                plan = (msg.get("content") or "").strip()[:300]
                nudge = ("You did not call a tool. Do not answer from memory - "
                         "you have no knowledge of this data. Call query_graph "
                         "or search_documents now, then answer only from what "
                         "it returns.")
                if retried_first > 1 and plan:
                    nudge = (f"You wrote a plan instead of calling a tool: "
                             f"{plan!r}\n\nDo that. Emit it as a tool call now. "
                             f"Text is not a lookup and nothing you write from "
                             f"memory can be used.")
                messages.append({"role": "user", "content": nudge})
                out["forced_retry"] = True
                continue

            if not calls:
                text = (msg.get("content") or "").strip()
                if not text:
                    text = (msg.get("reasoning_content") or "").strip()
                # A step-0 refusal that survived every retry leaves a PLAN in
                # hand, not an answer. Publishing it would be the worst of both
                # - unsourced prose wearing an answer's clothes. Say what
                # happened instead.
                if step == 0 and offered:
                    out["error"] = "no tool call after %d retries" % retried_first
                    out["answer"] = (
                        "I could not answer this - the model declined to query "
                        "either store, so there is nothing sourced to report. "
                        "(What it proposed instead: %s)" % (text[:200] or "nothing"))
                    break
                out["answer"] = _clean_answer(text, messages, out)
                break

            # Keep the assistant turn intact - the tool result must reply to
            # the exact call ids, or the next turn has no context.
            messages.append({"role": "assistant", "content": msg.get("content") or "",
                             "tool_calls": calls})
            for tc in calls:
                fn = _canonical_tool(tc["function"]["name"])
                try:
                    args = P._parse_args(tc["function"]["arguments"])
                except Exception as e:                       # noqa: BLE001
                    args, result = {}, f"could not read arguments: {e}"
                    rec = {"tool": fn, "error": str(e)[:120], "query": ""}
                else:
                    other = ("search_documents" if fn == "query_graph"
                             else "query_graph")
                    budget = _BUDGET_OF.get(fn, fn)
                    if fn in limits and used.get(budget, 0) >= limits[fn]:
                        # Belt and braces: the tool was withdrawn above, so
                        # reaching here means the model called it anyway. The
                        # counter is NOT bumped - a refused call was not spent,
                        # and counting it printed "graph 5/4" on the page.
                        result = (f"No {fn} calls left. Answer from what you "
                                  f"already have.")
                        rec = {"tool": fn, "query": "", "total": 0, "ms": 0,
                               "error": "budget exhausted", "why": ""}
                    elif (switch_on_miss and budget in run_of
                          and run_of[budget] >= RUN_BEFORE_SWITCH
                          and other in usable_names
                          and used[other] < limits[other]):
                        # Withholding the tool between steps was not enough:
                        # the model emits several calls in ONE assistant turn,
                        # so a run of four graph queries can happen before the
                        # loop gets to decide anything. The rule has to live
                        # where the call is dispatched.
                        #
                        # Refused, not silently dropped - the protocol needs a
                        # reply per call id, and the model needs to know why.
                        result = (f"Not yet - that is {run_of[fn]} calls to the "
                                  f"same store in a row and the other one has "
                                  f"not been tried. Use {other} for this "
                                  f"lookup. If it has nothing either, then the "
                                  f"data really is absent and you can say so.")
                        rec = {"tool": "graph" if fn == "query_graph" else "documents",
                               "query": "", "total": 0, "ms": 0,
                               "error": "switched: same store 3x running",
                               "why": args.get("why", "")}
                        run_of[fn] = 0
                    elif fn == "query_graph":
                        used[fn] += 1
                        result, rec = _run_graph(args)
                        run_of[fn] += 1
                        run_of["search_documents"] = 0
                    elif fn == "resolve_condition":
                        # Counted against the graph budget: it is a graph read,
                        # and letting it be free would make "resolve, resolve,
                        # resolve" a way round the cap.
                        used["query_graph"] += 1
                        result, rec = _run_resolve(args)
                        run_of["query_graph"] += 1
                        run_of["search_documents"] = 0
                    elif fn == "search_documents":
                        used[fn] += 1
                        result, rec = _run_docs(args, k)
                        run_of[fn] += 1
                        run_of["query_graph"] = 0
                    else:
                        # NOT tool="graph"/"documents": a name that did not
                        # resolve is a failed call, and recording it under a
                        # store made it count as a lookup that never happened.
                        result = (f"There is no tool called {fn}. The tools "
                                  f"are query_graph and search_documents.")
                        rec = {"tool": "unknown", "query": "", "total": 0,
                               "ms": 0, "error": f"unknown tool {fn}",
                               "why": args.get("why", "")}
                rec["step"] = len(out["steps"]) + 1
                out["steps"].append(rec)
                messages.append({"role": "tool", "tool_call_id": tc.get("id"),
                                 "content": result[:12000]})
        else:
            # Ran out of steps: ask for the answer with the tools withdrawn.
            data = P._chat(messages + [{
                "role": "user",
                "content": ("You have used all your lookups. Answer now from "
                            "what they returned, and say what you could not "
                            "establish.")}], max_tokens=STEP_TOKENS)
            out["tokens"] += data.get("usage", {}).get("total_tokens", 0)
            m = data["choices"][0]["message"]
            out["answer"] = _clean_answer(
                ((m.get("content") or "")
                 or (m.get("reasoning_content") or "")).strip(), messages, out)
    except Exception as e:                                   # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:300]}"

    import re
    m = re.search(r"^SOURCES:\s*(.+)$", out["answer"], re.I | re.M)
    if m:
        out["sources"] = [x.strip() for x in re.split(r"[,;]", m.group(1))
                          if x.strip() and x.strip().lower() != "none"]
        out["answer"] = out["answer"][:m.start()].rstrip()

    out["budget"] = {"graph": f"{used['query_graph']}/{MAX_GRAPH}",
                     "documents": f"{used['search_documents']}/{MAX_DOCS}",
                     "steps": f"{len([s for s in out['steps'] if s.get('tool') != 'budget'])}/{MAX_STEPS}"}
    out["total_ms"] = int((time.time() - t0) * 1000)
    return out
