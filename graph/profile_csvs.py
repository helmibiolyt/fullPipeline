"""Profile every CSV in the new lake: real headers + a sample row.

Uses ranged GETs so we read a few KB per file instead of downloading 69.8 GB.
"""
import csv, io, os, sys, pathlib
from collections import defaultdict
import boto3

# creds from automation/.env
env = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "automation/.env")
for line in env.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

BUCKET = os.environ.get("S3_BUCKET", "moine-data")
s3 = boto3.client("s3", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
pg = s3.get_paginator("list_objects_v2")

keys = []
for page in pg.paginate(Bucket=BUCKET):
    for o in page.get("Contents", []):
        k = o["Key"]
        if k.startswith("Old_DataLake/"):
            continue
        if k.lower().endswith(".csv"):
            keys.append((k, o["Size"]))

print(f"# {len(keys)} CSVs in the new lake\n")

by_src = defaultdict(list)
for k, size in keys:
    parts = k.split("/")
    by_src["/".join(parts[:2])].append((k, size))


def head_bytes(key, n=6000):
    try:
        body = s3.get_object(Bucket=BUCKET, Key=key, Range=f"bytes=0-{n}")["Body"].read()
        return body.decode("utf-8", errors="replace")
    except Exception as e:
        return f"__ERROR__ {e}"


def h(x):
    for u in ["B", "KB", "MB", "GB"]:
        if x < 1024:
            return f"{x:.1f}{u}"
        x /= 1024
    return f"{x:.1f}TB"


for src in sorted(by_src):
    print(f"\n{'='*100}\n## {src}\n{'='*100}")
    for key, size in sorted(by_src[src]):
        rel = key[len(src) + 1:]
        txt = head_bytes(key)
        if txt.startswith("__ERROR__"):
            print(f"\n-- {rel} ({h(size)}) -> {txt[:120]}")
            continue
        try:
            rdr = csv.reader(io.StringIO(txt))
            cols = next(rdr, [])
            sample = next(rdr, [])
        except Exception as e:
            print(f"\n-- {rel} ({h(size)}) -> parse error {e}")
            continue
        print(f"\n-- {rel}  ({h(size)}, {len(cols)} cols)")
        print(f"   COLS: {cols}")
        if sample:
            trimmed = [(c[:60] + "...") if len(c) > 60 else c for c in sample[:12]]
            print(f"   ROW1: {trimmed}")
