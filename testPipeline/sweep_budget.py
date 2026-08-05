#!/usr/bin/env python3
"""Does a bigger graph budget buy a better answer on this model?

    python testPipeline/sweep_budget.py            20 capped questions
    python testPipeline/sweep_budget.py 40

ROUTING.md's constants - MAX_GRAPH=4, MAX_DOCS=4, RUN_BEFORE_SWITCH - were
tuned against MiniMax-M2. The backend ships M2.7-highspeed, and its lookup
behaviour is not the same shape:

    graph calls per question   {0:1, 1:23, 2:19, 3:20, 4:6, 5:47}

That is bimodal. Either it answers in one or two calls, or it uses every call
it has - 53 of 116 questions reach the cap. So the cap binds on nearly half
the bank, and whether that costs anything is a measurable question rather than
a matter of opinion.

Runs the SAME capped questions at two budgets and compares verdicts. Anything
that improves has to improve here; anything that does not is the model
choosing to stop, which no budget can fix.
"""
from __future__ import annotations

import json
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agent as AG                                       # noqa: E402
import bench as B                                        # noqa: E402

RESULTS = HERE / "bench_results.jsonl"


def capped_questions(n: int) -> list[str]:
    """The questions that used every graph call they were given."""
    out = []
    for line in RESULTS.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        steps = [s for s in r.get("steps", []) if s.get("tool") == "graph"]
        if len(steps) >= AG.MAX_GRAPH:
            out.append(r["question"])
    return out[:n]


def run(question: str, max_graph: int) -> dict:
    try:
        res = AG.run(question, max_graph=max_graph, max_docs=4)
    except Exception as e:                                # noqa: BLE001
        return {"verdict": "ERROR", "lookups": 0, "err": str(e)[:60]}
    c = B.classify(question, res)
    steps = [s for s in res.get("steps", []) if s.get("tool") in ("graph", "documents")]
    return {"verdict": c["verdict"], "lookups": len(steps),
            "rows": c["rows"], "chars": len(res.get("answer") or "")}


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    qs = capped_questions(n)
    print(f"{len(qs)} questions that exhausted the graph budget\n")
    print(f"{'':<58}{'MAX_GRAPH=4':>22}{'MAX_GRAPH=10':>22}")
    tally = {4: {}, 10: {}}
    for q in qs:
        row = {}
        for cap in (4, 10):
            r = run(q, cap)
            row[cap] = r
            tally[cap][r["verdict"]] = tally[cap].get(r["verdict"], 0) + 1
            time.sleep(1)
        print(f"{q[:56]:<58}"
              f"{row[4]['verdict'][:12]:>13}/{row[4]['lookups']:<8}"
              f"{row[10]['verdict'][:12]:>13}/{row[10]['lookups']:<8}", flush=True)
    print()
    for cap in (4, 10):
        print(f"MAX_GRAPH={cap:<3} {tally[cap]}")


if __name__ == "__main__":
    main()
