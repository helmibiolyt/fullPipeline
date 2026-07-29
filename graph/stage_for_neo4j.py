#!/usr/bin/env python3
"""Prepare a build's CSVs for `neo4j-admin database import`.

    python graph/stage_for_neo4j.py --dir graph/final --out /var/lib/neo4j/import

Three things have to happen between a validated build and a successful import,
and each one was a failed import before it was a line of code here:

  1. **Drop the header row.** With a separate header file, neo4j-admin treats
     the data file as pure data - so the file's own header is parsed as a row
     and `enrollment` fails against `:int` on line 0.

  2. **Collapse embedded newlines.** Trial titles and recall reasons contain
     them. Python's csv writer quotes them correctly and Neo4j can read them
     with --multiline-fields=true, but that flag disables parallel parsing of
     the whole import. Replacing the newline with a space costs nothing and
     keeps one row on one line, which also makes the files greppable.

  3. **Enforce the declared types.** A column typed `:int` that contains
     anything else aborts the import after the node phase - 36 seconds in, with
     a byte offset for a message. ChiCTR writes enrollment per arm as prose
     ("Intervention group ( group B ):117;Control group ( group A ):117;"), so
     the value is real data that is simply not a number.

     Non-conforming values are blanked rather than guessed at. Summing that
     ChiCTR string to 234 would be inventing a total the registry never stated.
     Every blanked value is counted and reported, so the loss is visible.
"""
from __future__ import annotations

import argparse
import csv
import pathlib
import re
import sys

csv.field_size_limit(2**31 - 1)

# Must match TYPED in neo4j_import.py - the header says :int, so the data has
# to be an int, and this is the only place that can make that true.
TYPED = {
    "report_count": int, "serious_count": int, "death_count": int,
    "enrollment": int, "score": float, "max_phase": float,
}

_WS = re.compile(r"[\r\n\t]+")


def coerce(value: str, kind):
    v = (value or "").strip()
    if not v:
        return ""
    try:
        # int("4.0") raises; ChEMBL writes max_phase that way and it is a
        # legitimate number, so parse as float first and narrow.
        f = float(v)
        return str(int(f)) if kind is int else repr(f)
    except ValueError:
        return ""


def stage(src: pathlib.Path, dst: pathlib.Path) -> dict:
    dst.parent.mkdir(parents=True, exist_ok=True)
    blanked: dict[str, int] = {}
    rows = 0
    with src.open(encoding="utf-8", newline="") as f_in, \
         dst.open("w", encoding="utf-8", newline="") as f_out:
        r = csv.reader(f_in)
        w = csv.writer(f_out, lineterminator="\n")
        header = next(r, None)
        if header is None:
            return {"rows": 0}
        typed_at = {i: TYPED[c] for i, c in enumerate(header) if c in TYPED}
        for row in r:
            rows += 1
            out = [_WS.sub(" ", c) for c in row]
            for i, kind in typed_at.items():
                if i < len(out):
                    fixed = coerce(out[i], kind)
                    # Count only real loss. Comparing against the input catches
                    # reformatting too - "0.8700" -> "0.87", "4" -> "4.0" - and
                    # reporting those as blanked overstated the damage by an
                    # order of magnitude the first time this ran.
                    if out[i].strip() and not fixed:
                        blanked[header[i]] = blanked.get(header[i], 0) + 1
                    out[i] = fixed
            w.writerow(out)
    return {"rows": rows, "blanked": blanked}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="graph/final")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    src, out = pathlib.Path(a.dir), pathlib.Path(a.out)

    total = 0
    all_blanked: dict[str, int] = {}
    for kind in ("nodes", "edges"):
        for p in sorted((src / kind).glob("*.csv")):
            st = stage(p, out / kind / p.name)
            total += st["rows"]
            for k, v in st.get("blanked", {}).items():
                all_blanked[f"{p.stem}.{k}"] = all_blanked.get(f"{p.stem}.{k}", 0) + v
    print(f"staged {total:,} rows -> {out}")
    if all_blanked:
        print("values blanked for not matching their declared type:")
        for k, v in sorted(all_blanked.items(), key=lambda x: -x[1]):
            print(f"   {k:<34}{v:>9,}")
    else:
        print("every typed column conformed")


if __name__ == "__main__":
    main()
