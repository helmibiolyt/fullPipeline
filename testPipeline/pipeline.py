#!/usr/bin/env python3
"""Ask both stores, then answer from what they returned.

Three steps, and the middle one is the point:

    plan     one LLM call writes a Cypher query AND a document search
    gather   both run in parallel against the live stores
    answer   a second LLM call writes the answer from those results only

The model never answers from its own knowledge. Stage 1 is forced to emit a
tool call, so it cannot reply with prose; stage 2 is given nothing but the two
result sets. Every sentence therefore traces back to a row or a document, and
when the stores return nothing the answer says so instead of inventing.

Used by both the page (serve.py) and the CLI (ask.py).
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import re
import time

import ask as A
from schema_prompt import (ANSWER_SYSTEM, PLAN_TOOL,  # noqa: F401
                           PLANNER_SYSTEM, graph_schema, live_counts)


# A reasoning model spends tokens thinking before it emits anything, and that
# spend comes out of the same budget as the output. Too low a ceiling
# truncates the tool call rather than shortening it.
PLAN_TOKENS = 6000
ANSWER_TOKENS = 2500


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


def plan(question: str) -> tuple[dict, int, int]:
    """Both queries, in one forced tool call."""
    t0 = time.time()
    data = _chat(
        [{"role": "system",
          "content": PLANNER_SYSTEM + "\n\nGRAPH SCHEMA\n"
                     + graph_schema(live_counts(A.run_cypher))},
         {"role": "user", "content": question}],
        tools=PLAN_TOOL, max_tokens=PLAN_TOKENS)
    msg = data["choices"][0]["message"]
    calls = msg.get("tool_calls") or []
    if not calls:
        raise RuntimeError("the planner answered instead of writing queries: "
                           + (msg.get("content") or "")[:200])
    args = _parse_args(calls[0]["function"]["arguments"])
    if not args.get("document_query"):
        # Optional to recover: the question itself is a serviceable search.
        args["document_query"] = question
    return (args, int((time.time() - t0) * 1000),
            data.get("usage", {}).get("total_tokens", 0))


def gather(cypher: str, doc_query: str, section: str | None, k: int = 8):
    """Run both stores at once. Neither failure stops the other.

    In parallel because they are independent and on different continents -
    Neo4j in Azure, the search API in AWS - so the wall clock is the slower of
    the two rather than their sum.
    """
    graph = {"rows": [], "ms": 0, "error": "", "cypher": cypher}
    docs = {"chunks": [], "ms": 0, "error": "", "query": doc_query}

    def _graph():
        bad = A.check_cypher(cypher)
        if bad:
            graph["error"] = f"refused: {bad}"
            return
        try:
            rows, ms = A.run_cypher(cypher)
            graph["rows"], graph["ms"] = rows, ms
        except Exception as e:                               # noqa: BLE001
            graph["error"] = f"{type(e).__name__}: {str(e)[:400]}"
            graph["failed_cypher"] = cypher

    def _docs():
        try:
            res, ms = A.run_search(doc_query, section, k)
            docs["chunks"], docs["ms"] = res.get("results", []), ms
        except Exception as e:                               # noqa: BLE001
            docs["error"] = f"{type(e).__name__}: {str(e)[:200]}"

    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(lambda f: f(), (_graph, _docs)))
    return graph, docs


# Errors the model can actually fix by rewriting. A connection failure or a
# timeout is not one of them - retrying those just wastes a model call.
_FIXABLE = ("SyntaxError", "TypeError", "ArgumentError", "ParameterMissing",
            "InvalidArgument", "ProcedureCallFailed", "SemanticError")


def gather_repaired(question: str, cypher: str, doc_query: str,
                    section: str | None, k: int = 8):
    """gather(), with one repair round if Neo4j rejects the query.

    Neo4j's error is far more useful than any instruction in the prompt: it
    names the variable, the line and the column. Feeding it back fixes the
    kind of mistake no amount of prompting prevents - here, a second
    `WITH node LIMIT 1` that dropped the collection built by the first half of
    the query, because WITH resets scope to exactly what it lists.

    Only the graph half is retried; the document search has already returned.
    """
    graph, docs = gather(cypher, doc_query, section, k)
    err = graph.get("error", "")
    if not err or not any(k_ in err for k_ in _FIXABLE):
        return graph, docs

    try:
        data = _chat(
            [{"role": "system",
              "content": PLANNER_SYSTEM + "\n\nGRAPH SCHEMA\n"
                         + graph_schema(live_counts(A.run_cypher))},
             {"role": "user", "content": question},
             {"role": "assistant", "content": "cypher:\n" + cypher},
             {"role": "user",
              "content": "Neo4j rejected that query:\n\n" + err +
                         "\n\nRewrite it so it runs. Remember that WITH "
                         "resets scope - anything you want to keep must be "
                         "listed in every WITH that follows it. Keep the same "
                         "document_query."}],
            tools=PLAN_TOOL, max_tokens=PLAN_TOKENS)
        calls = data["choices"][0]["message"].get("tool_calls") or []
        if not calls:
            return graph, docs
        fixed = _parse_args(calls[0]["function"]["arguments"])
        new_cypher = (fixed.get("cypher") or "").strip()
        if not new_cypher or new_cypher == cypher:
            return graph, docs
        if check_directions(new_cypher):
            return graph, docs
        bad = A.check_cypher(new_cypher)
        if bad:
            return graph, docs
        rows, ms = A.run_cypher(new_cypher)
        graph.update({"rows": rows, "ms": ms, "error": "",
                      "cypher": new_cypher, "repaired": True})
    except Exception:                                        # noqa: BLE001
        # The original error is the honest thing to report; a failed repair
        # should not replace it with a confusing second one.
        pass
    return graph, docs


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


def _evidence(graph: dict, docs: dict, max_rows=EVIDENCE_ROWS,
              max_chunks=6) -> str:
    """What the answering model is allowed to see."""
    out = ["=== KNOWLEDGE GRAPH ===",
           f"query: {' '.join(graph['cypher'].split())}"]
    if graph["error"]:
        out.append(f"ERROR: {graph['error']}")
    elif not graph["rows"]:
        out.append("no rows - the query ran and matched nothing")
    else:
        n = len(graph["rows"])
        shown = min(n, max_rows)
        # State the true total separately from the sample, so the model
        # reports the count it was given rather than counting the lines it
        # can see.
        head = f"{n} rows matched"
        if n > shown:
            head += (f"; the first {shown} are listed below - when you give a "
                     f"total, use {n}, not the number of lines here")
        if _hit_limit(graph["cypher"], n):
            head += (f". NOTE: that equals the query's LIMIT, so the real "
                     f"total may be higher - say 'at least {n}'")
        out.append(head + ":")
        for r in graph["rows"][:max_rows]:
            out.append("  " + json.dumps(
                {k: A._fmt(v) for k, v in r.items()}, ensure_ascii=False))

    out += ["", "=== REGULATORY DOCUMENTS ===",
            f"search: {docs['query']}"]
    if docs["error"]:
        out.append(f"ERROR: {docs['error']}")
    elif not docs["chunks"]:
        out.append("no chunks above the 0.6 relevance floor")
    else:
        out.append(f"{len(docs['chunks'])} chunks retrieved by similarity. "
                   "Work through them one at a time and take only what "
                   "answers the question:")
        for i, c in enumerate(docs["chunks"][:max_chunks], 1):
            agency = (c.get("source", "").split("/")[-1] or "?")
            score = c.get("score") or c.get("rerank_score") or 0
            head = c.get("heading") or c.get("section") or ""
            body = " ".join((c.get("text") or "").split())[:1100]
            out.append(f"\n[{i}] {agency}  score {score:.3f}  {head}")
            out.append(f"    {body}")
    return "\n".join(out)


def _hit_limit(cypher: str, n: int) -> bool:
    """Did the result stop because it ran out of matches, or hit the LIMIT?

    A query returning exactly its limit has almost certainly been truncated,
    and the difference matters: "25 drugs target EGFR" and "at least 25" are
    different claims.
    """
    m = re.findall(r"LIMIT\s+(\d+)", cypher, re.I)
    return bool(m) and n == int(m[-1])


def answer(question: str, graph: dict, docs: dict) -> tuple[str, list, int, int]:
    """Write the answer from the two result sets, and nothing else."""
    t0 = time.time()
    data = _chat([
        {"role": "system", "content": ANSWER_SYSTEM},
        {"role": "user",
         "content": f"QUESTION: {question}\n\n{_evidence(graph, docs)}"},
    ], max_tokens=ANSWER_TOKENS)
    msg = data["choices"][0]["message"]
    text = (msg.get("content") or "").strip()
    # MiniMax-M2 is a reasoning model: with no tool call to make it commit,
    # the prose sometimes arrives in reasoning_content instead of content.
    if not text:
        text = (msg.get("reasoning_content") or "").strip()

    sources = []
    m = re.search(r"^SOURCES:\s*(.+)$", text, re.I | re.M)
    if m:
        sources = [x.strip() for x in re.split(r"[,;]", m.group(1))
                   if x.strip() and x.strip().lower() != "none"]
        text = text[:m.start()].rstrip()
    return (text, sources, int((time.time() - t0) * 1000),
            data.get("usage", {}).get("total_tokens", 0))


def run(question: str, k: int = 8) -> dict:
    """The whole thing, as one dict the page or the CLI can render."""
    t0 = time.time()
    out = {"question": question, "answer": "", "sources": [],
           "cypher": "", "document_query": "", "section": "",
           "graph": {}, "docs": {}, "error": "",
           "plan_ms": 0, "answer_ms": 0, "tokens": 0,
           "model": A.GROQ_MODEL, "provider": A.PROVIDER}
    try:
        args, pms, ptok = plan_checked(question)
        out["plan_ms"] = pms
        out["tokens"] += ptok
        out["cypher"] = (args.get("cypher") or "").strip()
        out["document_query"] = args.get("document_query", "")
        out["section"] = args.get("section") or ""

        graph, docs = gather_repaired(question, out["cypher"],
                                      out["document_query"],
                                      out["section"] or None, k)
        out["truncated"] = _hit_limit(out["cypher"], len(graph["rows"]))
        out["graph"] = {"rows": [{k2: A._fmt(v) for k2, v in r.items()}
                                 for r in (graph["rows"] if PAGE_ROWS is None
                                           else graph["rows"][:PAGE_ROWS])],
                        "columns": list(graph["rows"][0].keys())
                        if graph["rows"] else [],
                        "total": len(graph["rows"]),
                        "ms": graph["ms"], "error": graph["error"]}
        out["docs"] = {"chunks": [{
            "score": c.get("score") or c.get("rerank_score") or 0,
            "source": c.get("source", ""),
            "file": (c.get("s3_key") or "").split("/")[-1],
            "heading": c.get("heading") or c.get("section") or "",
            "text": " ".join((c.get("text") or "").split())[:1200],
        } for c in docs["chunks"][:k]],
            "total": len(docs["chunks"]),
            "ms": docs["ms"], "error": docs["error"]}

        text, srcs, ams, atok = answer(question, graph, docs)
        out["answer"], out["sources"] = text, srcs
        out["answer_ms"] = ams
        out["tokens"] += atok
    except Exception as e:                                   # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:300]}"
    out["total_ms"] = int((time.time() - t0) * 1000)
    return out


def run_streamed(question: str, k: int = 8):
    """The same three stages, yielding each as it completes.

    The whole thing takes 15-25 seconds and most of that is the two model
    calls. Waiting on a single response means staring at a spinner with no
    idea whether anything is happening; yielding after the plan, and again
    after the stores answer, shows the queries and the evidence within a few
    seconds and leaves only the prose outstanding.
    """
    t0 = time.time()
    try:
        args, pms, ptok = plan_checked(question)
    except Exception as e:                                   # noqa: BLE001
        yield {"stage": "error", "error": f"{type(e).__name__}: {str(e)[:300]}"}
        return

    cypher = (args.get("cypher") or "").strip()
    dq = args.get("document_query", "")
    section = args.get("section") or ""
    yield {"stage": "plan", "cypher": cypher, "document_query": dq,
           "section": section, "plan_ms": pms, "tokens": ptok}

    graph, docs = gather_repaired(question, cypher, dq,
                                  section or None, k)
    yield {"stage": "evidence",
           "graph": {"rows": [{k2: A._fmt(v) for k2, v in r.items()}
                              for r in (graph["rows"] if PAGE_ROWS is None
                                           else graph["rows"][:PAGE_ROWS])],
                     "columns": list(graph["rows"][0].keys())
                     if graph["rows"] else [],
                     "total": len(graph["rows"]), "ms": graph["ms"],
                     "error": graph["error"]},
           "docs": {"chunks": [{
               "score": c.get("score") or c.get("rerank_score") or 0,
               "source": c.get("source", ""),
               "file": (c.get("s3_key") or "").split("/")[-1],
               "heading": c.get("heading") or c.get("section") or "",
               "text": " ".join((c.get("text") or "").split())[:1200],
           } for c in docs["chunks"][:k]],
               "total": len(docs["chunks"]), "ms": docs["ms"],
               "error": docs["error"]}}

    try:
        text, srcs, ams, atok = answer(question, graph, docs)
    except Exception as e:                                   # noqa: BLE001
        yield {"stage": "error", "error": f"{type(e).__name__}: {str(e)[:300]}"}
        return
    yield {"stage": "answer", "answer": text, "sources": srcs,
           "answer_ms": ams, "tokens": ptok + atok,
           "total_ms": int((time.time() - t0) * 1000)}


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


def plan_checked(question: str) -> tuple[dict, int, int]:
    """plan(), with one repair attempt if the Cypher points the wrong way."""
    args, ms, tok = plan(question)
    problems = check_directions(args.get("cypher", ""))
    if not problems:
        return args, ms, tok

    t0 = time.time()
    data = _chat(
        [{"role": "system",
          "content": PLANNER_SYSTEM + "\n\nGRAPH SCHEMA\n"
                     + graph_schema(live_counts(A.run_cypher))},
         {"role": "user", "content": question},
         {"role": "assistant",
          "content": "cypher:\n" + args.get("cypher", "")},
         {"role": "user",
          "content": "That query will match nothing. " + " ".join(problems)
                     + " Rewrite both queries with every arrow the way the "
                       "schema states. Remember a Substance is approved when "
                       "EXISTS { (:Product)-[:CONTAINS]->(s) } - there is no "
                       "path from Substance to Approval."}],
        tools=PLAN_TOOL, max_tokens=PLAN_TOKENS)
    calls = data["choices"][0]["message"].get("tool_calls") or []
    if calls:
        fixed = _parse_args(calls[0]["function"]["arguments"])
        if fixed.get("cypher") and not check_directions(fixed["cypher"]):
            fixed.setdefault("document_query", args.get("document_query", ""))
            fixed.setdefault("section", args.get("section", ""))
            return (fixed, ms + int((time.time() - t0) * 1000),
                    tok + data.get("usage", {}).get("total_tokens", 0))
    return args, ms + int((time.time() - t0) * 1000), tok
