#!/usr/bin/env python3
"""Which alias strings bridge to a given MeSH disease?

    python graph/check_alias.py "Anxiety Disorders"

The bridge is built at load time and not stored, so when a tier links trials
somewhere surprising there is no way to see WHY from the graph. This rebuilds
the same dictionaries and prints the strings that point at one node.

Written after the bridge put 5,027 trials on "Anxiety Disorders", including a
trial of a multimedia distraction system during a hospital procedure. That is
procedural anxiety, a symptom, and MeSH has a separate descriptor for it.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import disease as D                                      # noqa: E402
import lake                                              # noqa: E402
from normalise import fold                               # noqa: E402


def main() -> None:
    target = " ".join(sys.argv[1:]) or "Anxiety Disorders"
    tf = fold(target)

    mesh, name_of = {}, {}
    for row in lake.stream_csv(D.L["mesh"]):
        ui = (row.get("descriptor_ui") or "").strip()
        nm = (row.get("name") or "").strip()
        trees = [t.strip()
                 for t in (row.get("tree_numbers") or "").split(";") if t.strip()]
        if not ui or not nm:
            continue
        if not any(t.startswith("C") or t.startswith("F03") for t in trees):
            continue
        key = "MESH:" + ui
        name_of[key] = nm
        mesh.setdefault(fold(nm), key)
        for syn in (row.get("synonyms") or "").split(";"):
            f = fold(syn)
            if len(f) >= 4:
                mesh.setdefault(f, key)

    want = mesh.get(tf)
    print(f"target {target!r} -> {want}")
    if not want:
        print("not a Disease node in this build")
        return

    # Does MeSH hold a NARROWER descriptor the aliases should have gone to?
    print()
    print("MeSH descriptors whose name contains the same head word:")
    head = tf.split()[0]
    for f, k in mesh.items():
        if k in name_of and fold(name_of[k]).startswith(head):
            print(f"   {k:<18} {name_of[k]!r}")

    print()
    print("alias strings the bridge points at this node:")
    shown = 0
    for names in D._vocab_concepts():
        hit = None
        for x in names:
            hit = mesh.get(fold(x))
            if hit:
                break
        if hit != want:
            continue
        for x in names:
            f = fold(x)
            if len(f) >= D.MIN_ALIAS and f not in mesh and shown < 30:
                print(f"   {x[:70]!r}")
                shown += 1


if __name__ == "__main__":
    main()
