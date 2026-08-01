#!/usr/bin/env python3
"""Shared helpers for talking to the two stores.

This was the pipeline: plan a Cypher query and a document search in one LLM
call, run both in parallel, answer from the results. agent.py replaced it with
a loop, and the loop measured better - 0 false denials against the fixed plan's
2, and roughly double the evidence on the same budget (testPipeline/ROUTING.md).
Fixed-parallel lost worst of all: 387 rows of evidence against the agent's
2,477. So plan/gather/answer, the streaming variant and the evidence formatter
are gone rather than kept warm.

What is left is the part that was never about the plan:

    _chat             one call to whichever provider is configured
    _parse_args       tool-call JSON, repaired when the model truncates it
    check_directions  a reversed arrow, caught BEFORE the query runs - it
                      returns zero rows and no error, which reads as absence
    _hit_limit        did this result stop at its LIMIT
    _MEMORY_ADVICE    what to tell the model when Neo4j runs out of heap

Imported by agent.py and ab_prompt.py.
"""
from __future__ import annotations

import json
import re

import ask as A
from schema_prompt import PLAN_TOOL, PLANNER_SYSTEM  # noqa: F401


# A reasoning model spends tokens thinking before it emits anything, and that
# spend comes out of the same budget as the output. Too low a ceiling
# truncates the tool call rather than shortening it.
PLAN_TOKENS = 6000


def _chat(messages, tools=None, tool_choice=None, max_tokens=2500):
    """One call to whichever provider is configured."""
    body = {"model": A.GROQ_MODEL, "messages": messages,
            "temperature": 0.1, "max_tokens": max_tokens}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = tool_choice or "required"

    import requests
    r = requests.post(A.GROQ_URL, json=body, timeout=120,
                      headers={"Authorization": f"Bearer {A.GROQ_KEY}"})
    if r.status_code != 200:
        raise RuntimeError(f"{A.PROVIDER} {r.status_code}: {r.text[:300]}")
    data = r.json()
    # MiniMax reports refusals inside a 200 response.
    br = data.get("base_resp") or {}
    if br.get("status_code"):
        raise RuntimeError(f"{A.PROVIDER}: {br.get('status_msg')}")
    return data


def _parse_args(raw: str) -> dict:
    """Tool arguments, repaired if the model ran out of tokens mid-string.

    MiniMax-M2 reasons before it answers, and that reasoning is charged
    against the same budget as the tool call. Hit the ceiling and the
    arguments arrive as truncated JSON - {"cypher": "MATCH (s:Sub - which
    fails with "Unterminated string". A bigger budget is the real fix; this
    salvages the run when it happens anyway, because a slightly short Cypher
    query is still worth running and a crash is not.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    patched = raw
    if patched.count('"') % 2:
        patched += '"'
    patched += "}" * max(0, patched.count("{") - patched.count("}"))
    try:
        return json.loads(patched)
    except json.JSONDecodeError:
        pass
    out = {}
    for field in ("cypher", "document_query", "section"):
        m = re.search(r'"' + field + r'"\s*:\s*"((?:[^"\\]|\\.)*)', raw)
        if m:
            out[field] = m.group(1).replace('\\"', '"').replace("\\n", " ")
    if not out.get("cypher") and not out.get("document_query"):
        raise RuntimeError(
            f"the planner's tool call was truncated beyond repair "
            f"({len(raw)} chars): {raw[:160]}")
    return out








# Errors the model can actually fix by rewriting. A connection failure is not
# one of them - retrying that just wastes a model call.
#
# Running out of transaction memory IS fixable, and is usually shape rather
# than scale: joining three relationships before aggregating expands to
# hundreds of millions of paths, where aggregating each step first answers the
# same question in a second. Measured on the query that prompted this - the
# unanchored form exhausted 512 MiB, the aggregate-early form returned 2,188
# rows in 1.2 seconds. So the limit is not raised; a query that needs more
# than half a gigabyte of intermediate state is asking the wrong way.

_MEMORY_ADVICE = """
That query ran out of transaction memory. It is almost never too much data -
it is the shape. Chaining several MATCH clauses before any aggregation builds
every combination in memory first.

