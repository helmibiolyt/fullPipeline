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


# Every S3 key stream_csv has opened. Recorded here rather than by the loaders
# because a file that is read but yields no matching rows still counts as read
# - in slice mode most of them do - and asking each loader to declare its own
# reads is exactly the bookkeeping that let five files go unloaded unnoticed.
READ: set[str] = set()

_client = None


def s3():
    global _client
    if _client is None:
        load_env()
        _client = boto3.client("s3", region_name=REGION)
    return _client


def _is_title_row(cells: list[str]) -> bool:
    """True when a row is a spreadsheet title, not column names.

    Several MENA regulators publish an Excel export with the report's title in
    A1 and the real header underneath:

        line0  ["NUPCO's PHARMACEUTICALS CATALOGUE - JUNE 2026",
                'Unnamed: 1', 'Unnamed: 2', ...]
        line1  ['SN', 'NUPCO Generic Code', 'Item Description', ...]

    Read straight, DictReader names one column after the report and the rest
    `Unnamed: N`, and every mapping that referenced a real column silently
    returns None. Two signatures catch it without guessing: pandas' own
    placeholder for a blank header cell, and a row that is one value followed
    by nothing. A genuine header has names in most of its cells.
    """
    if not cells:
        return True                       # a blank first line is never a header
    filled = [c for c in cells if c.strip()]
    placeholder = sum(1 for c in cells if c.strip().lower().startswith("unnamed:"))
    return len(filled) <= 1 or placeholder >= max(2, len(cells) // 2)


_OFFSET: dict[str, int] = {}


def header_offset(key: str, probe: int = 4) -> int:
    """How many lines to skip before the real header. 0 for a normal file.

    Reads the first 16 KB, not the object, and deliberately does NOT touch
    READ: that set is what the documentation uses to say which files the graph
    consumes, and probing a file for its header shape is not consuming it.
    Marking probes as reads would have quietly reclassified every excluded
    source as loaded. Cached because the build asks for the same key repeatedly.
    """
    if key in _OFFSET:
        return _OFFSET[key]
    raw = s3().get_object(Bucket=BUCKET, Key=key,
                          Range="bytes=0-16384")["Body"].read()
    txt = raw.decode("utf-8-sig", errors="replace")
    off = 0
    for i, line in enumerate(txt.split("\n")[:probe]):
        cells = next(csv.reader([line]), []) if line.strip() else []
        if not _is_title_row([c.strip() for c in cells]):
            off = i
            break
    _OFFSET[key] = off
    return off


def stream_csv(key: str, limit: int | None = None,
               header_row: int | None = None) -> Iterator[dict]:
    """Yield rows of an S3 CSV as dicts, without downloading it.

    Handles three things sources in this lake get wrong somewhere:

    * A UTF-8 BOM on the first header (all 20 SFDA files) - which otherwise
      turns the first column name into '\\ufeffregisterNumber' and silently
      breaks every mapping that references it.
    * Undecodable bytes mid-file. errors="replace" keeps the stream alive; one
      mangled character is better than losing the remaining rows.
    * A title row above the header. `header_row` says which line the names are
      on; left as None it is detected, because the failure is silent - you get
      a column called `Unnamed: 3` and None for every value, not an error.

    None of the affected files are loaded today. Fixing it here rather than
    when one is first consumed is deliberate: the symptom of getting this wrong
    is a mapping that quietly yields nothing, which reads as "the source has no
    data" rather than as a bug.
    """
    if header_row is None:
        header_row = header_offset(key)
    READ.add(key)
    body = s3().get_object(Bucket=BUCKET, Key=key)["Body"]
    wrapper = io.TextIOWrapper(body, encoding="utf-8-sig", errors="replace",
                               newline="")
    for _ in range(header_row):
        wrapper.readline()
    reader = csv.DictReader(wrapper)
    if reader.fieldnames:
        # utf-8-sig handles the leading BOM; strip any that survive mid-header.
        reader.fieldnames = [(f or "").replace("﻿", "").strip()
                             for f in reader.fieldnames]
    for i, row in enumerate(reader):
        if limit is not None and i >= limit:
            break
        yield row


def stream_rows(key: str, limit: int | None = None):
    """Yield rows as plain lists, for files that have no header.

    ClinVar's variant_summary.csv is one: its first line is data, so
    DictReader turns a real variant into the column names and then loses it.
    Callers index by position and must say why that is safe - for ClinVar the
    documented column order is stable at the front, where the fields used live,
    and the extra columns newer releases append go on the end.
    """
    body = s3().get_object(Bucket=BUCKET, Key=key)["Body"]
    wrapper = io.TextIOWrapper(body, encoding="utf-8-sig", errors="replace",
                               newline="")
    READ.add(key)
    for i, row in enumerate(csv.reader(wrapper)):
        if limit is not None and i >= limit:
            break
        yield row


def list_keys(prefix: str, suffix: str = "") -> list[str]:
    """Every object under a prefix, optionally filtered by suffix.

    Exists so loaders stop hardcoding which files a source publishes. A tuple
    of six disease areas or twelve FAERS quarters is a snapshot of what the
    scraper produced on the day it was written: add 2026Q2 and the loader
    silently ignores it, with nothing to flag that the graph is now missing a
    quarter of adverse events.
    """
    out = []
    for page in s3().get_paginator("list_objects_v2").paginate(
            Bucket=BUCKET, Prefix=prefix):
        for o in page.get("Contents", []):
            if o["Key"].endswith(suffix) and o.get("Size", 0) > 0:
                out.append(o["Key"])
    return sorted(out)


def header(key: str) -> list[str]:
    """Column names only - a few KB, not the whole object.

    Skips a title row the same way stream_csv does, so the documentation and
    the loaders agree about what the columns are. They did not before: the
    generated data-source pages showed 'Temporary Importation Medicine List'
    as a column name for that file, and the first real row as its values.
    """
    body = s3().get_object(Bucket=BUCKET, Key=key, Range="bytes=0-16384")["Body"].read()
    txt = body.decode("utf-8-sig", errors="replace")
    for line in txt.split("\n"):
        if not line.strip():
            continue
        cells = [c.strip().strip('"').replace("﻿", "")
                 for c in next(csv.reader([line]))]
        if not _is_title_row(cells):
            return cells
    return []


def exists(key: str) -> bool:
    try:
        s3().head_object(Bucket=BUCKET, Key=key)
        return True
    except Exception:
        return False
