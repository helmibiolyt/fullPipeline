#!/usr/bin/env python3
"""Every CSV under Ontologies_Standards: size, columns, and whether the build
reads it.

    python graph/survey_ontologies.py

Written because the folder holds 83 files and seven were being read. Deciding
what to do with the rest needs the shape of each one in front of you, not a
guess from its filename - "CDASH Terminology" sounds like clinical vocabulary
and is 376 rows of form-field codes, while "SDTM Terminology" is 46,774 rows
containing the standard dosage-form list.

Reads and prints. Changes nothing.
"""
from __future__ import annotations

import collections
import itertools
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import lake                                              # noqa: E402

ROOT = "Ontologies_Standards/"

# Paths the build already streams, so the survey can say what is left.
READ_BY_BUILD = {
    "meshb.nlm.nih.gov/mesh_data/mesh_descriptors.csv",
    "meshb.nlm.nih.gov/mesh_data/mesh_pharmacological_actions.csv",
    "meshb.nlm.nih.gov/mesh_data/mesh_supplemental_concepts.csv",
    "icd.who.int/icd_data/icd10_chapters.csv",
    "icd.who.int/icd_data/icd10_blocks.csv",
    "icd.who.int/icd_data/icd10_codes.csv",
    "icd.who.int/icd_data/icd11_codes.csv",
    "evs.nci.nih.gov/nci_thesaurus_data/nci_thesaurus_concepts.csv",
    "evs.nci.nih.gov/nci_thesaurus_data/neoplasm_core.csv",
    "evs.nci.nih.gov/nci_thesaurus_data/antineoplastic_agents.csv",
    "evs.nci.nih.gov/nci_thesaurus_data/mapping_ncit_swissprot.csv",
    "evs.nci.nih.gov/nci_thesaurus_data/mapping_ncit_chebi.csv",
    "evs.nci.nih.gov/nci_thesaurus_data/mapping_ncit_hgnc.csv",
    "evs.nci.nih.gov/nci_thesaurus_data/nci_code_cui_map.csv",
    "cdisc.org/CDISC/data/SDTM Terminology.csv",
    "cdisc.org/CDISC/data/CDASH Terminology.csv",
    "cdisc.org/CDISC/data/Protocol Terminology.csv",
}

SEP = chr(124)          # a pipe, written this way because heredocs eat quotes
COMMA = chr(44) + chr(32)


def rowcount(key: str, cap: int = 200_000) -> tuple[int, bool]:
    """Rows, and whether the count hit the cap. LOINC has files in the
    millions and an exact count is not worth the wait."""
    n = 0
    for _ in lake.stream_csv(key):
        n += 1
        if n >= cap:
            return n, True
    return n, False


def main() -> None:
    keys = [k for k in sorted(lake.list_keys(ROOT)) if k.endswith(".csv")]
    print(f"{len(keys)} CSVs under {ROOT}")
    print()
    by_source: dict[str, list] = collections.defaultdict(list)
    for k in keys:
        by_source[k.replace(ROOT, "").split("/")[0]].append(k)

    for source in sorted(by_source):
        print("=" * 78)
        print(f"{source}   ({len(by_source[source])} files)")
        print("=" * 78)
        for k in by_source[source]:
            short = k.replace(ROOT, "")
            mark = "READ " if short in READ_BY_BUILD else "  -  "
            try:
                head = list(itertools.islice(lake.stream_csv(k), 1))
                cols = list(head[0].keys()) if head else []
                n, capped = rowcount(k)
                size = f"{n:,}{chr(43) if capped else ''}"
                print(f"{mark} {short.split('/')[-1][:44]:<46} {size:>9} rows")
                print(f"        cols: {COMMA.join(cols[:8])}")
            except Exception as exc:                      # noqa: BLE001
                print(f"{mark} {short.split('/')[-1][:44]:<46}  ERR "
                      f"{type(exc).__name__}")
        print()


if __name__ == "__main__":
    main()
