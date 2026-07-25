"""List and download raw documents (pdf/doc/ppt) from the moine-data bucket.

Only the live view is scanned (skips `_runs/` staging and `_LATEST.json`). CSVs
are ignored — they belong to the deterministic graph path, not the vector store.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import boto3

from config import S3_BUCKET, AWS_REGION, DOC_SUFFIXES


@dataclass
class DocRef:
    s3_key: str
    source: str          # "<Topic>/<source>"
    doc_id: str          # stable id = key without extension
    size: int


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
            parts = key.split("/")
            source = "/".join(parts[:2]) if len(parts) >= 2 else parts[0]
            yield DocRef(
                s3_key=key, source=source,
                doc_id=key.rsplit(".", 1)[0], size=o["Size"],
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
