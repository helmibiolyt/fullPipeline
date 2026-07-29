#!/usr/bin/env python3
"""Turn a validated build into a neo4j-admin import.

    python graph/neo4j_import.py --dir graph/build --out graph/import

Produces three things and loads nothing:

    import/headers/<Label>.header.csv   typed column headers
    import/import.sh                    the neo4j-admin command line
    import/schema.cypher                constraints and indexes, run after

The header files are separate from the data on purpose. `neo4j-admin database
import` accepts `--nodes=header.csv,data.csv`, which means the typing lives in
a file this script owns and the build's CSVs are never rewritten - a full build
is tens of gigabytes and a second pass over it to prepend a header row would
cost more than the import itself.

Types matter here. Without `:int` on report_count, "12" imports as the string
"12", and `WHERE e.report_count > 100` silently compares strings - 9 sorts
above 100 and no error is ever raised.
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib

# Columns that are not strings. Everything absent from here imports as a
# string, which is the safe default: a date that fails to parse as a Neo4j date
# aborts the whole import, and these sources write at least four date formats.
TYPED = {
    "report_count": "int",
    "serious_count": "int",
    "death_count": "int",
    "score": "float",
    "enrollment": "int",
    "max_phase": "float",       # ChEMBL writes 4.0, and "" for unknown
}

# Property renames applied in the header only. `key` is the node id and `type`
# is a reserved-ish word that reads badly in Cypher next to :LABEL.
NODE_ID_COL = "key"


def header_for_nodes(label: str, columns: list[str]) -> list[str]:
    out = []
    for c in columns:
        if c == NODE_ID_COL:
            # One id space, named. The space is what stops a Product keyed
            # "CA:12345" colliding with anything else keyed the same way.
            out.append("key:ID(entity)")
        elif c in TYPED:
            out.append(f"{c}:{TYPED[c]}")
        else:
            out.append(c)
    return out


def header_for_edges(etype: str, columns: list[str]) -> list[str]:
    out = []
    for c in columns:
        if c == "src":
            out.append(":START_ID(entity)")
        elif c == "dst":
            out.append(":END_ID(entity)")
        elif c in TYPED:
            out.append(f"{c}:{TYPED[c]}")
        else:
            out.append(c)
    return out


SCHEMA = """// Run after the import completes:
//     cypher-shell -u neo4j -p <password> -f schema.cypher
//
// Constraints are created after bulk import, not before: neo4j-admin does not
// enforce them during load, and creating them first would cost an index build
// on every row as it lands.

// Uniqueness. `key` is globally unique by construction - the build's Writer
// deduplicates on it - so a violation here means the build is wrong.
{constraints}

// Lookups the agent will actually run. Names are the entry point for a
// question that starts from a drug or a disease rather than from an id.
CREATE INDEX substance_norm_name IF NOT EXISTS FOR (n:Substance) ON (n.norm_name);
CREATE INDEX substance_name      IF NOT EXISTS FOR (n:Substance) ON (n.name);
CREATE INDEX product_name        IF NOT EXISTS FOR (n:Product)   ON (n.name);
CREATE INDEX product_agency      IF NOT EXISTS FOR (n:Product)   ON (n.agency);
CREATE INDEX disease_name        IF NOT EXISTS FOR (n:Disease)   ON (n.name);
CREATE INDEX target_symbol       IF NOT EXISTS FOR (n:Target)    ON (n.symbol);
CREATE INDEX company_name        IF NOT EXISTS FOR (n:Company)   ON (n.name);
CREATE INDEX trial_registry      IF NOT EXISTS FOR (n:ClinicalTrial) ON (n.registry);
CREATE INDEX event_type          IF NOT EXISTS FOR (n:RegulatoryEvent) ON (n.type);
CREATE INDEX identifier_value    IF NOT EXISTS FOR (n:Identifier) ON (n.value);

