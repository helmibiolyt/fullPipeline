#!/usr/bin/env python3
"""Ask a question. A router LLM picks the graph or the documents, and you see
exactly what came back.

    python testPipeline/ask.py                       # interactive
    python testPipeline/ask.py "what targets EGFR"   # one shot

Built to answer one question before the research agent is written: for a given
kind of question, does the graph or the document store actually return
something worth having? So the raw result is printed, not a summary. The LLM
routes and writes the query; it never answers, and never sees the results.

Runs from a laptop against both production hosts over the public internet:
Neo4j on Azure (bolt) and the search API on AWS (HTTP).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import textwrap
import time

import requests
from dotenv import load_dotenv

# The Windows console defaults to cp1252, which cannot encode the box-drawing
# and arrow characters used below - printing one raises UnicodeEncodeError and
# kills the run before any result is shown.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
load_dotenv(HERE / ".env")



# Two providers, one shape. Both speak the OpenAI chat-completions dialect and
# both honour tool_choice="required", which is the property this pipeline is
# built on - the router must call one of the two tools and may never answer
# from its own knowledge.
#
# They differ in how they report failure: Groq uses HTTP status codes, MiniMax
# returns HTTP 200 with the real outcome in base_resp. A client that only
# checks the status code reads a MiniMax quota error as a successful empty
# reply.
PROVIDER = os.getenv("LLM_PROVIDER", "minimax").strip().lower()

PROVIDERS = {
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "key": os.getenv("GROQ_API_KEY", ""),
        "model": os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
    },
    "minimax": {
        "url": "https://api.minimax.io/v1/text/chatcompletion_v2",
        "key": os.getenv("MINIMAX_API_KEY", ""),
        "model": os.getenv("MINIMAX_MODEL", "MiniMax-M2"),
    },
}
_P = PROVIDERS.get(PROVIDER) or PROVIDERS["groq"]
GROQ_KEY = _P["key"]
GROQ_MODEL = _P["model"]
GROQ_URL = _P["url"]

# Was a hardcoded "bolt://4.233.210.24:7687". A host address baked in as
# a DEFAULT is the worst kind: replace the VM and this keeps pointing at
# the dead one, connects to nothing, and reports it as an empty graph
# rather than as a misconfiguration. localhost is the honest default -
# on the graph host it is correct, and anywhere else it fails loudly.
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")
NEO4J_DB = os.getenv("NEO4J_DATABASE", "biolyt")

VECTOR_API = os.getenv("VECTOR_API", "http://35.153.204.103:8000")

# Colours, skipped when piped to a file.
_TTY = sys.stdout.isatty()
def _c(code, s): return f"\033[{code}m{s}\033[0m" if _TTY else s
def dim(s):   return _c("2", s)
def bold(s):  return _c("1", s)
def blue(s):  return _c("38;5;75", s)
def violet(s):return _c("38;5;141", s)
def green(s): return _c("38;5;78", s)
def red(s):   return _c("38;5;203", s)
def amber(s): return _c("38;5;214", s)


# --------------------------------------------------------------------------
# Cypher safety. The model is told to write read-only queries; this is what
# happens when it does not. A router that can write is a router that can drop
# the graph, and the graph takes 32 minutes to rebuild.
FORBIDDEN = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD\s+CSV|"
    r"CALL\s*\{[^}]*\b(CREATE|MERGE|DELETE|SET)\b|"
    r"apoc\.(create|merge|refactor|trigger|periodic)|"
    r"db\.(create|index\.fulltext\.(create|drop))|"
    r"dbms\.)", re.I)


def check_cypher(q: str) -> str | None:
    """Return a refusal reason, or None if the query is safe to run."""
    m = FORBIDDEN.search(q)
    if m:
        return f"write or admin operation: {m.group(0).strip()!r}"
    if re.search(r"-\[[^\]]*\*\s*\]", q) or re.search(r"-\[[^\]]*\*\s*\.\.", q):
        return "unbounded variable-length traversal"
    # Any aggregate, not just count(). This accepted count( alone, so a query
    # grouping with collect() was refused as "not an aggregate" - and
    # collect(DISTINCT s.name) is bounded by the grouping key exactly as
    # count() is. One survived the LIMIT-appending fix in agent.py for this
    # reason: that code recognised it as an aggregate and left it alone, then
    # this check disagreed and rejected it.
    if not re.search(r"\bLIMIT\b", q, re.I) and not re.search(
            r"\b(count|collect|sum|avg|min|max)\s*\(", q, re.I):
        return "no LIMIT and not an aggregate"
    return None


# --------------------------------------------------------------------------
_driver = None


def run_cypher_params(cypher: str, params: dict,
                      _retry=True) -> tuple[list[dict], int]:
    """Run a read query with bound parameters.

    Separate from run_cypher because the model's Cypher arrives as one string
    and cannot carry parameters, while the tools this file provides build their
    own queries around user text. Binding rather than interpolating keeps a
    condition named O'Brien's disease from ending the string early, and lets
    Neo4j reuse the plan. Params is a dict rather than **kwargs so a parameter
    called `q` or `_retry` cannot collide with this function's own arguments.
    """
    global _driver
    from neo4j import GraphDatabase
    from neo4j.exceptions import ServiceUnavailable, SessionExpired
    if _driver is None:
        _driver = GraphDatabase.driver(NEO4J_URI,
                                       auth=(NEO4J_USER, NEO4J_PASSWORD))
    t0 = time.time()
    try:
        with _driver.session(database=NEO4J_DB,
                             default_access_mode="READ") as s:
            rows = [dict(r) for r in s.run(cypher, **params)]
    except (ServiceUnavailable, SessionExpired):
        if not _retry:
            raise
        try:
            _driver.close()
        except Exception:                                      # noqa: BLE001
            pass
        _driver = None
        return run_cypher_params(cypher, params, _retry=False)
    return rows, int((time.time() - t0) * 1000)


def run_cypher(q: str, _retry=True) -> tuple[list[dict], int]:
    """Run a read query, reconnecting once if the pooled connection is dead.

    The driver is held for the process lifetime, so a Neo4j restart - an
    import replaces the store and bounces the service - leaves every pooled
    connection defunct. The driver does not always recover on its own, and the
    failure surfaces as ServiceUnavailable on the next question rather than at
    the moment the server went away.
    """
    global _driver
    from neo4j import GraphDatabase
    from neo4j.exceptions import ServiceUnavailable, SessionExpired
    if _driver is None:
        _driver = GraphDatabase.driver(NEO4J_URI,
                                       auth=(NEO4J_USER, NEO4J_PASSWORD))
    t0 = time.time()
    try:
        with _driver.session(database=NEO4J_DB,
                             default_access_mode="READ") as s:
            rows = [dict(r) for r in s.run(q)]
    except (ServiceUnavailable, SessionExpired):
        if not _retry:
            raise
        try:
            _driver.close()
        except Exception:                                    # noqa: BLE001
            pass
        _driver = None
        return run_cypher(q, _retry=False)
    return rows, int((time.time() - t0) * 1000)


def run_search(query: str, section: str | None, k: int) -> tuple[dict, int]:
    payload = {"query": query, "final_k": k}
    if section:
        payload["section"] = section
    t0 = time.time()
    r = requests.post(f"{VECTOR_API}/search", json=payload, timeout=180)
    r.raise_for_status()
    return r.json(), int((time.time() - t0) * 1000)


# --------------------------------------------------------------------------
def show_graph(rows: list[dict], ms: int, limit: int):
    if not rows:
        print(amber("  no rows — the query ran and matched nothing"))
        print(dim("  usually the starting node, not the traversal. Try the "
                  "full-text index with a label filter."))
        return
    print(green(f"  {len(rows)} rows in {ms} ms"))
    print()
    cols = list(rows[0].keys())
    widths = {c: min(46, max(len(c), *(len(_fmt(r.get(c))) for r in rows)))
              for c in cols}
    head = "  ".join(bold(c.ljust(widths[c])) for c in cols)
    print("   " + head)
    print("   " + dim("─" * min(112, sum(widths.values()) + 2 * len(cols))))
    for r in rows[:limit]:
        line = "  ".join(_fmt(r.get(c))[:widths[c]].ljust(widths[c])
                         for c in cols)
        print("   " + line)
    if len(rows) > limit:
        print(dim(f"   … {len(rows) - limit} more"))


def _fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.3f}"
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)[:60]
    if isinstance(v, dict):
        return json.dumps(v)[:60]
    return str(v).replace("\n", " ")


def show_docs(res: dict, ms: int, limit: int):
    hits = res.get("results", [])
    if not hits:
        print(amber("  no chunks above the 0.6 relevance floor"))
        print(dim("  the corpus has nothing to say about this — the floor is "
                  "fixed so a weak match cannot look confident."))
        return
    print(green(f"  {len(hits)} chunks in {ms} ms"))
    for i, h in enumerate(hits[:limit], 1):
        score = h.get("score") or h.get("rerank_score") or 0
        src = h.get("source", "")
        key = (h.get("s3_key") or "").split("/")[-1]
        head = h.get("heading") or h.get("section") or ""
        print()
        print(f"  {bold(str(i))}  {violet(f'{score:.3f}')}  "
              f"{dim(src)}  {head}")
        print(dim(f"      {key[:96]}"))
        body = " ".join((h.get("text") or "").split())
        for line in textwrap.wrap(body[:520], 100):
            print(f"      {line}")


# --------------------------------------------------------------------------
def ask(question: str, limit: int, k: int, show_cypher: bool = True):
    """Ask both stores and print the synthesised answer plus its evidence."""
    import pipeline

    print()
    print(bold(f"> {question}"))
    d = pipeline.run(question, k=k)
    if d["error"]:
        print(red(f"  {d['error']}"))
        return

    print(dim(f"  planned in {d['plan_ms']} ms · answered in {d['answer_ms']} ms"
              f" · {d['tokens']} tokens · {d['model']}"))
    print()
    for line in textwrap.wrap(d["answer"], 96) or ["(no answer)"]:
        print("  " + line)
    if d["sources"]:
        print()
        print("  " + green("sources: ") + dim(", ".join(d["sources"])))

    g, dc = d["graph"], d["docs"]
    print()
    print(blue(f"  GRAPH  {g.get('total', 0)} rows in {g.get('ms', 0)} ms")
          + dim(f"   {' '.join(d['cypher'].split())[:88]}"))
    if g.get("error"):
        print(red(f"    {g['error']}"))
    for r in g.get("rows", [])[:limit]:
        print(dim("    " + " · ".join(f"{v}" for v in r.values())[:100]))

    print(violet(f"  DOCS   {dc.get('total', 0)} chunks in {dc.get('ms', 0)} ms")
          + dim(f"   \"{d['document_query']}\""))
    if dc.get("error"):
        print(red(f"    {dc['error']}"))
    for c in dc.get("chunks", [])[:limit]:
        src = c["source"].split("/")[-1]
        print(dim(f"    {c['score']:.3f}  {src}  {c['heading'][:40]}"))


BANNER = f"""{bold('biolyt · test pipeline')}
{dim('Every question queries BOTH the knowledge graph and the document store.')}
{dim('The answer is written only from what they return, with its sources.')}

  {blue('graph')}      {NEO4J_URI}  db={NEO4J_DB}
  {violet('documents')}  {VECTOR_API}
  {dim('model')}      {GROQ_MODEL}  {dim(f'via {PROVIDER}')}

{dim('type a question, or  :q  to quit')}"""

EXAMPLES = [
    "which drugs target EGFR",
    "what are the contraindications of sertraline",
    "what are the side effects of atorvastatin",
    "how many trials are running in the MENA region",
    "what does the label say about ibuprofen in pregnancy",
    "what adverse events are reported for pembrolizumab",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="*", help="ask once and exit")
    ap.add_argument("--limit", type=int, default=8,
                    help="evidence rows or chunks to list")
    ap.add_argument("-k", type=int, default=8, help="chunks to retrieve")
    ap.add_argument("--examples", action="store_true",
                    help="run the built-in example questions")
    a = ap.parse_args()

    if not NEO4J_PASSWORD:
        sys.exit("NEO4J_PASSWORD missing - copy testPipeline/.env.example "
                 "to testPipeline/.env and fill it in")

    if a.examples:
        for q in EXAMPLES:
            ask(q, a.limit, a.k)
        return
    if a.question:
        ask(" ".join(a.question), a.limit, a.k)
        return

    print(BANNER)
    while True:
        try:
            print()
            q = input(bold("> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if q in (":q", "quit", "exit"):
            break
        if q:
            ask(q, a.limit, a.k)


if __name__ == "__main__":
    main()
