#!/usr/bin/env python3
"""How many trials COULD have a drug link, and how many do?

    python graph/drug_ceiling.py

19.1% of trials name a drug this graph knows, and that number on its own says
nothing, because most trials do not test a drug at all. A behavioural trial, a
device trial, a surgical technique - none of them should resolve to a
Substance, and counting them in the denominator makes the pipeline look worse
than it is and hides where the real loss is.

ClinicalTrials.gov labels every intervention with its type, so its rows can be
split into the ones that name a drug and the ones that do not. That gives a
ceiling: the share of DRUG trials that resolve is the number worth improving.

Reads the lake, prints, changes nothing.
"""
from __future__ import annotations

import collections
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import lake                                              # noqa: E402
import trials as T                                       # noqa: E402

# ct.gov's own labels. DRUGGY ones should resolve to a Substance; the rest
# should not, and a graph that linked them would be wrong rather than complete.
DRUGGY = ("drug:", "biological:")
NOT_DRUGGY = ("behavioral:", "device:", "procedure:", "dietary supplement:",
              "radiation:", "diagnostic test:", "genetic:", "other:",
              "combination product:")


def main() -> None:
    n = 0
    has_druggy = has_other = has_none = 0
    druggy_terms = collections.Counter()
    for row in lake.stream_csv(T.L["ctgov"]):
        n += 1
        raw = (row.get("interventions") or "")
        low = raw.lower()
        d = any(k in low for k in DRUGGY)
        o = any(k in low for k in NOT_DRUGGY)
        if d:
            has_druggy += 1
            # What the drug-typed arms actually say, once the label is off.
            for part in T._SEP.split(raw):
                pl = part.lower().strip()
                if any(pl.startswith(k) for k in DRUGGY):
                    for t in T._terms(part, kind="intervention"):
                        druggy_terms[t.strip()[:44]] += 1
        elif o:
            has_other += 1
        else:
            has_none += 1

    print(f"ClinicalTrials.gov rows        {n:,}")
    print(f"  name a Drug or Biological    {has_druggy:>9,} ({has_druggy/n:5.1%})"
          f"   <- the only ones that SHOULD resolve")
    print(f"  only non-drug interventions  {has_other:>9,} ({has_other/n:5.1%})"
          f"   behavioural, device, procedure")
    print(f"  no intervention label at all {has_none:>9,} ({has_none/n:5.1%})")
    print()
    print("The 19.1% headline uses every trial as its denominator, which counts")
    print("device and behavioural trials as drug-linkage failures. They are not.")
    print()
    print("most common drug-typed terms after label stripping:")
    for term, c in druggy_terms.most_common(20):
        print(f"   {c:>7,}  {term!r}")


if __name__ == "__main__":
    main()
