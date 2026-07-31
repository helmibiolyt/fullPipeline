#!/usr/bin/env python3
"""Which way of using the two stores actually answers more questions.

    python testPipeline/strategy.py --sample 2

There is no rule that says graph first or documents first. Whether one store
answers, or one has to feed the other, is a property of the question, and the
only way to find out is to run the same questions four ways and compare what
came back.

    graph      only Cypher
    docs       only document search
    parallel   one of each, fixed, both fired before either is read
    agentic    the free loop: any order, any number, each choice made after
               seeing the last result

Scored WITHOUT a judge. The model grades its own work far too kindly, so the
comparison uses the one thing that cannot be argued with: contradiction
between arms. If `graph` says nothing was found and `agentic` returns forty
rows on the same question, `graph` is wrong - not "worse", wrong - and no
opinion was needed to establish it.

Everything else recorded here is cost: lookups, seconds, tokens. The output is
meant to answer one question - when is each route worth taking - with numbers
attached rather than an argument.
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import json
import pathlib
import sys
import threading

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agent as AG                                             # noqa: E402
import bench as B                                              # noqa: E402

#: Each arm is (name, allow, max_graph, max_docs). The two single-store arms
#: keep the same total budget as the mixed ones, so a win is not just a bigger
#: allowance.
#: `split` is the old 4+4 per-store budget with no miss handling - the shape
#: that produced six of the seven false denials. `agentic` is the same loop
#: with a shared ceiling and the switch-on-repeated-miss rule. They differ in
#: nothing else, so the difference between them IS the value of the rule.
ARMS = [
    ("graph",    ("graph",),             8, 0, False),
    ("docs",     ("documents",),         0, 8, False),
    ("parallel", ("graph", "documents"), 1, 1, False),
    ("split",    ("graph", "documents"), 4, 4, False),
    ("agentic",  ("graph", "documents"), 6, 6, True),
]


def _evidence(res: dict) -> int:
    return sum(s.get("total", 0) for s in res.get("steps", [])
               if s.get("tool") in ("graph", "documents"))


def run_arm(name: str, allow, mg: int, md: int, sw: bool, q: str, k: int) -> dict:
    try:
        res = AG.run(q, k=k, allow=allow, max_graph=mg, max_docs=md,
                     switch_on_miss=sw)
    except Exception as e:                                     # noqa: BLE001
        return {"arm": name, "fatal": f"{type(e).__name__}: {e}",
                "evidence": 0, "denies": True, "seq": "", "ms": 0, "tokens": 0}
    c = B.classify(q, res)
    return {"arm": name, "seq": c["seq"], "lookups": c["lookups"],
            "evidence": _evidence(res), "denies": c["denies"],
            "verdict": c["verdict"], "chained": c["chained"],
            "ms": res.get("total_ms", 0), "tokens": res.get("tokens", 0),
            "answer": (res.get("answer") or "")[:1200]}


def compare(cat: str, q: str, k: int) -> dict:
    arms = {}
    # Sequential across arms on purpose: the arms share a rate limit, and a
    # timeout in one would otherwise be scored as that arm losing.
    for name, allow, mg, md, sw in ARMS:
        arms[name] = run_arm(name, allow, mg, md, sw, q, k)

    best = max(a["evidence"] for a in arms.values())
    # The finding that needs no judge: an arm that reported absence while
    # another arm found something on the identical question.
    wrong = [n for n, a in arms.items()
             if a["denies"] and best > 0 and a["evidence"] < best]
    return {"category": cat, "question": q, "arms": arms,
            "best_evidence": best, "false_denial": wrong,
            "winner": max(arms.items(), key=lambda kv: kv[1]["evidence"])[0]}


def report(rows: list[dict]) -> None:
    names = [a[0] for a in ARMS]
    print(f"\n{'='*76}\n{len(rows)} questions x {len(names)} arms\n{'='*76}")

    print(f"\n{'arm':<10} {'answered':>9} {'false denials':>14} "
          f"{'evidence':>10} {'lookups':>8} {'sec':>6} {'tokens':>9}")
    for n in names:
        got = [r["arms"][n] for r in rows]
        print(f"  {n:<8} "
              f"{sum(1 for a in got if a['evidence'] > 0):>9} "
              f"{sum(1 for r in rows if n in r['false_denial']):>14} "
              f"{sum(a['evidence'] for a in got):>10,} "
              f"{sum(a.get('lookups', 0) for a in got):>8} "
              f"{sum(a['ms'] for a in got)/1000/len(got):>6.0f} "
              f"{sum(a['tokens'] for a in got):>9,}")

    print("\nWHICH ARM RETURNED THE MOST EVIDENCE")
    for n, c in collections.Counter(r["winner"] for r in rows).most_common():
        print(f"  {n:<10} {c:>4}")

    print("\nBY CATEGORY - winner, and whether one store alone sufficed")
    bycat = collections.defaultdict(list)
    for r in rows:
        bycat[r["category"]].append(r)
    print(f"  {'category':<30} {'n':>3}  {'winner':<10} "
          f"{'graph alone':>11} {'docs alone':>10}")
    for c, rs in sorted(bycat.items()):
        w = collections.Counter(r["winner"] for r in rs).most_common(1)[0][0]
        ga = sum(1 for r in rs if r["arms"]["graph"]["evidence"] > 0)
        da = sum(1 for r in rs if r["arms"]["docs"]["evidence"] > 0)
        print(f"  {c:<30} {len(rs):>3}  {w:<10} {ga:>11} {da:>10}")

    fd = [r for r in rows if r["false_denial"]]
    if fd:
        print(f"\nFALSE DENIALS - an arm reported absence that another arm "
              f"disproved ({len(fd)})")
        for r in fd:
            print(f"  {','.join(r['false_denial']):<20} {r['question'][:56]}")

    # The whole point: does one store's output need to feed the other's input?
    ch = [r for r in rows if r["arms"]["agentic"]["chained"]]
    print(f"\nCHAINED in the agentic arm: {len(ch)}/{len(rows)}")
    beat = [r for r in ch
            if r["arms"]["agentic"]["evidence"] > r["arms"]["parallel"]["evidence"]]
    print(f"  of those, agentic beat fixed-parallel on evidence: {len(beat)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=2)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--k", type=int, default=6)
    ap.add_argument("--out", default="testPipeline/strategy_results.jsonl")
    ap.add_argument("--report", default="")
    a = ap.parse_args()

    if a.report:
        report([json.loads(l) for l in pathlib.Path(a.report)
                .read_text(encoding="utf-8").splitlines() if l.strip()])
        return

    bank = B.load_bank(B.BANK)
    per, keep = collections.defaultdict(int), []
    for c, q in bank:
        if per[c] < a.sample:
            per[c] += 1
            keep.append((c, q))

    out = pathlib.Path(a.out)
    fh = out.open("w", encoding="utf-8")
    lock, rows, done = threading.Lock(), [], 0
    print(f"{len(keep)} questions x {len(ARMS)} arms = "
          f"{len(keep)*len(ARMS)} runs -> {out}\n")
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(compare, c, q, a.k): q for c, q in keep}
        for f in cf.as_completed(futs):
            try:
                r = f.result()
            except Exception as e:                             # noqa: BLE001
                print("  FAILED", futs[f][:50], e)
                continue
            with lock:
                done += 1
                rows.append(r)
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                fh.flush()
                print(f"  [{done:>3}/{len(keep)}] winner={r['winner']:<9} "
                      f"{r['question'][:56]}")
    fh.close()
    report(rows)


if __name__ == "__main__":
    main()
