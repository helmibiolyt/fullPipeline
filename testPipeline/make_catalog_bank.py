#!/usr/bin/env python3
"""Turn the persona catalog into a bank the agent can actually be asked.

    python testPipeline/make_catalog_bank.py

Two things stand between the xlsx and a runnable benchmark.

The first is that 63 of the 134 questions are about the sponsor's own EDC,
CTMS and safety case database - open queries, screen-fail rate by site, budget
vs actual. Neither store holds that, so they are dropped rather than answered
badly. catalog_route.py does that split and explains it.

The second is that the remaining 71 are CARD LABELS, not questions: 60 of them
carry a placeholder like [product] or [indication]. "Show all serious adverse
events for [product]" cannot be asked of anything. They are instantiated here
with values that are real and well-populated IN THIS GRAPH, checked before
being chosen:

    PEMBROLIZUMAB                    2,917 trials,   587 adverse events
    NIVOLUMAB                        2,051 trials,   720 adverse events
    Metformin                        2,094 trials, 1,370 adverse events
    Carcinoma, Non-Small-Cell Lung   8,656 trials

Picking a drug the graph barely holds would measure the data, not the routing.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "questions_catalog.txt"

#: Placeholder -> a value this graph can actually answer about.
FILL = {
    "product": "pembrolizumab",
    "competitor product": "nivolumab",
    "comparator": "nivolumab",
    "indication": "non-small cell lung cancer",
    "therapeutic class": "PD-1 checkpoint inhibitors",
    "pt": "pneumonitis",
    "concomitant medication": "metformin",
    "country/region": "Germany",
    "subpopulation": "elderly patients",
    "claim": "improved overall survival versus chemotherapy",
    "scientific question": "how does pembrolizumab compare to nivolumab on "
                           "overall survival in NSCLC",
    "kol question": "what is the evidence for pembrolizumab in PD-L1 low NSCLC",
    "specific labeling statement": "the overall survival benefit in NSCLC",
    "x": "2.7.4 (Summary of Clinical Safety)",
    "study": "the pembrolizumab NSCLC programme",
    "form/crf": "the adverse event form",
    "n": "30",
    "guidance change": "the FDA 2024 guidance on dose optimisation in oncology",
    "emerging safety signal": "immune-mediated pneumonitis",
    "program": "the pembrolizumab NSCLC programme",
    "therapeutic area": "oncology",
    "competitor asset": "nivolumab",
    "labeling claim": "the overall survival benefit in NSCLC",
}

HEADERS = [
    ("GRAPH", "Graph - counts, filters, aggregations over structured records"),
    ("DOCS", "Documents - wording, findings, narrative"),
    ("BOTH", "Both - the graph fixes the set, the documents say what it found"),
]


def fill(q: str) -> tuple[str, list[str]]:
    """Substitute every placeholder; report any with no value defined."""
    missing = []

    def sub(m: re.Match) -> str:
        key = m.group(1).strip().lower()
        if key in FILL:
            return FILL[key]
        missing.append(key)
        return m.group(0)

    return re.sub(r"\[([^\]]+)\]", sub, q), missing


def main() -> None:
    tmp = HERE / "_catalog.json"
    subprocess.run([sys.executable, str(HERE / "catalog_route.py"),
                    "--json", str(tmp)], check=True,
                   stdout=subprocess.DEVNULL)
    rows = [x for x in json.loads(tmp.read_text(encoding="utf-8"))
            if x["verdict"] != "ABSENT"]
    tmp.unlink(missing_ok=True)

    # Only the three ROUTE lines may start with "#": bench.py opens a new
    # category on every comment line, so a nine-line preamble made the last
    # header line the category and the report came out labelled
    # "make_catalog_bank.py, not this file." The explanation lives in this
    # module's docstring, where it cannot corrupt the parse.
    out = []
    unresolved: list[str] = []
    for verdict, label in HEADERS:
        out.append(f"# {label}")
        for x in rows:
            if x["verdict"] != verdict:
                continue
            q, missing = fill(x["question"])
            unresolved += missing
            out.append(q)
        out.append("")

    OUT.write_text("\n".join(out), encoding="utf-8")
    n = sum(1 for line in out if line and not line.startswith("#"))
    print(f"wrote {OUT}  ({n} questions)")
    if unresolved:
        print(f"  UNFILLED placeholders: {sorted(set(unresolved))}")
    else:
        print("  every placeholder resolved")


if __name__ == "__main__":
    sys.exit(main())
