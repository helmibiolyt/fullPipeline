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
MAX_STEPS = 8
MAX_GRAPH = 4
MAX_DOCS = 4
MAX_SECONDS = 90
STEP_TOKENS = 4000

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

SYSTEM = """You answer questions about drugs, trials, regulation and safety
using two tools and nothing else.

You have NO knowledge of your own. Every fact in your answer must come from a
row or a document chunk a tool returned in this conversation. If the tools do
not have it, say so - an honest "the graph returned no trials for this" is
worth more than a fluent guess.

HOW TO WORK

Think about what the question actually needs before calling anything.

* Some questions need only the graph - "how many trials in the Gulf" has no
  document component, and searching anyway wastes a call.
* Some need only the documents - "what does the sertraline label say about
  pregnancy" is prose, not structure.
* Some need BOTH, independently - "what are the side effects of X" wants the
  reported counts from the graph and the label wording from the documents.
* Some need them IN ORDER, where the first answers the second. If the
  question is about the documents belonging to a set you have to work out
  first, query the graph, take the names it returns, and search with those.

Call one tool at a time and look at what comes back. If a query returns
nothing, that is information: try a different starting node - the full-text
index instead of an exact name, a name prefix instead of equality - before
concluding the data is absent.

Stop as soon as you can answer. You have at most 8 tool calls.

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


def _run_graph(args: dict) -> tuple[str, dict]:
    """Execute Cypher, returning (text for the model, record for the page)."""
    cypher = (args.get("cypher") or "").strip()
    rec = {"tool": "graph", "why": args.get("why", ""), "query": cypher,
           "rows": [], "columns": [], "total": 0, "ms": 0, "error": ""}

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
    warn = ""
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


def run(question: str, k: int = 6) -> dict:
    """Answer a question, deciding the lookups as it goes."""
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
    limits = {"query_graph": MAX_GRAPH, "search_documents": MAX_DOCS}

    try:
        for step in range(MAX_STEPS):
            over = time.time() - t0 > MAX_SECONDS
            offered = [] if over else [
                t for t in TOOLS
                if used[t["function"]["name"]] < limits[t["function"]["name"]]]
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

            if not calls:
                text = (msg.get("content") or "").strip()
                if not text:
                    text = (msg.get("reasoning_content") or "").strip()
                out["answer"] = text
                break

            # Keep the assistant turn intact - the tool result must reply to
            # the exact call ids, or the next turn has no context.
            messages.append({"role": "assistant", "content": msg.get("content") or "",
                             "tool_calls": calls})
            for tc in calls:
                fn = tc["function"]["name"]
                try:
                    args = P._parse_args(tc["function"]["arguments"])
                except Exception as e:                       # noqa: BLE001
                    args, result = {}, f"could not read arguments: {e}"
                    rec = {"tool": fn, "error": str(e)[:120], "query": ""}
                else:
                    if fn in limits and used[fn] >= limits[fn]:
                        # Belt and braces: the tool was withdrawn above, so
                        # reaching here means the model called it anyway. The
                        # counter is NOT bumped - a refused call was not spent,
                        # and counting it printed "graph 5/4" on the page.
                        result = (f"No {fn} calls left. Answer from what you "
                                  f"already have.")
                        rec = {"tool": fn, "query": "", "total": 0, "ms": 0,
                               "error": "budget exhausted", "why": ""}
                    elif fn == "query_graph":
                        used[fn] += 1
                        result, rec = _run_graph(args)
                    elif fn == "search_documents":
                        used[fn] += 1
                        result, rec = _run_docs(args, k)
                    else:
                        result, rec = f"unknown tool {fn}", {"tool": fn}
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
            out["answer"] = ((m.get("content") or "")
                             or (m.get("reasoning_content") or "")).strip()
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
