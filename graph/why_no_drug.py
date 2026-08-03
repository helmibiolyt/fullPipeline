#!/usr/bin/env python3
"""Why only 15.7% of trials name a drug this graph knows.

    python graph/why_no_drug.py

Disease linkage got a day of attention and reached 60%. TESTED_IN never got
any and sits at 15.7%, with 93% of it coming from ClinicalTrials.gov alone -
so this is either a resolver problem or a text problem, and which one decides
whether it is worth fixing.

_trial() passes intervention prose through _terms() and then demands a
CONFIDENT resolver hit: `if m.key and m.resolved`. A provisional match is
dropped on purpose, because 1.6M rows of "Drug: placebo comparator, 10mg
tablet, twice daily" would otherwise mint a Substance node each.

Prints, changes nothing.
"""
from __future__ import annotations

import collections
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import lake                                              # noqa: E402
import trials as T                                       # noqa: E402

# Registry -> (lake path, the column its loader passes as `interventions`)
SOURCES = {
    "ctgov":  (T.L["ctgov"],  "interventions"),
    "who":    (T.L["who"],    "Intervention"),
    "chictr": (T.L["chictr"], "i_freetext"),
    "isrctn": (T.L["isrctn"], "Drug/device/biological/vaccine name(s)"),
    "ctis":   (T.L["ctis"],   "Product"),
    "anzctr": (T.L["anzctr"], "INTERVENTIONS"),
}

# The loaders that pass NO intervention column at all, so no amount of
# matching can help them - the fix there is a loader change, not a resolver.
NO_COLUMN = {
    "ctri":  (T.L["ctri"],  "61,738 trials"),
    "jrct":  (T.L["jrct"],  "478 trials"),
    "euctr": (T.L["euctr"], "44,511 trials, header damaged past column 4"),
}


def main() -> None:
    want = sys.argv[1:] or list(SOURCES)
    print("Registries whose loader passes no intervention column at all:")
    for reg, (path, note) in NO_COLUMN.items():
        cols = []
        try:
            for row in lake.stream_csv(path):
                cols = list(row.keys())
                break
        except Exception as exc:                          # noqa: BLE001
            print(f"   {reg}: {type(exc).__name__}")
            continue
        hits = [c for c in cols
                if any(w in c.lower() for w in
                       ("interven", "drug", "product", "treatment", "arm"))]
        print(f"   {reg:<8} {note}")
        print(f"            candidate columns: {hits[:6] if hits else 'NONE'}")
    print()

    for reg in want:
        if reg not in SOURCES:
            continue
        path, col = SOURCES[reg]
        n = blank = resolved = provisional = 0
        shapes = collections.Counter()
        misses = collections.Counter()
        for row in lake.stream_csv(path):
            n += 1
            raw = row.get(col, "") or ""
            terms = T._terms(raw)
            if not terms:
                blank += 1
                continue
            # No resolver here: building one needs the whole gsrs/chembl load.
            # What this can answer without it is the SHAPE of the text, which
            # is what decides whether the fix is a resolver change or a
            # cleaning step before it.
            provisional += 1
            shapes[_shape(raw)] += 1
            for t in terms[:2]:
                misses[t.strip()[:56]] += 1
        print(f"{reg}  {n:,} rows")
        print(f"   no intervention text     {blank:>9,} ({blank/n:5.1%})")
        print(f"   has intervention text    {provisional:>9,} ({provisional/n:5.1%})")
        print("   how that text is written:")
        for sh, c in shapes.most_common(4):
            print(f"      {c:>7,}  {sh}")
        print("   most common unmatched terms:")
        for term, c in misses.most_common(8):
            print(f"      {c:>7,}  {term!r}")
        print()


def _shape(raw: str) -> str:
    """A crude description of how a registry writes its intervention cell."""
    s = " ".join((raw or "").split())
    if not s:
        return "empty"
    if len(s) > 200:
        return "long prose (>200 chars)"
    if ":" in s[:24]:
        return "prefixed, e.g. 'Drug: X'"
    if any(sep in s for sep in (";", "|")):
        return "delimited list"
    return "short free text"


if __name__ == "__main__":
    main()
