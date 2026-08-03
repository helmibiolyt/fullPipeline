#!/usr/bin/env python3
"""Why half the trials have no disease link.

    python graph/why_unlinked.py                 # ct.gov, the biggest block
    python graph/why_unlinked.py who chictr      # named registries

_trial() links a trial to a Disease by looking its condition text up in
`mesh_by_name`, which load_mesh fills with MeSH headings and up to 30 entry
terms each. Nothing else is consulted - so the 8,500 ICD-10 and 17,012 ICD-11
Disease nodes in the same graph cannot match a trial, and a condition written
the way a clinician writes it rather than the way MeSH indexes it produces no
edge at all.

This measures which of those is actually costing the coverage, before anyone
writes a matcher to fix it. Reads the lake and prints; changes nothing.
"""
from __future__ import annotations

import collections
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import lake                                              # noqa: E402
import trials as T                                       # noqa: E402
from normalise import fold                               # noqa: E402

import disease as D                                     # noqa: E402

MESH = D.L["mesh"]
ICD10 = D.L["icd10_codes"]
ICD11 = D.L["icd11"]

# Registry -> (lake path, condition column). Only the ones whose loader passes
# a condition through; the rest cannot be helped by a better matcher.
SOURCES = {
    "ctgov":  (T.L["ctgov"],  "conditions"),
    "who":    (T.L["who"],    "Condition"),
    "chictr": (T.L["chictr"], "hc_freetext"),
    "isrctn": (T.L["isrctn"], "Health condition(s) or problem(s) studied"),
    "ctis":   (T.L["ctis"],   "Medical conditions"),
}


def load_names():
    """The dictionary the build actually uses, and the one it ignores."""
    mesh, icd = {}, {}
    for row in lake.stream_csv(MESH):
        name = (row.get("name") or "").strip()
        ui = (row.get("descriptor_ui") or "").strip()
        if not name or not ui:
            continue
        mesh.setdefault(fold(name), ui)
        for syn in (row.get("synonyms") or "").split(";")[:30]:
            f = fold(syn)
            if len(f) >= 4:
                mesh.setdefault(f, ui)
    for path in (ICD10, ICD11):
        try:
            for row in lake.stream_csv(path):
                name = (row.get("title") or "").strip()
                c = (row.get("code") or "").strip()
                # "Other specified", "Unspecified" and friends are ICD
                # bookkeeping, not conditions anyone runs a trial on.
                low = name.lower()
                if (not name or not c or len(fold(name)) < 4
                        or low.startswith(("other ", "unspecified"))
                        or "unspecified" in low or "not elsewhere" in low):
                    continue
                icd.setdefault(fold(name), c)
        except Exception as exc:                          # noqa: BLE001
            print(f"  (skipped {path}: {type(exc).__name__})")
    return mesh, icd


def main():
    want = sys.argv[1:] or ["ctgov"]
    mesh, icd = load_names()
    print(f"MeSH names+synonyms {len(mesh):,}   ICD-10/11 names {len(icd):,}\n")

    for reg in want:
        if reg not in SOURCES:
            print(f"{reg}: no condition column"); continue
        path, col = SOURCES[reg]
        n = no_text = by_mesh = by_icd = unmatched = 0
        misses = collections.Counter()
        for row in lake.stream_csv(path):
            n += 1
            terms = T._terms(row.get(col, ""))
            if not terms:
                no_text += 1
                continue
            if any(fold(t) in mesh for t in terms):
                by_mesh += 1
            elif any(fold(t) in icd for t in terms):
                by_icd += 1                 # ICD WOULD match, today it does not
            else:
                unmatched += 1
                for t in terms[:3]:
                    misses[t.strip()[:60]] += 1
        print(f"{reg}  {n:,} rows")
        print(f"   no condition text        {no_text:>9,} ({no_text/n:5.1%})")
        print(f"   matched by MeSH today    {by_mesh:>9,} ({by_mesh/n:5.1%})")
        print(f"   ICD would match, unused  {by_icd:>9,} ({by_icd/n:5.1%})  <-- the gap")
        print(f"   neither                  {unmatched:>9,} ({unmatched/n:5.1%})")
        print("   most common unmatched terms:")
        for term, c in misses.most_common(12):
            print(f"      {c:>7,}  {term!r}")
        print()


if __name__ == "__main__":
    main()
