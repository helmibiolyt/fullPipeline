#!/usr/bin/env python3
"""Run the question bank through the agent and classify what happened.

    python testPipeline/bench.py                    everything
    python testPipeline/bench.py --sample 2         2 per category, fast pass
    python testPipeline/bench.py --category Safety  one category
    python testPipeline/bench.py --report out.jsonl re-analyse a finished run

The question the research agent design turns on is not "did it answer" - a
fluent answer is cheap and this model will always produce one. It is:

    which store held the answer, and did one store's output have to feed the
    other's input?

So every run records the tool sequence, and whether a lookup used a value that
could only have come from an earlier lookup. Those two numbers decide whether
the research agent runs the stores in parallel or chains them, and that is the
whole point of testPipeline.

Written to JSONL as it goes, because a 120-question run takes an hour and
losing it to one provider timeout would be painful.
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import json
import pathlib
import re
import sys
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agent as AG                                             # noqa: E402

BANK = HERE / "questions.txt"

# Words too common to prove a chain. "phase", "cancer" and a drug's own name
# appear in the question, so their reappearance in a later query says nothing.
_STOP = {
    "phase", "trial", "trials", "study", "studies", "drug", "drugs", "name",
    "disease", "diseases", "approved", "approval", "target", "targets",
    "company", "companies", "true", "false", "null", "none", "count", "total",
    "recruiting", "completed", "terminated", "active", "unknown", "human",
    "oral", "cancer", "therapy", "therapies", "treatment", "treatments",
}
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9\-]{4,}")


def load_bank(path: pathlib.Path) -> list[tuple[str, str]]:
    out, cat = [], "Uncategorised"
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            label = line.lstrip("#").strip()
            # The file opens with a paragraph of prose comment; only short
            # lines are category headers.
            if label and len(label) < 40:
                cat = label
            continue
        out.append((cat, line))
    return out


def _values(step: dict) -> set[str]:
    """Every distinct token this step RETURNED. The vocabulary a later query
    could only have got from here."""
    if "vals" in step:                    # re-analysing a saved run
        return set(step["vals"])
    got = set()
    for r in step.get("rows", [])[:60]:
        for v in r.values():
            for m in _TOKEN.findall(str(v)[:200]):
                got.add(m.lower())
    for c in step.get("chunks", [])[:10]:
        for f in ("heading", "text"):
            for m in _TOKEN.findall(str(c.get(f, ""))[:400]):
                got.add(m.lower())
    return got - _STOP


def _inputs(step: dict) -> set[str]:
    """Every token this step SEARCHED ON - string literals in the Cypher, or
    the document query. Not the whole query: keywords would match everything."""
    q = step.get("query", "") or ""
    if step.get("tool") == "graph":
        # Only the string literals. Matching on the whole query would count
        # every label and keyword, and "Substance" appears in all of them.
        text = " ".join(a or b for a, b in
                        re.findall(r"'([^']{3,})'|\"([^\"]{3,})\"", q))
    else:
        text = q
    return {m.lower() for m in _TOKEN.findall(text)} - _STOP


def classify(question: str, res: dict) -> dict:
    """What shape did this answer have, and how did it fail if it did."""
    steps = [s for s in res.get("steps", []) if s.get("tool") in ("graph", "documents")]
    qtok = {m.lower() for m in _TOKEN.findall(question)}

    seq = [s["tool"][0] for s in steps]              # e.g. "gdd" = graph,doc,doc
    rows = sum(s.get("total", 0) for s in steps)
    errs = [s for s in steps if s.get("error")]
    empty = [s for s in steps if not s.get("error") and not s.get("total")]

    # A chain: a step searched on a value it could not have known from the
    # question, and an EARLIER step returned that value.
    chained, chain_ev = False, []
    seen: set[str] = set()
    for s in steps:
        borrowed = (_inputs(s) & seen) - qtok
        if borrowed:
            chained = True
            chain_ev.append({"step": s.get("step"), "tool": s.get("tool"),
                             "borrowed": sorted(borrowed)[:6]})
        seen |= _values(s)

    def _cut(s: dict) -> bool:
        m = re.search(r"\bLIMIT\s+(\d+)\s*$", (s.get("query") or "").strip(), re.I)
        return bool(m and s.get("total") and s["total"] == int(m.group(1)))

    truncated = [s for s in steps if _cut(s)]

    # A truncated step is only a defect if nothing after it fixed the problem.
    # The warning tells the model to re-run grouped, and when it does - count,
    # collect, sum - the earlier partial read stops mattering. Scoring every
    # truncation as a failure hid that the warning was working.
    _AGG = re.compile(r"\b(count|collect|sum|avg|min|max)\s*\(", re.I)
    recovered = False
    if truncated:
        after = steps[steps.index(truncated[-1]) + 1:]
        recovered = any(_AGG.search(s.get("query") or "") and not _cut(s)
                        for s in after if s.get("tool") == "graph")

    ans = (res.get("answer") or "").strip()
    low = ans.lower()

    # An answer that describes the AGENT rather than the data. The docs-only
    # arm returned six chunks for "are there recruiting trials for ALS" and
    # then said it had no tool that could query a trial registry - retrieved
    # something, answered nothing. Scoring on evidence counted that as a store
    # that could answer, which is how "both stores could answer 17 of 22"
    # became a finding that was really a metric artefact.
    refuses = any(p in low for p in (
        "i don't have access", "i do not have access", "the available tools",
        "i'm unable to", "i am unable to", "i cannot query", "i can't query",
        "no tool", "not able to query", "outside my", "i don't have a tool"))
    # The failure that matters: a confident sentence saying the data is absent
    # when the query simply did not reach it.
    denies = any(p in low for p in (
        "no trials", "not found", "does not contain", "no data", "returned no",
        "no results", "unable to find", "no information", "graph does not",
        "no such", "nothing in the graph"))

    # A provider failure and a model that declined to use its tools both
    # produce zero steps, and they mean opposite things - one is infrastructure,
    # the other is the behaviour tool_choice="required" exists to prevent.
    # Telling them apart needs the error, which is why it is carried here.
    if not steps and (res.get("error") or not res.get("tokens")):
        verdict = "PROVIDER_ERROR"
    elif not steps:
        verdict = "NO_LOOKUP"
    elif not ans:
        verdict = "NO_ANSWER"
    elif refuses:
        verdict = "REFUSED"          # answered about itself, not the question
    elif errs and not rows:
        verdict = "ALL_ERRORED"
    elif not rows:
        verdict = "ZERO_EVIDENCE"
    elif truncated and denies:
        verdict = "TRUNCATED_DENIAL"      # the epilepsy failure, exactly
    elif denies:
        verdict = "DENIES"
    elif truncated and recovered:
        verdict = "RECOVERED"
    elif truncated:
        verdict = "TRUNCATED"
    else:
        verdict = "OK"

    return {"verdict": verdict, "seq": "".join(seq), "lookups": len(steps),
            "rows": rows, "errors": len(errs), "empty": len(empty),
            "chained": chained, "chain_evidence": chain_ev,
            "truncated": len(truncated), "recovered": recovered,
            "denies": denies, "refuses": refuses,
            # The only honest summary of "did this work": evidence came back,
            # the answer is about the data, and it does not claim absence.
            "answered": bool(rows) and not refuses and not denies,
            "answer_chars": len(ans)}


def run_one(cat: str, q: str, k: int, switch: bool = True) -> dict:
    t0 = time.time()
    try:
        res = AG.run(q, k=k, switch_on_miss=switch)
    except Exception as e:                                     # noqa: BLE001
        return {"category": cat, "question": q, "fatal": f"{type(e).__name__}: {e}",
                "verdict": "CRASH", "ms": int((time.time() - t0) * 1000)}
    row = {"category": cat, "question": q, "ms": res.get("total_ms"),
           "tokens": res.get("tokens"), "budget": res.get("budget"),
           # Kept, because dropping it turned every provider failure into a
           # row that read "the agent chose to look nothing up". 79 of 114
           # questions in one run were scored NO_LOOKUP when the model call
           # had simply raised.
           "error": res.get("error", ""),
           "answer": res.get("answer", ""), "sources": res.get("sources", []),
           # The rows themselves are dropped - a 120-question run would be
           # hundreds of MB - but the token set they contributed is kept, so
           # --report can re-derive chaining without re-running anything.
           "steps": [{kk: vv for kk, vv in s.items()
                      if kk not in ("rows", "chunks", "breakdowns")}
                     | {"n_rows": len(s.get("rows", [])),
                        "vals": sorted(_values(s))[:400]}
                     for s in res.get("steps", [])]}
    row.update(classify(q, res))
    return row


def report(rows: list[dict]) -> None:
    ok = [r for r in rows if r.get("verdict") == "OK"]
    print(f"\n{'='*74}\n{len(rows)} questions   "
          f"{len(ok)} clean   "
          f"{sum(r.get('ms') or 0 for r in rows)/1000/60:.1f} min   "
          f"{sum(r.get('tokens') or 0 for r in rows):,} tokens\n{'='*74}")

    print("\nVERDICTS")
    for v, n in collections.Counter(r.get("verdict") for r in rows).most_common():
        print(f"  {v:<20} {n:>4}")

    print("\nTOOL SEQUENCE  (g=graph  d=documents)")
    for s, n in collections.Counter(r.get("seq", "") for r in rows).most_common(12):
        print(f"  {s or '(none)':<20} {n:>4}")

    ch = [r for r in rows if r.get("chained")]
    print(f"\nCHAINED (a lookup used a value an earlier lookup returned): "
          f"{len(ch)}/{len(rows)}")
    both = [r for r in rows if "g" in r.get("seq", "") and "d" in r.get("seq", "")]
    print(f"USED BOTH STORES: {len(both)}/{len(rows)}   "
          f"graph only: {sum(1 for r in rows if set(r.get('seq','')) == {'g'})}   "
          f"documents only: {sum(1 for r in rows if set(r.get('seq','')) == {'d'})}")

    print("\nBY CATEGORY")
    bycat = collections.defaultdict(list)
    for r in rows:
        bycat[r.get("category")].append(r)
    print(f"  {'category':<28} {'n':>3} {'ok':>3} {'chain':>6} {'both':>5} "
          f"{'rows':>8} {'s':>5}")
    for c, rs in bycat.items():
        print(f"  {c:<28} {len(rs):>3} "
              f"{sum(1 for r in rs if r.get('verdict')=='OK'):>3} "
              f"{sum(1 for r in rs if r.get('chained')):>6} "
              f"{sum(1 for r in rs if 'g' in r.get('seq','') and 'd' in r.get('seq','')):>5} "
              f"{sum(r.get('rows') or 0 for r in rs):>8,} "
              f"{sum(r.get('ms') or 0 for r in rs)/1000/max(1,len(rs)):>5.0f}")

    bad = [r for r in rows if r.get("verdict") not in ("OK",)]
    if bad:
        print(f"\nNOT CLEAN ({len(bad)})")
        for r in bad:
            print(f"  {r.get('verdict'):<18} [{r.get('seq',''):<5}] "
                  f"{r['question'][:74]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0,
                    help="take only N questions per category")
    ap.add_argument("--category", default="")
    ap.add_argument("--workers", type=int, default=3,
                    help="the provider rate-limits; 3 is the safe ceiling")
    ap.add_argument("--k", type=int, default=6)
    ap.add_argument("--no-switch", action="store_true",
                    help="turn the store-switch rule off, to measure the "
                         "prompt on its own")
    ap.add_argument("--out", default="testPipeline/bench_results.jsonl")
    ap.add_argument("--report", default="", help="re-analyse an existing jsonl")
    a = ap.parse_args()

    if a.report:
        rows = [json.loads(l) for l in
                pathlib.Path(a.report).read_text(encoding="utf-8").splitlines() if l.strip()]
        # Re-classify from the stored steps rather than trusting the verdict
        # written at run time, so a change to classify() can be applied to runs
        # that already cost an hour.
        for r in rows:
            if r.get("steps") is not None:
                r.update(classify(r["question"], r))
        report(rows)
        return

    bank = load_bank(BANK)
    if a.category:
        bank = [(c, q) for c, q in bank if a.category.lower() in c.lower()]
    if a.sample:
        per = collections.defaultdict(int)
        keep = []
        for c, q in bank:
            if per[c] < a.sample:
                per[c] += 1
                keep.append((c, q))
        bank = keep

    out = pathlib.Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fh = out.open("w", encoding="utf-8")
    lock = threading.Lock()
    rows, done = [], 0

    print(f"{len(bank)} questions, {a.workers} at a time -> {out}\n")
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(run_one, c, q, a.k, not a.no_switch): (c, q)
                for c, q in bank}
        for f in cf.as_completed(futs):
            c, q = futs[f]
            try:
                r = f.result()
            except Exception as e:                             # noqa: BLE001
                r = {"category": c, "question": q, "verdict": "CRASH",
                     "fatal": str(e)}
            with lock:
                done += 1
                rows.append(r)
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                fh.flush()
                print(f"  [{done:>3}/{len(bank)}] {r.get('verdict','?'):<17} "
                      f"{r.get('seq',''):<5} {(r.get('ms') or 0)/1000:>5.0f}s  "
                      f"{q[:56]}")
    fh.close()
    report(rows)


if __name__ == "__main__":
    main()