Aggregate at each step instead, so the row count falls before the next join:

    MATCH (p:Product)-[:HAS_APPROVAL]->(:Approval)
    WITH p, count(*) AS approvals
    MATCH (p)-[:CONTAINS]->(s:Substance)
    WITH s, sum(approvals) AS approvals
    ORDER BY approvals DESC LIMIT 25
    MATCH (s)-[:TESTED_IN]->(t:ClinicalTrial)
    RETURN s.name, approvals, t.title LIMIT 5000

Also: put the ORDER BY and LIMIT on the aggregate, not at the end - narrowing
to the top 25 substances before expanding to their trials is what keeps this
small. And anchor on something specific wherever the question allows it."""




# How many rows the ANSWERING model sees. Separate from the query's LIMIT on
# purpose: the query should fetch everything that matches so the count is
# true, while the model only needs enough rows to name examples. Conflating
# the two is what made every answer say "25".
# Three separate caps, and only one of them may lose data.
#
#   query LIMIT   fetches from Neo4j. This is the only one that can hide a
#                 row, so it is set high and a result that exactly equals it
#                 is reported as "at least N" rather than as a total.
#   EVIDENCE_ROWS how many rows the ANSWERING model reads. It is given the
#                 true total separately, so a sample here cannot change the
#                 count it reports - only which examples it can name.
#   page          none. Everything fetched is shown, however long the table.
EVIDENCE_ROWS = 40
PAGE_ROWS = None          # no cap - show every row that came back




def _hit_limit(cypher: str, n: int) -> bool:
    """Did the result stop because it ran out of matches, or hit the LIMIT?

    A query returning exactly its limit has almost certainly been truncated,
    and the difference matters: "25 drugs target EGFR" and "at least 25" are
    different claims.
    """
    m = re.findall(r"LIMIT\s+(\d+)", cypher, re.I)
    return bool(m) and n == int(m[-1])








# --------------------------------------------------------------------------
# Direction checking.
#
# Telling the model which way each arrow points does not reliably work. It
# wrote (:Product)<-[:HAS_APPROVAL]-(:Approval) with the arrow reversed, which
# matches nothing and raises nothing - the answer then says "the graph has no
# such data", which is false and unfalsifiable from the outside.
#
# So the direction is checked against the schema before the query runs. Only
# patterns where BOTH ends carry a label can be checked; anything else is left
# alone rather than guessed at.
# Wrapped in a lookahead so matches may OVERLAP. A chained path,
# (a)-[r1]-(b)-[r2]-(c), shares node (b) between the two links; a consuming
# match eats it and never sees the second relationship - which is exactly
# where the reversed HAS_APPROVAL was hiding.
_PAT = re.compile(
    r"(?=("
    r"\(\s*\w*\s*:\s*(\w+)[^)]*\)\s*(<-|-)\s*\[\s*:?\s*(\w+)[^\]]*\]\s*(->|-)\s*"
    r"\(\s*\w*\s*:\s*(\w+)[^)]*\)"
    r"))")


def _allowed(spec: str) -> set[str]:
    return {x.strip() for x in spec.replace("|", " ").split() if x.strip()}


def check_directions(cypher: str) -> list[str]:
    """Reversed or impossible relationships, as human-readable complaints."""
    from make_tech_doc import EDGES

    problems = []
    for _whole, left_label, larrow, rel, rarrow, right_label in             _PAT.findall(cypher):
        spec = EDGES.get(rel)
        if not spec:
            continue
        src_ok, dst_ok = _allowed(spec[0]), _allowed(spec[1])
        if src_ok == {"any"}:
            continue
        # Which node is the source depends on which side the arrowhead is on.
        if larrow == "<-":
            src, dst = right_label, left_label
        elif rarrow == "->":
            src, dst = left_label, right_label
        else:
            continue                      # undirected, nothing to check
        if src in src_ok and (dst in dst_ok or dst_ok == {"any"}):
            continue
        if dst in src_ok and src in dst_ok:
            problems.append(
                f"{rel} is written backwards: it runs "
                f"(:{spec[0]})-[:{rel}]->(:{spec[1]}), "
                f"you wrote it from {src} to {dst}")
        else:
            problems.append(
                f"{rel} cannot connect {src} to {dst}: it runs "
                f"(:{spec[0]})-[:{rel}]->(:{spec[1]})")
    return problems


