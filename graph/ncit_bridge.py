#!/usr/bin/env python3
"""Would NCIt and CDISC synonyms link the trials MeSH cannot reach?

    python graph/ncit_bridge.py

The tail left after the three tiers in trials.py is protocol vocabulary MeSH
does not index: "Lung Cancer" where MeSH heads "Lung Neoplasms", "NSCLC",
"Solid Tumors". Those are not missing concepts and they are not rewritings of
a MeSH string either - they are a different vocabulary, and the lake already
holds four of them.

The safe way to use another vocabulary is as a BRIDGE, never as a new source
of nodes:

    take an NCIt concept's preferred name and its synonyms
    if ANY of them already resolves to a MeSH Disease
    then the OTHERS are aliases for that same Disease

Nothing new is created and nothing is guessed: a bridge only exists where the
two vocabularies already agree on one string. A concept that touches no MeSH
node contributes nothing.

This measures the win before anyone writes the loader. Reads the lake, prints,
changes nothing.
"""
from __future__ import annotations

import collections
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import disease as D                                      # noqa: E402
import lake                                              # noqa: E402
import trials as T                                       # noqa: E402
from normalise import condition_variants, fold           # noqa: E402

NCIT = "Ontologies_Standards/evs.nci.nih.gov/nci_thesaurus_data/nci_thesaurus_concepts.csv"
NEO = "Ontologies_Standards/evs.nci.nih.gov/nci_thesaurus_data/neoplasm_core.csv"
CDISC = [
    "Ontologies_Standards/cdisc.org/CDISC/data/SDTM Terminology.csv",
    "Ontologies_Standards/cdisc.org/CDISC/data/CDASH Terminology.csv",
    "Ontologies_Standards/cdisc.org/CDISC/data/Protocol Terminology.csv",
]

# A synonym this short or this generic bridges everything to everything.
MIN_ALIAS = 5


def mesh_dict():
    """Exactly what load_mesh builds, uncapped, so the measurement matches
    what the graph can actually do today."""
    mesh = {}
    for row in lake.stream_csv(D.L["mesh"]):
        name = (row.get("name") or "").strip()
        ui = (row.get("descriptor_ui") or "").strip()
        if not name or not ui:
            continue
        mesh.setdefault(fold(name), f"MESH:{ui}")
        for syn in (row.get("synonyms") or "").split(";"):
            f = fold(syn)
            if len(f) >= 4:
                mesh.setdefault(f, f"MESH:{ui}")
    return mesh


def _concepts():
    """(names, ...) per concept, from every vocabulary that has synonyms."""
    for row in lake.stream_csv(NCIT):
        names = [(row.get("preferred_name") or "").strip()]
        names += [s.strip() for s in (row.get("synonyms") or "").split("|")]
        yield [n for n in names if n]
    for row in lake.stream_csv(NEO):
        names = [(row.get("Preferred Term") or "").strip()]
        names += [s.strip() for s in (row.get("Synonyms") or "").split("|")]
        yield [n for n in names if n]
    for path in CDISC:
        try:
            for row in lake.stream_csv(path):
                names = [(row.get("NCI Preferred Term") or "").strip(),
                         (row.get("CDISC Submission Value") or "").strip()]
                names += [s.strip()
                          for s in (row.get("CDISC Synonym(s)") or "").split(";")]
                yield [n for n in names if n]
        except Exception as exc:                          # noqa: BLE001
            print(f"  (skipped {path}: {type(exc).__name__})")


def build_bridge(mesh):
    """folded alias -> MeSH key, only where the concept already met MeSH."""
    bridge, touched, orphan_concepts = {}, 0, 0
    for names in _concepts():
        hit = None
        for n in names:
            hit = mesh.get(fold(n))
            if hit:
                break
        if not hit:
            orphan_concepts += 1
            continue
        touched += 1
        for n in names:
            f = fold(n)
            if len(f) >= MIN_ALIAS and f not in mesh:
                bridge.setdefault(f, hit)
    return bridge, touched, orphan_concepts


def main():
    mesh = mesh_dict()
    bridge, touched, orphans = build_bridge(mesh)
    print(f"MeSH names+synonyms      {len(mesh):,}")
    print(f"concepts meeting MeSH    {touched:,}   (contributed aliases)")
    print(f"concepts meeting nothing {orphans:,}   (contributed none, by design)")
    print(f"NEW aliases              {len(bridge):,}\n")

    for probe in ("lung cancer", "nsclc", "solid tumor", "breast cancer",
                  "renal cell carcinoma", "diabetes"):
        print(f"   {probe!r:<24} -> {bridge.get(probe) or mesh.get(probe) or '-'}")

    n = before = now = 0
    misses = collections.Counter()
    for row in lake.stream_csv(T.L["ctgov"]):
        n += 1
        terms = T._terms(row.get("conditions", ""))
        if not terms:
            continue
        hit_now = any(fold(v) in mesh
                      for t in terms for v in condition_variants(t))
        hit_new = hit_now or any(fold(v) in bridge
                                 for t in terms for v in condition_variants(t))
        before += hit_now
        now += hit_new
        if not hit_new:
            for t in terms[:2]:
                misses[t.strip()[:52]] += 1
    print(f"\nct.gov {n:,} rows")
    print(f"   matched today            {before:,} ({before/n:5.1%})")
    print(f"   matched with the bridge  {now:,} ({now/n:5.1%})"
          f"   +{now-before:,}")
    print("   still unmatched, top:")
    for term, c in misses.most_common(10):
        print(f"      {c:>7,}  {term!r}")


if __name__ == "__main__":
    main()
