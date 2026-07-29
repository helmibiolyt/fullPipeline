"""Streaming reads from the S3 lake.

Nothing here loads a whole file. The largest inputs are 4.6 GB (WHO trials) and
2.7 GB (ClinicalTrials.gov), against ~7.6 GB of RAM shared with Qdrant - so
every read is a stream and peak memory is one row plus whatever the caller
keeps.
"""
from __future__ import annotations

import csv
import io
import os
import pathlib
import sys
from typing import Iterator

import boto3

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))   # trial summaries are huge

BUCKET = os.environ.get("S3_BUCKET", "moine-data")
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")


def load_env(path: str = "automation/.env") -> None:
    """Best-effort load of automation/.env, if one happens to be there.

    Only a fallback. boto3 already resolves environment variables, ~/.aws and
    the EC2 instance role on its own, and an instance role is the right answer
    on a build host - nothing to rotate, nothing to leak, and it survives the
    box being replaced. This exists so a laptop with a .env file also works.

    Checked relative to this file as well as the working directory, because the
    build is run from several places and a missing .env should not depend on
    which one.
    """
    here = pathlib.Path(__file__).resolve().parent
    for p in (pathlib.Path(path), here / path, here.parent / path):
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
        return


_client = None


def s3():
    global _client
    if _client is None:
        load_env()
        _client = boto3.client("s3", region_name=REGION)
    return _client


def stream_csv(key: str, limit: int | None = None) -> Iterator[dict]:
    """Yield rows of an S3 CSV as dicts, without downloading it.

    Handles two things every source in this lake gets wrong somewhere:

    * A UTF-8 BOM on the first header (all 20 SFDA files) - which otherwise
      turns the first column name into '\\ufeffregisterNumber' and silently
      breaks every mapping that references it.
    * Undecodable bytes mid-file. errors="replace" keeps the stream alive; one
      mangled character is better than losing the remaining rows.
    """
    body = s3().get_object(Bucket=BUCKET, Key=key)["Body"]
    wrapper = io.TextIOWrapper(body, encoding="utf-8-sig", errors="replace",
                               newline="")
    reader = csv.DictReader(wrapper)
    if reader.fieldnames:
        # utf-8-sig handles the leading BOM; strip any that survive mid-header.
        reader.fieldnames = [(f or "").replace("﻿", "").strip()
                             for f in reader.fieldnames]
    for i, row in enumerate(reader):
        if limit is not None and i >= limit:
            break
        yield row


def header(key: str) -> list[str]:
    """Column names only - a few KB, not the whole object."""
    body = s3().get_object(Bucket=BUCKET, Key=key, Range="bytes=0-16384")["Body"].read()
    txt = body.decode("utf-8-sig", errors="replace")
    line = txt.split("\n", 1)[0]
    return [c.strip().strip('"').replace("﻿", "")
            for c in next(csv.reader([line]))] if line else []


def exists(key: str) -> bool:
    try:
        s3().head_object(Bucket=BUCKET, Key=key)
        return True
    except Exception:
        return False