// Full-text over the names a question arrives in. One index across the labels
// a user names a thing by, so "give me everything about Keytruda" is one call.
CREATE FULLTEXT INDEX entity_names IF NOT EXISTS
FOR (n:Substance|Product|Disease|Target|Company) ON EACH [n.name];
"""


# A shell line-continuation: space, one backslash, newline. Joined BETWEEN
# arguments rather than appended to each, because an appended continuation on
# the last argument would splice the following line into it.
CONT = " \\\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="graph/build")
    ap.add_argument("--out", default="graph/import")
    ap.add_argument("--database", default="biolyt")
    ap.add_argument("--data-path", default="/var/lib/neo4j/import",
                    help="where the CSVs will sit on the Neo4j host")
    a = ap.parse_args()

    d = pathlib.Path(a.dir)
    out = pathlib.Path(a.out)
    (out / "headers").mkdir(parents=True, exist_ok=True)
    man = json.loads((d / "manifest.json").read_text(encoding="utf-8"))

    node_args, edge_args, labels = [], [], []
    for p in sorted((d / "nodes").glob("*.csv")):
        with p.open(encoding="utf-8", newline="") as f:
            cols = next(csv.reader(f))
        hp = out / "headers" / f"{p.stem}.header.csv"
        hp.write_text(",".join(header_for_nodes(p.stem, cols)) + "\n",
                      encoding="utf-8")
        labels.append(p.stem)
        node_args.append(f"  --nodes={p.stem}="
                         f"{a.data_path}/headers/{p.stem}.header.csv,"
                         f"{a.data_path}/nodes/{p.name}")

    for p in sorted((d / "edges").glob("*.csv")):
        with p.open(encoding="utf-8", newline="") as f:
            cols = next(csv.reader(f))
        hp = out / "headers" / f"{p.stem}.header.csv"
        hp.write_text(",".join(header_for_edges(p.stem, cols)) + "\n",
                      encoding="utf-8")
        edge_args.append(f"  --relationships={p.stem}="
                         f"{a.data_path}/headers/{p.stem}.header.csv,"
                         f"{a.data_path}/edges/{p.name}")

    sh = f"""#!/usr/bin/env bash
# Generated from run {man.get('run_id')} - do not hand-edit, regenerate.
#
# neo4j-admin import writes a fresh store: it cannot add to a running database.
# Stop Neo4j, import, start it, then apply schema.cypher.
set -euo pipefail

sudo systemctl stop neo4j

sudo -u neo4j neo4j-admin database import full {a.database} \\
{CONT.join(node_args + edge_args)} \\
  --id-type=string \\
  --skip-bad-relationships=false \\
  --skip-duplicate-nodes=false \\
  --bad-tolerance=0 \\
  --high-parallel-io=on \\
  --overwrite-destination=true

# --bad-tolerance=0 and --skip-bad-relationships=false are deliberate. The
# validator already proved every endpoint resolves, so a bad relationship here
# means the files changed between validating and importing, and a partial
# import that silently drops edges is worse than a failed one.

sudo systemctl start neo4j
echo "waiting for neo4j"
until cypher-shell -u neo4j -p "$NEO4J_PASSWORD" "RETURN 1" >/dev/null 2>&1; do sleep 3; done
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" -d {a.database} -f {a.data_path}/schema.cypher
echo "done"
"""
    (out / "import.sh").write_text(sh, encoding="utf-8")
    (out / "import.sh").chmod(0o755)

    cons = "\n".join(
        f"CREATE CONSTRAINT {l.lower()}_key IF NOT EXISTS "
        f"FOR (n:{l}) REQUIRE n.key IS UNIQUE;" for l in labels)
    (out / "schema.cypher").write_text(SCHEMA.format(constraints=cons),
                                       encoding="utf-8")

    print(f"{len(node_args)} node files, {len(edge_args)} relationship files")
    print(f"-> {out}/import.sh")
    print(f"-> {out}/schema.cypher   ({len(labels)} constraints)")


if __name__ == "__main__":
    main()
