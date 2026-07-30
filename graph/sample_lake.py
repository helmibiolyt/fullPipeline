#!/usr/bin/env python3
"""Inventory the S3 lake: every category, every source, every file, with real
column names and real sample rows.

    python graph/sample_lake.py --out lake_sample.json

Written to run on a host with S3 access and be copied back as one JSON, so the
document generator never needs credentials.

Sampling reads the first 128 KB of each CSV with a ranged GET rather than the
object. One file in this bucket is 2.9 GB and several are over 100 MB;
downloading them to read five rows would move ~40 GB to print a table.

Documents are counted by extension only. There are ~93,000 of them and their
content is not what this inventory is about.
"""
from __future__ import annotations

import argparse
import collections
import csv
import io
import json

import boto3

BUCKET = "moine-data"

# The eight real categories. _template is scaffolding and Old_DataLake is
# explicitly out of scope.
CATEGORIES = [
    "Clinical_Trials_Pipeline_Intelligence",
    "Drug_Substance_Reference",
    "Literature_Evidence",
    "MENA_GCC_Regulatory_Market",
    "Ontologies_Standards",
    "Regulatory_Approvals",
    "Safety_Pharmacovigilance",
    "Targets_Genomics_Biomarkers",
]

SAMPLE_BYTES = 128 * 1024
N_ROWS = 5
DOC_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".htm", ".html", ".txt",
           ".xml", ".zip", ".json"}


def sample_csv(s3, key: str) -> dict:
    """Header and first rows of a CSV, read from its first 128 KB.

    A truncated final row is dropped: a ranged GET almost always cuts one in
    half, and half a row printed as data is worse than one fewer row.
    """
    try:
        body = s3.get_object(Bucket=BUCKET, Key=key,
                             Range=f"bytes=0-{SAMPLE_BYTES - 1}")["Body"].read()
    except Exception as e:                                   # noqa: BLE001
        return {"error": str(e)[:200]}

    text = body.decode("utf-8", errors="replace")
    if len(body) == SAMPLE_BYTES:
        cut = text.rfind("\n")
        if cut > 0:
            text = text[:cut]

    try:
        rows = list(csv.reader(io.StringIO(text)))
    except Exception as e:                                   # noqa: BLE001
        return {"error": f"csv parse: {str(e)[:150]}"}
    if not rows:
        return {"error": "empty"}

    header = rows[0]
    data = rows[1:1 + N_ROWS]
    # Long free-text fields (abstracts, eligibility criteria) are unreadable in
    # a table cell and push every other column off the page.
    data = [[(c[:90] + "…") if len(c) > 90 else c for c in r] for r in data]
    return {"columns": header, "n_columns": len(header), "rows": data}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="lake_sample.json")
    a = ap.parse_args()

    s3 = boto3.client("s3")
    pager = s3.get_paginator("list_objects_v2")
    out = {}

    for cat in CATEGORIES:
        sources: dict[str, dict] = {}
        n_obj = 0
        for page in pager.paginate(Bucket=BUCKET, Prefix=f"{cat}/"):
            for o in page.get("Contents", []):
                key, size = o["Key"], o["Size"]
                parts = key.split("/")
                if len(parts) < 3:
                    continue
                src = parts[1]
                name = parts[-1]
                s = sources.setdefault(
                    src, {"csvs": {}, "docs": collections.Counter(),
                          "doc_bytes": 0, "csv_bytes": 0, "examples": []})
                n_obj += 1

                low = name.lower()
                if low.endswith(".csv"):
                    s["csv_bytes"] += size
                    # A source may write the same CSV under several run
                    # prefixes; keep the newest, which is what the build reads.
                    prev = s["csvs"].get(name)
                    if prev is None or o["LastModified"].isoformat() > prev["modified"]:
                        s["csvs"][name] = {
                            "key": key, "size": size,
                            "modified": o["LastModified"].isoformat(),
                        }
                elif any(low.endswith(e) for e in DOC_EXT):
                    ext = "." + low.rsplit(".", 1)[-1]
                    s["docs"][ext] += 1
                    s["doc_bytes"] += size
                    if len(s["examples"]) < 3:
                        s["examples"].append(name)

        for src, s in sources.items():
            for name, meta in s["csvs"].items():
                meta.update(sample_csv(s3, meta["key"]))
            s["docs"] = dict(s["docs"])
        out[cat] = sources
        print(f"{cat}: {len(sources)} sources, {n_obj} objects", flush=True)

    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    print(f"-> {a.out}")


if __name__ == "__main__":
    main()
