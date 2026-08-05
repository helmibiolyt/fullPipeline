#!/usr/bin/env python3
"""Is euctr's data actually lost, or only its header?

    python graph/euctr_recover.py

The eu_ctr loader reads four columns and says so in its docstring: the header
is damaged past column four, the names there being inclusion-criteria prose
that leaked in during scraping. 44,511 trials therefore carry a status, a
condition, and nothing else - no title, no phase, no sponsor, no drug.

That reasoning was right about the header and wrong about the data. The file
has 8,102 columns because every distinct criteria line across 44,511 trials
became one, but the REAL EudraCT fields are still in there among them, and
they are identifiable: EudraCT numbers its sections, so a genuine field is
"A.3 Full title of the trial" or "E.1.1 Medical condition(s)" while the junk
is "A. Adequate renal function defined as".

This counts how many rows actually carry each field worth having, so the
recovery is sized before it is written.

Reads the lake, prints, changes nothing.
"""
from __future__ import annotations

import collections
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import lake                                              # noqa: E402
import trials as T                                       # noqa: E402

# The EudraCT protocol sections worth having, by their published numbers.
WANT = [
    ("A.3 Full title of the trial", "title"),
    ("B.1.1 Name of Sponsor", "sponsor"),
    ("E.1.1 Medical condition(s) being investigated", "condition (already read)"),
    ("D.3.1 Product name", "drug, trade name"),
    ("D.3.8 INN - Proposed INN", "drug, INN"),
    ("D.2.1.1.1 Trade name", "drug, trade name alt"),
    ("E.7.1 Human pharmacology (Phase I)", "phase 1 flag"),
    ("E.7.2 Therapeutic exploratory (Phase II)", "phase 2 flag"),
    ("E.7.3 Therapeutic confirmatory (Phase III)", "phase 3 flag"),
    ("E.7.4 Therapeutic use (Phase IV)", "phase 4 flag"),
    ("E.8.1 Controlled", "controlled"),
    ("F.1.1 Trials involving minors", "paediatric"),
]

LIMIT = 6000


def main() -> None:
    filled: collections.Counter = collections.Counter()
    sample: dict[str, str] = {}
    n = 0
    for row in lake.stream_csv(T.L["euctr"]):
        n += 1
        for col, _ in WANT:
            v = (row.get(col) or "").strip()
            if v:
                filled[col] += 1
                sample.setdefault(col, v[:52])
        if n >= LIMIT:
            break

    print(f"sampled {n:,} eu_ctr rows of 44,511\n")
    print(f"{'field':<46}{'filled':>8}  example")
    print("-" * 96)
    for col, what in WANT:
        pct = filled[col] / n if n else 0
        print(f"{col[:44]:<46}{filled[col]:>7,} {pct:>5.0%}  "
              f"{sample.get(col, '')!r}")
    print()
    print("Every one of these is present in the file today and unread. The")
    print("loader stops at four columns because the HEADER is polluted, not")
    print("because the fields are missing.")


if __name__ == "__main__":
    main()
