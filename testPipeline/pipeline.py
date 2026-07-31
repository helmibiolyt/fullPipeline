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


def _chat(messages, tools=None, tool_choice=None, max_tokens=2000):
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


def plan(question: str) -> tuple[dict, int, int]:
    """Both queries, in one forced tool call."""
    t0 = time.time()
    data = _chat(
        [{"role": "system",
          "content": PLANNER_SYSTEM + "\n\nGRAPH SCHEMA\n"
                     + graph_schema(live_counts(A.run_cypher))},
         {"role": "user", "content": question}],
        tools=PLAN_TOOL)
    msg = data["choices"][0]["message"]
    calls = msg.get("tool_calls") or []
    if not calls:
        raise RuntimeError("the planner answered instead of writing queries: "
                           + (msg.get("content") or "")[:200])
    args = json.loads(calls[0]["function"]["arguments"])
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
            graph["error"] = f"{type(e).__name__}: {str(e)[:200]}"

    def _docs():
        try:
            res, ms = A.run_search(doc_query, section, k)
            docs["chunks"], docs["ms"] = res.get("results", []), ms
        except Exception as e:                               # noqa: BLE001
            docs["error"] = f"{type(e).__name__}: {str(e)[:200]}"

    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(lambda f: f(), (_graph, _docs)))
    return graph, docs


def _evidence(graph: dict, docs: dict, max_rows=25, max_chunks=6) -> str:
    """What the answering model is allowed to see."""
    out = ["=== KNOWLEDGE GRAPH ===",
           f"query: {' '.join(graph['cypher'].split())}"]
    if graph["error"]:
        out.append(f"ERROR: {graph['error']}")
    elif not graph["rows"]:
        out.append("no rows - the query ran and matched nothing")
    else:
        out.append(f"{len(graph['rows'])} rows:")
        for r in graph["rows"][:max_rows]:
            out.append("  " + json.dumps(
                {k: A._fmt(v) for k, v in r.items()}, ensure_ascii=False))
        if len(graph["rows"]) > max_rows:
            out.append(f"  ... {len(graph['rows']) - max_rows} more")

    out += ["", "=== REGULATORY DOCUMENTS ===",
            f"search: {docs['query']}"]
    if docs["error"]:
        out.append(f"ERROR: {docs['error']}")
    elif not docs["chunks"]:
        out.append("no chunks above the 0.6 relevance floor")
    else:
        for i, c in enumerate(docs["chunks"][:max_chunks], 1):
            agency = (c.get("source", "").split("/")[-1] or "?")
            score = c.get("score") or c.get("rerank_score") or 0
            head = c.get("heading") or c.get("section") or ""
            body = " ".join((c.get("text") or "").split())[:1100]
            out.append(f"\n[{i}] {agency}  score {score:.3f}  {head}")
            out.append(f"    {body}")
    return "\n".join(out)


def answer(question: str, graph: dict, docs: dict) -> tuple[str, list, int, int]:
    """Write the answer from the two result sets, and nothing else."""
    t0 = time.time()
    data = _chat([
        {"role": "system", "content": ANSWER_SYSTEM},
        {"role": "user",
         "content": f"QUESTION: {question}\n\n{_evidence(graph, docs)}"},
    ], max_tokens=1200)
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
        args, pms, ptok = plan(question)
        out["plan_ms"] = pms
        out["tokens"] += ptok
        out["cypher"] = (args.get("cypher") or "").strip()
        out["document_query"] = args.get("document_query", "")
        out["section"] = args.get("section") or ""

        graph, docs = gather(out["cypher"], out["document_query"],
                             out["section"] or None, k)
        out["graph"] = {"rows": [{k2: A._fmt(v) for k2, v in r.items()}
                                 for r in graph["rows"][:25]],
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
