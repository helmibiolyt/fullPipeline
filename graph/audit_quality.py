#!/usr/bin/env python3
"""Audit the live graph for data-quality defects the build validator misses.

    python graph/audit_quality.py                  # everything
    python graph/audit_quality.py --label Target   # one label
    python graph/audit_quality.py --nodes          # skip relationships
    python graph/audit_quality.py --quick          # skip the expensive scans

The validator checks STRUCTURE - every edge endpoint resolves, keys are
unique, declared files were read. It passes on a graph where a property is
declared and never populated, or where one concept is spelled twelve ways, or
where a node has no relationships at all. Each of those looks like an absence
of data at query time rather than a defect, so nobody reports it.

NODE CHECKS
  EMPTY         a declared property null on every node
  SPARSE        filled on under 5%
  UNNORMALISED  one value written several ways - 'PHASE3', '3', 'Phase 3'
  NUMERIC       numbers stored as strings, so 9 sorts above 100
  PLACEHOLDER   'N/A', 'unknown', '-', 'null' stored as if it were a value
  WHITESPACE    leading/trailing space or embedded newlines
  KEYFORM       keys that are not NAMESPACE:VALUE
  ORPHAN        nodes with no relationship in either direction
  DUPNAME       one name held by many keys - possible failed merge

RELATIONSHIP CHECKS
  EMPTY_EDGE    a declared type with no relationships
  ENDPOINTS     endpoint labels that disagree with the documented schema
  SELFLOOP      a node related to itself
  DUPEDGE       the same pair connected more than once by the same type
  METHOD        the match_method mix, so weak evidence is visible
  DEGREE        nodes absorbing an implausible number of edges

Reads only. Nothing here writes to the graph.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from emit import NODE_COLUMNS, EDGE_COLUMNS       # noqa: E402
from make_tech_doc import EDGES                   # noqa: E402

try:
    from dotenv import load_dotenv
    load_dotenv(pathlib.Path(__file__).resolve().parent.parent
                / "testPipeline" / ".env")
except ImportError:
    pass

URI = os.getenv("NEO4J_URI", "bolt://4.233.210.24:7687")
USER = os.getenv("NEO4J_USER", "neo4j")
PWD = os.getenv("NEO4J_PASSWORD", "")
DB = os.getenv("NEO4J_DATABASE", "biolyt")

FREE_TEXT = {"title", "synonyms", "abstract", "definition", "use_definition",
             "reason", "url", "value", "mesh_heading", "matched_name",
             "journal", "raw_names"}

# Values that mean "we do not know" but are stored as if they were data. A
# query filtering on them returns rows that assert nothing.
PLACEHOLDERS = {"n/a", "na", "none", "null", "nil", "unknown", "-", "--",
                "not applicable", "not specified", "not available", "unknown",
                "n.a.", "?", "tbd", "no data", "not stated", "", "0000-00-00"}

# Columns where a placeholder-looking value is the correct value, and
# flagging it trains the reader to skim past this section.
#
#   Country.iso2 'NA' is NAMIBIA. It is the single most convincing false
#   positive this script can produce, because it looks exactly like the
#   defect it is meant to catch.
#
#   ClinicalTrial.phase and study_type carry 'NA' by design, so that a trial
#   with no phase and a trial nobody gave a phase read the same way instead
#   of one being null. Product.status does the same.
DELIBERATE_NA = {
    ("Country", "iso2"),
    ("ClinicalTrial", "phase"),
    ("ClinicalTrial", "study_type"),
    ("Product", "status"),
}

ENUM_MAX_DISTINCT = 400
BIG = 500_000          # above this, expensive scans are sampled

_TTY = sys.stdout.isatty()
def _c(code, s): return f"\033[{code}m{s}\033[0m" if _TTY else s
def dim(s):   return _c("2", s)
def bold(s):  return _c("1", s)
def red(s):   return _c("38;5;203", s)
def amber(s): return _c("38;5;214", s)
def green(s): return _c("38;5;78", s)
def blue(s):  return _c("38;5;75", s)
def violet(s):return _c("38;5;141", s)


def fold(v) -> str:
    """Comparison form. Two raw values folding together but written
    differently are the defect this script exists to find. Numbers are kept
    numeric - stripping punctuation made -1.0 and 1.0 collide."""
    if isinstance(v, (int, float)):
        return f"num:{v}"
    s = str(v).strip()
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        return f"num:{float(s)}"
    return re.sub(r"[^a-z0-9]+", "", s.lower())


class Audit:
    def __init__(self, drv, quick=False):
        self.drv = drv
        self.quick = quick
        self.f = defaultdict(list)

    def q(self, cypher, **kw):
        with self.drv.session(database=DB, default_access_mode="READ") as s:
            return [dict(r) for r in s.run(cypher, **kw)]

    def one(self, cypher, key, default=0, **kw):
        r = self.q(cypher, **kw)
        return r[0][key] if r else default

    def add(self, kind, where, detail):
        self.f[kind].append((where, detail))

    # ---------------------------------------------------------------- nodes
    def node_label(self, label):
        props = [c for c in NODE_COLUMNS[label] if c != "key"]
        total = self.one(f"MATCH (n:{label}) RETURN count(n) AS n", "n")
        if not total:
            self.add("EMPTY_LABEL", label, "no nodes at all")
            print(red(f"  {label:<20} 0 nodes"))
            return
        print(f"  {blue(label):<30} {total:>11,}")

        self._keyform(label, total)
        self._orphans(label, total)
        self._dupnames(label, total)
        for p in props:
            self._prop(label, p, total)

    def _keyform(self, label, total):
        bad = self.one(
            f"MATCH (n:{label}) WHERE NOT n.key CONTAINS ':' "
            f"RETURN count(n) AS n", "n")
        if bad:
            ex = self.q(f"MATCH (n:{label}) WHERE NOT n.key CONTAINS ':' "
                        f"RETURN n.key AS k LIMIT 3")
            self.add("KEYFORM", label,
                     f"{bad:,} keys without a namespace, e.g. "
                     + ", ".join(repr(r["k"]) for r in ex))
            print(red(f"      {'key':<24} {bad:,} not NAMESPACE:VALUE"))

    def _orphans(self, label, total):
        if self.quick:
            return
        # COUNT{} is index-free but cheap per node; on the huge labels this
        # still scans, so sample rather than wait minutes for a known answer.
        if total > BIG:
            n = self.one(
                f"MATCH (n:{label}) WITH n LIMIT 200000 "
                f"WHERE COUNT {{ (n)--() }} = 0 RETURN count(n) AS n", "n")
            if n:
                pct = n / 200000 * 100
                self.add("ORPHAN", label,
                         f"~{pct:.1f}% of a 200k sample have no relationship "
                         f"(~{int(total*pct/100):,} of {total:,})")
                print(amber(f"      {'(orphans)':<24} ~{pct:.1f}% sampled"))
            return
        n = self.one(f"MATCH (n:{label}) WHERE COUNT {{ (n)--() }} = 0 "
                     f"RETURN count(n) AS n", "n")
        if n:
            self.add("ORPHAN", label,
                     f"{n:,} of {total:,} ({n/total*100:.1f}%) have no "
                     f"relationship")
            print(amber(f"      {'(orphans)':<24} {n:,} ({n/total*100:.1f}%)"))

    def _dupnames(self, label, total):
        if self.quick or "name" not in NODE_COLUMNS[label] or total > BIG:
            return
        rows = self.q(
            f"MATCH (n:{label}) WHERE n.name IS NOT NULL AND n.name <> '' "
            f"WITH toLower(trim(n.name)) AS nm, count(*) AS c, "
            f"collect(n.key)[..3] AS keys WHERE c > 1 "
            f"RETURN nm, c, keys ORDER BY c DESC LIMIT 5")
        if rows:
            tot = self.one(
                f"MATCH (n:{label}) WHERE n.name IS NOT NULL AND n.name <> '' "
                f"WITH toLower(trim(n.name)) AS nm, count(*) AS c "
                f"WHERE c > 1 RETURN count(*) AS n", "n")
            worst = "; ".join(f"{r['nm'][:34]!r}×{r['c']}" for r in rows[:3])
            self.add("DUPNAME", label,
                     f"{tot:,} names held by more than one key — {worst}")
            print(amber(f"      {'(duplicate names)':<24} {tot:,} names"))

    def _prop(self, label, p, total):
        filled = self.one(
            f"MATCH (n:{label}) WHERE n.`{p}` IS NOT NULL AND n.`{p}` <> '' "
            f"RETURN count(n) AS n", "n")
        pct = filled / total * 100 if total else 0
        if filled == 0:
            self.add("EMPTY", f"{label}.{p}", f"0 of {total:,}")
            print(red(f"      {p:<24} EMPTY  0 / {total:,}"))
            return
        if pct < 5:
            self.add("SPARSE", f"{label}.{p}",
                     f"{filled:,} of {total:,} ({pct:.1f}%)")
            print(amber(f"      {p:<24} sparse {pct:5.1f}%"))

        # valueType() guards the string functions. enrollment is a Long, and
        # trim() on it aborts the entire query rather than skipping the row.
        ws = self.one(
            f"MATCH (n:{label}) WHERE n.`{p}` IS NOT NULL "
            f"AND valueType(n.`{p}`) STARTS WITH 'STRING' AND "
            f"(n.`{p}` <> trim(n.`{p}`) OR n.`{p}` CONTAINS '\\n' "
            f" OR n.`{p}` CONTAINS '\\t') RETURN count(n) AS n", "n")
        if ws:
            self.add("WHITESPACE", f"{label}.{p}",
                     f"{ws:,} values with leading/trailing space or newlines")
            print(amber(f"      {p:<24} whitespace in {ws:,}"))

        d = self.one(f"MATCH (n:{label}) WHERE n.`{p}` IS NOT NULL "
                     f"RETURN count(DISTINCT n.`{p}`) AS d", "d")
        if d < 2 or d > ENUM_MAX_DISTINCT:
            return
        vals = self.q(f"MATCH (n:{label}) WHERE n.`{p}` IS NOT NULL "
                      f"RETURN n.`{p}` AS v, count(*) AS n "
                      f"ORDER BY n DESC LIMIT {ENUM_MAX_DISTINCT}")

        junk = [(r["v"], r["n"]) for r in vals
                if str(r["v"]).strip().lower() in PLACEHOLDERS
                and (label, p) not in DELIBERATE_NA]
        if junk:
            tot = sum(n for _, n in junk)
            self.add("PLACEHOLDER", f"{label}.{p}",
                     f"{tot:,} rows hold a non-value: "
                     + ", ".join(f"{v!r}({n:,})" for v, n in junk[:5]))
            print(amber(f"      {p:<24} placeholder in {tot:,}"))

        if p in FREE_TEXT:
            return
        groups = defaultdict(list)
        for r in vals:
            groups[fold(r["v"])].append((r["v"], r["n"]))
        dupes = {k: v for k, v in groups.items() if len(v) > 1}
        if dupes:
            worst = sorted(dupes.values(),
                           key=lambda g: -sum(n for _, n in g))[:3]
            self.add("UNNORMALISED", f"{label}.{p}",
                     f"{len(dupes)} collisions of {d} values — " + " | ".join(
                         ", ".join(f"{v!r}({n:,})" for v, n in g)
                         for g in worst))
            print(red(f"      {p:<24} UNNORMALISED {len(dupes)} collisions"))
            for g in worst[:2]:
                print(dim("          " + ", ".join(
                    f"{v!r} ({n:,})" for v, n in g)[:94]))

        numeric = [r for r in vals if isinstance(r["v"], str)
                   and re.fullmatch(r"-?\d+(\.\d+)?", r["v"].strip())]
        if numeric and len(numeric) == len(vals) and len(vals) > 3:
            self.add("NUMERIC", f"{label}.{p}",
                     f"{d} distinct values, all numeric, stored as text")
            print(amber(f"      {p:<24} numbers stored as text"))

    # -------------------------------------------------------------- edges
    def edge_type(self, etype):
        n = self.one(f"MATCH ()-[r:{etype}]->() RETURN count(r) AS n", "n")
        if n == 0:
            self.add("EMPTY_EDGE", etype, "declared but never written")
            print(red(f"  {etype:<24} 0 relationships"))
            return
        print(f"  {violet(etype):<34} {n:>11,}")

        for p in [c for c in EDGE_COLUMNS[etype]
                  if c not in ("src", "dst", "match_method")]:
            f_ = self.one(f"MATCH ()-[r:{etype}]->() WHERE r.`{p}` IS NOT NULL "
                          f"AND toString(r.`{p}`) <> '' RETURN count(r) AS n",
                          "n")
            if f_ == 0:
                self.add("EMPTY", f":{etype}.{p}", f"0 of {n:,}")
                print(red(f"      {p:<24} EMPTY 0 / {n:,}"))

        methods = self.q(f"MATCH ()-[r:{etype}]->() RETURN r.match_method AS m, "
                         f"count(*) AS n ORDER BY n DESC LIMIT 8")
        weak = sum(r["n"] for r in methods
                   if r["m"] in ("name", "provisional"))
        if weak:
            self.add("METHOD", etype,
                     f"{weak:,} of {n:,} ({weak/n*100:.0f}%) rest on "
                     f"name or provisional matching")
            print(dim(f"      {'match_method':<24} "
                      + ", ".join(f"{r['m']}={r['n']:,}" for r in methods)))

        # Endpoint labels, against the documented schema.
        want = EDGES.get(etype)
        pairs = self.q(
            f"MATCH (a)-[r:{etype}]->(b) WITH a, b LIMIT 40000 "
            f"RETURN labels(a)[0] AS src, labels(b)[0] AS dst, "
            f"count(*) AS n ORDER BY n DESC LIMIT 6")
        if want:
            ok_src = {x.strip() for x in want[0].replace("|", " ").split()}
            ok_dst = {x.strip() for x in want[1].replace("|", " ").split()}
            bad = [p for p in pairs
                   if (ok_src != {"any"} and p["src"] not in ok_src)
                   or (ok_dst != {"any"} and p["dst"] not in ok_dst)]
            if bad:
                self.add("ENDPOINTS", etype,
                         f"documented as {want[0]} -> {want[1]}, found "
                         + ", ".join(f"{p['src']}->{p['dst']}({p['n']:,})"
                                     for p in bad[:3]))
                print(amber(f"      {'endpoints':<24} "
                            + ", ".join(f"{p['src']}->{p['dst']}"
                                        for p in bad[:3])))

        loops = self.one(f"MATCH (a)-[r:{etype}]->(a) RETURN count(r) AS n",
                         "n")
        if loops:
            self.add("SELFLOOP", etype, f"{loops:,} nodes related to themselves")
            print(amber(f"      {'self-loops':<24} {loops:,}"))

        if not self.quick:
            dup = self.q(
                f"MATCH (a)-[r:{etype}]->(b) WITH a, b, count(r) AS c "
                f"WHERE c > 1 RETURN count(*) AS pairs, max(c) AS worst")
            if dup and dup[0]["pairs"]:
                self.add("DUPEDGE", etype,
                         f"{dup[0]['pairs']:,} pairs connected more than once "
                         f"(worst {dup[0]['worst']}×)")
                print(amber(f"      {'duplicate pairs':<24} "
                            f"{dup[0]['pairs']:,}"))

            deg = self.q(
                f"MATCH (a)-[r:{etype}]->() WITH a, count(r) AS c "
                f"ORDER BY c DESC LIMIT 3 "
                f"RETURN a.key AS k, coalesce(a.name, a.term, a.title, '') "
                f"AS nm, c")
            if deg and deg[0]["c"] > 5000:
                self.add("DEGREE", etype,
                         "highest fan-out " + ", ".join(
                             f"{r['k']}({r['nm'][:26]}) {r['c']:,}"
                             for r in deg[:2]))
                print(amber(f"      {'max fan-out':<24} "
                            f"{deg[0]['k']} {deg[0]['c']:,}"))


ORDER = ["EMPTY_LABEL", "EMPTY_EDGE", "EMPTY", "UNNORMALISED", "KEYFORM",
         "ENDPOINTS", "PLACEHOLDER", "NUMERIC", "WHITESPACE", "SELFLOOP",
         "DUPEDGE", "DUPNAME", "ORPHAN", "DEGREE", "SPARSE", "METHOD"]
SERIOUS = {"EMPTY_LABEL", "EMPTY_EDGE", "EMPTY", "UNNORMALISED", "KEYFORM",
           "ENDPOINTS"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label")
    ap.add_argument("--edge")
    ap.add_argument("--nodes", action="store_true", help="skip relationships")
    ap.add_argument("--edges", action="store_true", help="skip nodes")
    ap.add_argument("--quick", action="store_true",
                    help="skip orphan, duplicate and fan-out scans")
    a = ap.parse_args()

    if not PWD:
        sys.exit("NEO4J_PASSWORD not set (testPipeline/.env or environment)")

    from neo4j import GraphDatabase
    drv = GraphDatabase.driver(URI, auth=(USER, PWD))
    au = Audit(drv, quick=a.quick)

    if not a.edges:
        labels = [a.label] if a.label else sorted(NODE_COLUMNS)
        print(bold(f"\nNODES  ({len(labels)} labels)\n"))
        for l in labels:
            au.node_label(l)

    if not a.nodes:
        etypes = [a.edge] if a.edge else sorted(EDGE_COLUMNS)
        print(bold(f"\nRELATIONSHIPS  ({len(etypes)} types)\n"))
        for e in etypes:
            au.edge_type(e)

    print()
    print(bold("=" * 72))
    print(bold("SUMMARY"))
    n_serious = sum(len(au.f[k]) for k in SERIOUS)
    for k in ORDER:
        rows = au.f.get(k)
        if not rows:
            continue
        col = red if k in SERIOUS else amber
        print(col(f"\n  {k}  ({len(rows)})"))
        for where, detail in rows:
            print(f"    {where:<30} {detail[:110]}")
    print()
    print(bold(f"  {n_serious} defects needing a fix, "
               f"{sum(len(v) for v in au.f.values()) - n_serious} "
               f"observations"))
    drv.close()


if __name__ == "__main__":
    main()
