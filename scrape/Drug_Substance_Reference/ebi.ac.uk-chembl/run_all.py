#!/usr/bin/env python3
"""Manifest entrypoint for ChEMBL: fetch the bulk DB, then export tables to CSV.

  1. chembl_downloader.py download-db  -> chembl_data/chembl_XX_sqlite/**/chembl_XX.db
  2. this script                       -> chembl_data/*.csv

`download-db` only ever produced a ~20 GB SQLite file. The pipeline publishes
CSV and documents, so a run that stopped there uploaded nothing and failed at
`collect` with "produced no artifacts" -- which is why the only ChEMBL data ever
committed was 55.9 KB of hand-run sample queries.

The exported tables are the ones the knowledge graph needs, not the whole
database: identity and structure for Drug, UniProt-backed identity for Target,
and the edges between them. `activities` (tens of millions of rows) is left out
deliberately -- it is bioactivity measurement data, not graph structure, and
would dwarf everything else in the lake.
"""
import csv
import sqlite3
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "chembl_data"

# table -> (output csv, columns or None for all)
TABLES = {
    # --- Drug identity -----------------------------------------------------
    "molecule_dictionary": ("chembl_molecules.csv", [
        "molregno", "chembl_id", "pref_name", "molecule_type", "max_phase",
        "first_approval", "oral", "parenteral", "topical", "black_box_warning",
        "natural_product", "prodrug", "withdrawn_flag",
    ]),
    # InChIKey is the canonical Drug id across the whole lake
    "compound_structures": ("chembl_structures.csv", [
        "molregno", "standard_inchi_key", "canonical_smiles",
    ]),
    "molecule_synonyms": ("chembl_synonyms.csv", [
        "molregno", "synonyms", "syn_type",
    ]),
    # --- Target identity ---------------------------------------------------
    "target_dictionary": ("chembl_targets.csv", [
        "tid", "chembl_id", "pref_name", "target_type", "organism",
    ]),
    # accession is the UniProt id -> canonical Target key
    "component_sequences": ("chembl_target_components.csv", [
        "component_id", "accession", "sequence_type", "description", "organism",
    ]),
    "target_components": ("chembl_target_component_map.csv", [
        "tid", "component_id", "homologue",
    ]),
    # --- Edges the schema needs -------------------------------------------
    "drug_mechanism": ("chembl_mechanisms.csv", [
        "molregno", "tid", "mechanism_of_action", "action_type",
        "direct_interaction", "molecular_mechanism",
    ]),
    # ChEMBL maps indications to MeSH -- this is INDICATED_FOR without any
    # text extraction from labels.
    "drug_indication": ("chembl_indications.csv", [
        "molregno", "mesh_id", "mesh_heading", "efo_id", "efo_term", "max_phase_for_ind",
    ]),
    "molecule_atc_classification": ("chembl_atc.csv", ["molregno", "level5"]),
    # Salt/ester form -> parent molecule, stated as fact rather than guessed by
    # stripping "hydrochloride"/"sodium"/... off names. Salt-form confusion is a
    # main way drug resolution goes wrong, so this drives Substance merging.
    "molecule_hierarchy": ("chembl_molecule_hierarchy.csv", None),
    # Small lookups that make derived nodes principled instead of ad hoc:
    "action_type": ("chembl_action_types.csv", None),        # enriches Mechanism
    "usan_stems": ("chembl_usan_stems.csv", None),           # -mab/-tide -> Modality
    "component_synonyms": ("chembl_component_synonyms.csv", None),  # Target names
}

# Small companion file on the same FTP: ChEMBL target id -> UniProt accession,
# which is the key our Target nodes use. Saved as CSV because the pipeline only
# publishes CSV and documents - a .txt would be dropped by collect.
UNIPROT_MAP_URL = ("https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/latest/"
                   "chembl_uniprot_mapping.txt")

CHUNK = 50_000


def find_db() -> Path:
    dbs = sorted(DATA_DIR.rglob("*.db"), key=lambda p: p.stat().st_size, reverse=True)
    if not dbs:
        sys.exit("no ChEMBL SQLite database found under chembl_data/ after download-db")
    return dbs[0]


def table_columns(cur, table):
    cur.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in cur.fetchall()]


def export(db_path: Path) -> int:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
    cur = conn.cursor()
    written = 0

    for table, (out_name, wanted) in TABLES.items():
        have = table_columns(cur, table)
        if not have:
            print(f"  [skip] {table}: not present in this ChEMBL release", flush=True)
            continue
        cols = [c for c in wanted if c in have] if wanted else have
        missing = [c for c in (wanted or []) if c not in have]
        if missing:
            print(f"  [warn] {table}: missing {missing} in this release", flush=True)
        if not cols:
            print(f"  [skip] {table}: none of the wanted columns exist", flush=True)
            continue

        out = DATA_DIR / out_name
        cur.execute(f"SELECT {', '.join(cols)} FROM {table}")
        n = 0
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            while True:
                rows = cur.fetchmany(CHUNK)
                if not rows:
                    break
                w.writerows(rows)
                n += len(rows)
        size_mb = out.stat().st_size / 1e6
        print(f"  [ok]   {table:<32} -> {out_name:<34} {n:>9,} rows  {size_mb:>8.1f} MB",
              flush=True)
        written += 1

    conn.close()
    return written


def fetch_uniprot_map() -> bool:
    """Fetch the ChEMBL target -> UniProt mapping and store it as CSV."""
    import urllib.request
    out = DATA_DIR / "chembl_uniprot_mapping.csv"
    try:
        req = urllib.request.Request(UNIPROT_MAP_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=120) as r:
            text = r.read().decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001 - a missing companion file must not fail the run
        print(f"  [warn] could not fetch uniprot mapping: {e}", flush=True)
        return False

    rows = 0
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["uniprot_accession", "chembl_target_id", "target_name", "target_type"])
        for line in text.splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                w.writerow(parts[:4] + [""] * (4 - len(parts[:4])))
                rows += 1
    print(f"  [ok]   uniprot mapping{'':<24} -> {out.name:<34} {rows:>9,} rows", flush=True)
    return rows > 0


def main() -> None:
    print("=== chembl: download-db ===", flush=True)
    rc = subprocess.run([sys.executable, "chembl_downloader.py", "download-db"],
                        cwd=str(BASE_DIR)).returncode
    if rc != 0:
        sys.exit(f"chembl download-db failed (exit {rc})")

    db = find_db()
    print(f"\n=== chembl: exporting tables from {db.name} "
          f"({db.stat().st_size / 1e9:.1f} GB) ===", flush=True)
    written = export(db)
    print("\n=== chembl: companion files ===", flush=True)
    fetch_uniprot_map()
    if not written:
        sys.exit("no tables exported - refusing to report success with no CSV output")

    # The SQLite dump is an intermediate: it is not publishable (CSV + docs only)
    # and it is ~20 GB, so drop it once the CSVs exist.
    print(f"\nRemoving intermediate database {db.name}", flush=True)
    try:
        db.unlink()
        for leftover in DATA_DIR.rglob("*.tar.gz"):
            leftover.unlink()
    except OSError as e:
        print(f"  could not remove {db}: {e}", flush=True)

    print(f"chembl: {written} table(s) exported to CSV", flush=True)


if __name__ == "__main__":
    main()
