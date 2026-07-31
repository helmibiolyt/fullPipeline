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

from schema_prompt import (ROUTER_SYSTEM, TOOLS, graph_schema,  # noqa: E402
                           live_counts)

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

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://4.233.210.24:7687")
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
    if not re.search(r"\bLIMIT\b", q, re.I) and not re.search(
            r"\bcount\s*\(", q, re.I):
        return "no LIMIT and not an aggregate"
    return None


# --------------------------------------------------------------------------
def route(question: str, verbose=False) -> dict:
    """Ask Groq which of the two stores to use, and for the query to run."""
    if not GROQ_KEY:
        sys.exit(f"no API key for provider {PROVIDER!r} - set "
                 f"{'MINIMAX_API_KEY' if PROVIDER == 'minimax' else 'GROQ_API_KEY'} "
                 f"in testPipeline/.env")

    body = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system",
             "content": ROUTER_SYSTEM + "\n\nGRAPH SCHEMA\n"
                        + graph_schema(live_counts(run_cypher))},
            {"role": "user", "content": question},
        ],
        "tools": TOOLS,
        # required, not auto: the model must call one of the two. Without this
        # it will happily answer from its own knowledge, which is the one
        # thing this pipeline exists to prevent.
        "tool_choice": "required",
        "temperature": 0.1,
    }
    t0 = time.time()
    # The free tier allows 12,000 tokens per minute and this prompt is ~2,800,
    # so about four questions a minute. Groq states the exact wait it wants in
    # the error body; honour that rather than guessing, and retry rather than
    # dying halfway through a probe run.
    for attempt in range(6):
        r = requests.post(GROQ_URL, json=body, timeout=90,
                          headers={"Authorization": f"Bearer {GROQ_KEY}"})
        if r.status_code != 429:
            break
        wait = 8.0
        try:
            msg = r.json()["error"]["message"]
            m = re.search(r"try again in ([\d.]+)s", msg)
            if m:
                wait = float(m.group(1)) + 0.6
        except Exception:                                    # noqa: BLE001
            pass
        wait = min(wait * (1 + attempt * 0.4), 45)
        if verbose:
            print(dim(f"  rate limited, waiting {wait:.1f}s"))
        time.sleep(wait)
    if r.status_code != 200:
        sys.exit(f"{PROVIDER} {r.status_code}: {r.text[:400]}")
    data = r.json()
    # MiniMax answers 200 even when it refuses: insufficient balance, an
    # invalid key and a rate limit all arrive here rather than as a status.
    br = data.get("base_resp") or {}
    if br.get("status_code"):
        sys.exit(f"{PROVIDER}: {br.get('status_msg')} "
                 f"(code {br['status_code']})")
    msg = data["choices"][0]["message"]
    calls = msg.get("tool_calls") or []
    if not calls:
        # Should be impossible with tool_choice=required, but a model that
        # talks instead of routing is exactly the failure worth surfacing.
        return {"error": "the model answered instead of routing",
                "said": (msg.get("content") or "")[:400]}

    call = calls[0]["function"]
    try:
        args = json.loads(call["arguments"])
    except json.JSONDecodeError as e:
        return {"error": f"unparseable tool arguments: {e}",
                "said": call["arguments"][:400]}
    usage = data.get("usage", {})
    return {"tool": call["name"], "args": args,
            "ms": int((time.time() - t0) * 1000),
            "tokens": usage.get("total_tokens", 0)}


# --------------------------------------------------------------------------
_driver = None


def run_cypher(q: str) -> tuple[list[dict], int]:
    global _driver
    from neo4j import GraphDatabase
    if _driver is None:
        _driver = GraphDatabase.driver(NEO4J_URI,
                                       auth=(NEO4J_USER, NEO4J_PASSWORD))
    t0 = time.time()
    with _driver.session(database=NEO4J_DB,
                         default_access_mode="READ") as s:
        rows = [dict(r) for r in s.run(q)]
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
    print()
    print(bold(f"❯ {question}"))

    r = route(question)
    if "error" in r:
        print(red(f"  router failed: {r['error']}"))
        if r.get("said"):
            print(dim(f"  it said: {r['said'][:200]}"))
        return

    tool, args = r["tool"], r["args"]
    tag = (blue("GRAPH") if tool == "query_graph" else violet("DOCUMENTS"))
    print(f"  {tag}  {dim(args.get('why', ''))}")
    print(dim(f"  routed in {r['ms']} ms · {r['tokens']} tokens · "
              f"{GROQ_MODEL}"))

    try:
        if tool == "query_graph":
            q = args.get("cypher", "").strip()
            bad = check_cypher(q)
            if bad:
                print(red(f"  REFUSED — {bad}"))
                print(dim("  " + q[:300]))
                return
            if show_cypher:
                print()
                for line in q.splitlines():
                    print(dim("  │ ") + line)
            print()
            rows, ms = run_cypher(q)
            show_graph(rows, ms, limit)
        else:
            q = args.get("query", "")
            sec = args.get("section")
            print(dim(f'  │ search "{q}"' + (f"  section={sec}" if sec else "")))
            res, ms = run_search(q, sec, k)
            show_docs(res, ms, limit)
    except Exception as e:                                   # noqa: BLE001
        print(red(f"  {type(e).__name__}: {str(e)[:300]}"))


BANNER = f"""{bold('biolyt · test pipeline')}
{dim('The LLM only ever does one of two things: query the graph, or search the')}
{dim('documents. It never answers. You see the raw result of its choice.')}

  {blue('graph')}      {NEO4J_URI}  db={NEO4J_DB}
  {violet('documents')}  {VECTOR_API}
  {dim('router')}     {GROQ_MODEL}

{dim('type a question, or  :q  to quit')}"""

EXAMPLES = [
    "which drugs target EGFR",
    "what are the contraindications of sertraline",
    "how many clinical trials are running in Saudi Arabia",
    "what does the label say about taking ibuprofen in pregnancy",
    "what adverse events are reported for pembrolizumab",
    "which companies sponsor the most trials for NSCLC",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="*", help="ask once and exit")
    ap.add_argument("--limit", type=int, default=12,
                    help="rows or chunks to display")
    ap.add_argument("-k", type=int, default=6, help="chunks to retrieve")
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
            q = input(f"\n{bold('›')} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if q in (":q", "quit", "exit"):
            break
        if q:
            ask(q, a.limit, a.k)


if __name__ == "__main__":
    main()
