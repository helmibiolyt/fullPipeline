#!/usr/bin/env python3
"""Check a build before anyone trusts it.

    python graph/validate.py --dir graph/build

The point of building to files is that every claim about the graph is a claim
about a table, and can be checked without a database. These checks are ordered
by how badly a failure would mislead someone:

  1. referential integrity - an edge to a node that does not exist becomes,
     in Neo4j, a silently missing row rather than an error
  2. label collision    - one key under two labels merges two things
  3. duplicate substance - the same molecule as two nodes is the failure the
     whole resolver exists to prevent, and it is invisible in totals
  4. resolution quality  - what fraction of edges came from a dictionary match
     rather than a stated fact, per source
  5. fan-out outliers    - a node with 100k edges is usually a parse bug
  6. fixtures            - known biology that must survive any refactor

Exit code is non-zero if a FAIL-level check fails, so this can gate an import.
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import pathlib
import sys

csv.field_size_limit(2**31 - 1)

FAILS: list[str] = []
WARNS: list[str] = []


def _read(path: pathlib.Path):
    with path.open(encoding="utf-8", newline="") as f:
        yield from csv.DictReader(f)


def fail(msg):
    FAILS.append(msg)
    print(f"  FAIL  {msg}")


def warn(msg):
    WARNS.append(msg)
    print(f"  warn  {msg}")


def ok(msg):
    print(f"  ok    {msg}")


# Known biology. If a refactor breaks one of these it broke the graph, whatever
# the totals say. Kept small and specific on purpose.
FIXTURES = [
    ("UNII:C0GEJ5QCSO", "TARGETS", "UNIPROT:P04035",
     "atorvastatin -> HMG-CoA reductase"),
    ("UNII:DPT0O3T46P", "TARGETS", "UNIPROT:Q15116",
     "pembrolizumab -> PD-1"),
    ("UNII:I5I8VB78VT", "TARGETS", "UNIPROT:Q16602",
     "erenumab -> CGRP receptor"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="graph/build")
    ap.add_argument("--max-mem-gb", type=float, default=0,
                    help="self-imposed ceiling; 0 disables")
    a = ap.parse_args()
    if a.max_mem_gb:
        # Same reasoning as build.py, and learned the same way: this now runs
        # on a host where Neo4j holds 8 GB, and without a ceiling the kernel
        # picks the OOM victim. It picked badly enough to reboot the box.
        import resource
        cap = int(a.max_mem_gb * 1024 ** 3)
        resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
    d = pathlib.Path(a.dir)
    if not (d / "manifest.json").exists():
        sys.exit(f"no build at {d}")
    man = json.loads((d / "manifest.json").read_text(encoding="utf-8"))

    # ---- load keys -------------------------------------------------------
    #
    # Keys only, plus small per-label tallies. An earlier version kept every
    # row - 11.5M dicts - which on the graph host meant several GB next to a
    # Neo4j holding 8, and the box went to swap hard enough to drop SSH. The
    # checks below never needed whole rows: they need the key set, the
    # Substance name/resolution columns, and counters.
    keys_by_label: dict[str, set] = {}
    substance_norm: dict[str, set] = collections.defaultdict(set)
    resolved_by = collections.Counter()
    codeish = collections.Counter()      # label -> name equals its own code
    blank_name = collections.Counter()
    label_rows = collections.Counter()
    for p in sorted((d / "nodes").glob("*.csv")):
        label = p.stem
        ks = set()
        for r in _read(p):
            key = r["key"]
            ks.add(key)
            label_rows[label] += 1
            if label == "Substance":
                resolved_by[r.get("resolved_by") or "?"] += 1
                nn = (r.get("norm_name") or "").strip()
                if nn:
                    substance_norm[nn].add(key)
            name = (r.get("name") or "").strip()
            if not name:
                blank_name[label] += 1
            elif name in ((r.get("iso2") or "").strip(),
                          (r.get("code") or "").strip(),
                          key.split(":", 1)[-1].strip()):
                codeish[label] += 1
        keys_by_label[label] = ks
    # Only names shared by more than one substance are of interest; dropping
    # the singletons here is what keeps this dict small on a 3M-node build.
    substance_norm = {k: v for k, v in substance_norm.items() if len(v) > 1}
    all_keys = set().union(*keys_by_label.values()) if keys_by_label else set()
    print(f"\n{sum(len(v) for v in keys_by_label.values()):,} nodes / "
          f"{len(keys_by_label)} labels   run {man.get('run_id')}\n")

    # ---- 1. referential integrity ---------------------------------------
    print("referential integrity")
    total_edges = 0
    for p in sorted((d / "edges").glob("*.csv")):
        missing = collections.Counter()
        n = 0
        for r in _read(p):
            n += 1
            for end in (r["src"], r["dst"]):
                if end not in all_keys:
                    missing[end.split(":", 1)[0]] += 1
        total_edges += n
        if missing:
            fail(f"{p.stem}: {sum(missing.values()):,}/{n:,} dangling endpoints "
                 f"-> {dict(missing.most_common(4))}")
        else:
            ok(f"{p.stem}: {n:,} edges, all endpoints resolve")

    # ---- 2. label collision ---------------------------------------------
    print("\nkey uniqueness across labels")
    owner: dict[str, str] = {}
    clashes = collections.Counter()
    for label, ks in keys_by_label.items():
        for k in ks:
            if k in owner and owner[k] != label:
                clashes[(owner[k], label)] += 1
            else:
                owner[k] = label
    if clashes:
        for (l1, l2), c in clashes.most_common(5):
            # Identifier deliberately shares its key with nothing else; any
            # other pair means two different things collapsed into one node.
            fail(f"{c:,} keys are both {l1} and {l2}")
    else:
        ok("no key is used by two labels")

    # ---- 3. duplicate substances ----------------------------------------
    print("\nsubstance resolution")
    by_method = resolved_by
    dupes = substance_norm
    if dupes:
        sample = list(dupes.items())[:3]
        warn(f"{len(dupes):,} normalised names map to >1 Substance node "
             f"(e.g. {sample[0][0]!r} -> {sorted(sample[0][1])[:3]})")
    else:
        ok("no two Substance nodes share a normalised name")
    tot = sum(by_method.values()) or 1
    prov = by_method.get("provisional", 0)
    print(f"        {dict(by_method.most_common())}")
    if prov / tot > 0.25:
        warn(f"{prov / tot:.1%} of substances are provisional (unresolved names)")
    else:
        ok(f"provisional substances {prov:,}/{tot:,} = {prov / tot:.1%}")

    # ---- 4. match quality per edge type ---------------------------------
    print("\nhow edges were established")
    for p in sorted((d / "edges").glob("*.csv")):
        m = collections.Counter(r.get("match_method") or "?" for r in _read(p))
        n = sum(m.values())
        inferred = sum(c for k, c in m.items()
                       if k in ("name", "symbol", "provisional", "stereo"))
        flag = "  <- mostly inferred" if n and inferred / n > 0.5 else ""
        print(f"        {p.stem:20} {n:>9,}  {dict(m.most_common(4))}{flag}")

    # ---- 5. fan-out outliers --------------------------------------------
    print("\nfan-out outliers")
    worst = []
    for p in sorted((d / "edges").glob("*.csv")):
        deg = collections.Counter()
        for r in _read(p):
            deg[r["src"]] += 1
        if deg:
            k, c = deg.most_common(1)[0]
            worst.append((c, p.stem, k))
    for c, etype, k in sorted(worst, reverse=True)[:6]:
        # A hub is normal (one agency approves every product). A hub on an
        # edge type that should be sparse is a parse bug.
        print(f"        {etype:20} max out-degree {c:>8,}  {k}")

    # ---- 6. fixtures -----------------------------------------------------
    #
    # A slice only contains the molecules it was asked for, so a fixture about
    # pembrolizumab cannot pass on a build sliced to atorvastatin. Failing it
    # anyway made a correct slice build report two failures - and since
    # build-graph.sh gates the import on this exit code, that teaches people to
    # ignore validation failures, which is the one habit these checks exist to
    # prevent.
    #
    # Absent subject means out of scope; present subject with the edge missing
    # is a real defect and still fails. On a full build nothing is out of
    # scope, so every fixture is enforced.
    print("\nfixtures")
    sliced = man.get("mode") == "slice"
    checked = 0
    for src, etype, dst, label in FIXTURES:
        p = d / "edges" / f"{etype}.csv"
        if not p.exists():
            # A slice can legitimately produce no edges of a type. A full build
            # cannot: no TARGETS.csv at all means the mechanisms loader emitted
            # nothing, which is a broken build wearing a warning.
            (warn if sliced else fail)(f"{label}: no {etype} table at all")
            continue
        if sliced and src not in all_keys:
            print(f"  skip  {label}  (subject not in this slice)")
            continue
        checked += 1
        hit = any(r["src"] == src and r["dst"] == dst for r in _read(p))
        (ok if hit else fail)(f"{label}")
    if sliced and not checked:
        # Otherwise a fixtures section of nothing but skips reads as a pass,
        # and the check silently stops covering anything. Slice-only: on a full
        # build an unchecked fixture has already failed above, and adding "out
        # of scope for this slice" there is both wrong and noise.
        warn(f"no fixture was actually checked - all {len(FIXTURES)} were out "
             f"of scope for this slice")

    # ---- 7. declared sources that were never read ------------------------
    #
    # sources.py is the statement of what feeds the graph. Five files sat in it
    # with no loader for a while and nothing noticed, because a relationship
    # that is never emitted does not look wrong in the totals - it looks like
    # sparse data. IN_CLASS had no Substance source at all and simply appeared
    # small.
    #
    # The manifest records the S3 key of every file actually read, so this is
    # an exact comparison rather than a guess at whether some loader mentions a
    # path somewhere.
    print("\ndeclared sources actually read")
    try:
        import sources as _sources
        declared = {d["file"] for d in _sources.INCLUDED}
        read = set(man.get("files_read", []))
        never = sorted(declared - read)
        # A slice legitimately skips sources it cannot filter - eu_ctr has no
        # substance column - so an unread file is only a failure on a full run.
        report = warn if man.get("mode") == "slice" else fail
        if never:
            report(f"{len(never)}/{len(declared)} declared sources were never read"
                   + ("  (slice mode)" if man.get("mode") == "slice" else ""))
            for f in never[:10]:
                print(f"          {f}")
        else:
            ok(f"all {len(declared)} declared sources were read")
        extra = sorted(read - declared)
        if extra:
            warn(f"{len(extra)} files read but not declared in sources.py: "
                 f"{extra[:3]}")
    except Exception as e:
        warn(f"source coverage check skipped: {type(e).__name__}: {e}")

    # ---- 8. names that are not names ------------------------------------
    #
    # COUNTRY:SA carried name="SA". Every count was right, referential
    # integrity was clean, and the node was still useless to anyone reading it
    # - the label loader wrote the ISO code into both fields, and because the
    # Writer keeps the first writer, the later loader that had the real name
    # never got to correct it. It affected exactly the ten regulatory-agency
    # countries, which is every GCC one.
    #
    # A display name equal to the node's own code is the detectable form of
    # that mistake.
    print("\nnames that are just the code again")
    # Only labels that declare a `name` column. AdverseEvent carries `term`,
    # ClinicalTrial `title`, Identifier `scheme`/`value` - warning that those
    # have no name is noise, and noise is what stops anyone reading warnings.
    try:
        from emit import NODE_COLUMNS
        named = {l for l, c in NODE_COLUMNS.items() if "name" in c}
    except Exception:
        named = set(keys_by_label)

    clean = True
    for label in sorted(keys_by_label):
        if label not in named:
            continue
        n = label_rows[label] or 1
        if codeish[label]:
            warn(f"{label}: {codeish[label]:,}/{n:,} nodes have name == their own code")
            clean = False
        if blank_name[label] and blank_name[label] / n > 0.5:
            warn(f"{label}: {blank_name[label]:,}/{n:,} nodes have no name at all")
            clean = False
    if clean:
        ok("every label's name property carries a name")

    # ---- 9. isolated nodes ----------------------------------------------
    print("\nconnectivity")
    touched = set()
    for p in (d / "edges").glob("*.csv"):
        for r in _read(p):
            touched.add(r["src"])
            touched.add(r["dst"])
    for label, ks in sorted(keys_by_label.items()):
        iso = len(ks - touched)
        if iso and iso / len(ks) > 0.5:
            warn(f"{label}: {iso:,}/{len(ks):,} nodes have no edge at all")

    print(f"\n{total_edges:,} edges checked")
    if man.get("stats"):
        print(f"stats: {json.dumps(man['stats'])[:400]}")
    print(f"\n{len(FAILS)} failures, {len(WARNS)} warnings")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
