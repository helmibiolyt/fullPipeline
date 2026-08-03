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
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import os                                                # noqa: E402

import lake                                              # noqa: E402
import trials as T                                       # noqa: E402
from normalise import fold                               # noqa: E402

import disease as D                                     # noqa: E402

# load_mesh keeps 30 entry terms per descriptor. That cap is right for the
# stored `synonyms` property - nobody reads 127 of them - but it also decides
# what the MATCHER can see, and 'renal cell carcinoma' is entry term 40 of
# 'Carcinoma, Renal Cell'. SYN_CAP=0 measures the matcher without the cap.
SYN_CAP = int(os.getenv("SYN_CAP", "30")) or None

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
        for syn in (row.get("synonyms") or "").split(";")[:SYN_CAP]:
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


# What the unmatched terms actually are. Measured on ct.gov's 146,576 misses:
# they are not missing concepts, they are the same concepts written the way a
# protocol writes them - a stage qualifier in front, a plural on the end, and
# "cancer" where MeSH indexes "Neoplasms".
_QUALIFIER = re.compile(
    r"^(?:metastatic|advanced|recurrent|refractory|relapsed|unresectable|"
    r"locally advanced|early|late|acute|chronic|severe|mild|moderate|"
    r"newly diagnosed|previously treated|unspecified|adult|paediatric|"
    r"pediatric|primary|secondary|stage [0-9iv]+)\s+", re.I)

_CANCER = re.compile(r"(?<![a-z])(cancers?|tumou?rs?|carcinomas?|malignanc(?:y|ies))(?![a-z])",
                     re.I)


def variants(term: str):
    """Rewritings worth trying before declaring a condition unmatched.

    Each one is a guess about how the writer differed from MeSH, and they are
    tried cheapest-first. Nothing here invents a concept: every variant still
    has to hit the dictionary to count.
    """
    t = " ".join((term or "").split())
    if not t:
        return
    yield t
    stripped = _QUALIFIER.sub("", t)
    while stripped != t:                       # "metastatic advanced X"
        t, stripped = stripped, _QUALIFIER.sub("", stripped)
    if stripped != term:
        yield stripped
    base = stripped
    # Plural/singular. MeSH heads are plural for neoplasms, singular for most
    # diseases, and protocols pick whichever.
    if base.endswith("s"):
        yield base[:-1]
    else:
        yield base + "s"
    # "cancer" -> "neoplasms" is the single biggest vocabulary difference.
    if _CANCER.search(base):
        yield _CANCER.sub("neoplasms", base)
        yield _CANCER.sub("neoplasm", base)
    # "Overweight and Obesity" is two conditions, and both are MeSH headings.
    for part in re.split(r"\s+and\s+|\s*,\s*", base):
        if len(part.strip()) >= 4:
            yield part.strip()


# MEASURED AND NOT BUILT: expanding a condition to a MeSH heading it is a
# PREFIX of. It sounds like the obvious next tier - "Diabetes" reaching
# "Diabetes Mellitus" - and 75% of the 16,294 distinct prefixes do resolve to
# exactly one heading. The catch is that the frequent unmatched terms are all
# in the other 25%:
#
#   "diabetes"  -> 10 headings, including Diabetes Insipidus AND Diabetes
#                  Mellitus, which are unrelated diseases
#   "stress"    -> 8 headings
#   "asthma"    -> 4 headings
#
# So it would help the tail, where the terms are rare, and guess wrong on the
# head, where they are common. A wrong disease link is worse than none: it
# reads as a finding. Not built on purpose - if someone measures the 75% and
# proposes this again, this is the number that matters instead.
#
# "NSCLC" and "solid tumor" match no heading and no entry term at all, so
# they need a real abbreviation source, not a rewriting of what we have.


def main():
    want = sys.argv[1:] or ["ctgov"]
    mesh, icd = load_names()
    print(f"MeSH names+synonyms {len(mesh):,}   ICD-10/11 names {len(icd):,}\n")

    for reg in want:
        if reg not in SOURCES:
            print(f"{reg}: no condition column"); continue
        path, col = SOURCES[reg]
        n = no_text = by_mesh = by_icd = by_variant = unmatched = 0
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
            elif any(fold(v) in mesh for t in terms for v in variants(t)):
                by_variant += 1             # a rewriting would reach MeSH
            else:
                unmatched += 1
                for t in terms[:3]:
                    misses[t.strip()[:60]] += 1
        print(f"{reg}  {n:,} rows")
        print(f"   no condition text        {no_text:>9,} ({no_text/n:5.1%})")
        print(f"   matched by MeSH today    {by_mesh:>9,} ({by_mesh/n:5.1%})")
        print(f"   ICD would match, unused  {by_icd:>9,} ({by_icd/n:5.1%})  <-- the gap")
        print(f"   a rewriting would match  {by_variant:>9,} ({by_variant/n:5.1%})  <-- the real gap")
        print(f"   neither                  {unmatched:>9,} ({unmatched/n:5.1%})")
        print("   most common unmatched terms:")
        for term, c in misses.most_common(12):
            print(f"      {c:>7,}  {term!r}")
        print()


if __name__ == "__main__":
    main()
