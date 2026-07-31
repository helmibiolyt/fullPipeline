#!/usr/bin/env python3
"""Compare two planner prompts on the same questions, by what they return.

    python testPipeline/ab_prompt.py

MINIMAL is the schema and nothing else: every label with its properties, every
relationship with its direction. That is the honest baseline - if the model
can write good Cypher from the schema alone, the rest of the prompt is
ballast and should go.

FULL is what ships today: the same schema plus the direction warnings, the
real enum values, the query recipes and the traps.

Only the planning stage runs, then the Cypher is executed and the rows
counted. Rows are the measure, because the failure that matters here is a
query that runs, returns nothing, and reads as "the graph has no data".
"""
from __future__ import annotations

import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import ask as A                                              # noqa: E402
import pipeline as P                                         # noqa: E402
from schema_prompt import (PLAN_TOOL, PLANNER_SYSTEM,        # noqa: E402
                           graph_schema, live_counts)
sys.path.insert(0, str(HERE.parent / "graph"))
from emit import NODE_COLUMNS                                # noqa: E402
from make_tech_doc import EDGES                              # noqa: E402


def minimal_schema() -> str:
    counts = live_counts(A.run_cypher)
    nodes = "\n".join(
        f"  (:{l})  {counts.get(l, '?'):>11,}   "
        + ", ".join(c for c in cols if c != "key")
        if isinstance(counts.get(l), int) else
        f"  (:{l})   " + ", ".join(c for c in cols if c != "key")
        for l, cols in sorted(NODE_COLUMNS.items()))
    edges = "\n".join(f"  (:{v[0]})-[:{k}]->(:{v[1]})"
                      for k, v in sorted(EDGES.items()))
    return f"NODE LABELS\n{nodes}\n\nRELATIONSHIPS\n{edges}\n"


QUESTIONS = [
    "which drugs target EGFR",
    "what are the side effects of metformin",
    "how many trials are running in the MENA region",
    "which agencies approved products containing atorvastatin",
    "find phase 3 trials studying breast cancer",
    "what recalls involve valsartan",
    "which companies sponsor the most clinical trials",
    "what is the UNII for atorvastatin",
    "which pathogenic variants are implicated in cystic fibrosis",
    "what ATC class does metformin belong to",
    "which drugs have cardiac adverse events reported",
    "what publications mention pembrolizumab",
]


def plan_with(schema: str, question: str, tries: int = 3):
    """One planning call against a given schema block.

    Retried: the provider reset the connection partway through a 24-call run,
    and losing the comparison to a transient network fault would say nothing
    about either prompt.
    """
    t0 = time.time()
    for attempt in range(tries):
        try:
            data = P._chat(
                [{"role": "system",
                  "content": PLANNER_SYSTEM + "\n\nGRAPH SCHEMA\n" + schema},
                 {"role": "user", "content": question}],
                tools=PLAN_TOOL, max_tokens=P.PLAN_TOKENS)
            break
        except Exception:                                    # noqa: BLE001
            if attempt == tries - 1:
                return "", int((time.time() - t0) * 1000)
            time.sleep(4 * (attempt + 1))
    calls = data["choices"][0]["message"].get("tool_calls") or []
    if not calls:
        return "", int((time.time() - t0) * 1000)
    try:
        args = P._parse_args(calls[0]["function"]["arguments"])
    except Exception:                                        # noqa: BLE001
        return "", int((time.time() - t0) * 1000)
    return (args.get("cypher") or "").strip(), int((time.time() - t0) * 1000)


def score(cypher: str):
    """(rows, note). A query that will not run is worth nothing."""
    if not cypher:
        return -1, "no query"
    bad = A.check_cypher(cypher)
    if bad:
        return -1, f"refused: {bad[:40]}"
    wrong = P.check_directions(cypher)
    if wrong:
        return -1, "arrow backwards"
    try:
        rows, _ = A.run_cypher(cypher)
        return len(rows), "" if rows else "0 rows"
    except Exception as e:                                   # noqa: BLE001
        return -1, type(e).__name__


def main():
    mini = minimal_schema()
    full = graph_schema(live_counts(A.run_cypher))
    print(f"\nMINIMAL prompt: {len(mini):,} chars   "
          f"FULL prompt: {len(full):,} chars\n")
    print(f"  {'question':<48} {'minimal':>10} {'full':>10}")
    print("  " + "-" * 70)

    wins = {"minimal": 0, "full": 0, "tie": 0}
    for q in QUESTIONS:
        cm, _ = plan_with(mini, q)
        nm, note_m = score(cm)
        cf, _ = plan_with(full, q)
        nf, note_f = score(cf)

        def cell(n, note):
            return note[:10] if n < 0 else (f"{n:,}" if n else "0 rows")

        if nm > 0 and nf > 0:
            w = "tie"
        elif nm > nf:
            w = "minimal"
        elif nf > nm:
            w = "full"
        else:
            w = "tie"
        wins[w] += 1
        mark = " " if w == "tie" else ("<" if w == "minimal" else ">")
        print(f"  {q[:48]:<48} {cell(nm, note_m):>10} {mark} "
              f"{cell(nf, note_f):>10}")
        if nm <= 0 and cm:
            print(f"        minimal: {' '.join(cm.split())[:96]}")
        if nf <= 0 and cf:
            print(f"        full:    {' '.join(cf.split())[:96]}")

    print()
    ok_m = sum(1 for q in [] )  # placeholder, totals printed below
    print(f"  usable answers — minimal beat full on {wins['minimal']}, "
          f"full beat minimal on {wins['full']}, "
          f"both worked on {wins['tie']}")


if __name__ == "__main__":
    main()
