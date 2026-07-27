"""List and download raw documents (pdf/doc/ppt) from the moine-data bucket.

Only the live view is scanned (skips `_runs/` staging and `_LATEST.json`). CSVs
are ignored — they belong to the deterministic graph path, not the vector store.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import boto3

from config import S3_BUCKET, AWS_REGION, DOC_SUFFIXES

# The eight live categories. Everything else in the bucket is off limits:
# Old_DataLake is a historical copy the user asked never to read, and
# endpoint-schema.md is not a document. Enforced here rather than relying on a
# caller passing the right prefix, because list_docs("") would otherwise walk
# the entire bucket.
CATEGORIES = (
    "Clinical_Trials_Pipeline_Intelligence",
    "Drug_Substance_Reference",
    "Literature_Evidence",
    "MENA_GCC_Regulatory_Market",
    "Ontologies_Standards",
    "Regulatory_Approvals",
    "Safety_Pharmacovigilance",
    "Targets_Genomics_Biomarkers",
)


@dataclass
class DocRef:
    s3_key: str
    source: str          # "<Topic>/<source>"
    doc_id: str          # stable id = key without extension
    size: int
    etag: str = ""       # S3 ETag - the change signal for incremental sync
    doc_type: str | None = None   # spc | pil | par — from the path, for chunking


def _client():
    return boto3.client("s3", region_name=AWS_REGION)


def list_docs(prefix: str = "", limit: int | None = None):
    """Yield DocRef for every document object in the bucket (optionally under prefix)."""
    s3 = _client()
    n = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for o in page.get("Contents", []):
            key = o["Key"]
            name = key.rsplit("/", 1)[-1]
            if "." not in name:
                continue
            if "." + name.rsplit(".", 1)[-1].lower() not in DOC_SUFFIXES:
                continue
            if "/_runs/" in key:
                continue
            if not key.startswith(CATEGORIES):
                continue                      # Old_DataLake and friends
            parts = key.split("/")
            source = "/".join(parts[:2]) if len(parts) >= 2 else parts[0]
            dt = None
            if "/pdfs/" in key:
                dt = key.split("/pdfs/")[1].split("/")[0].lower()
            yield DocRef(
                s3_key=key, source=source,
                doc_id=key.rsplit(".", 1)[0], size=o["Size"],
                etag=(o.get("ETag") or "").strip('"'), doc_type=dt,
            )
            n += 1
            if limit and n >= limit:
                return


def download(doc: DocRef, dest_dir: str) -> str:
    """Download one doc to a local path; returns that path."""
    os.makedirs(dest_dir, exist_ok=True)
    local = os.path.join(dest_dir, doc.s3_key.replace("/", "__"))
    _client().download_file(S3_BUCKET, doc.s3_key, local)
    return local
