"""Profile every CSV in the live lake: headers, a sample row, and row counts.

Kept cheap by ranged GETs: headers from the first few KB, row counts estimated
from a 2 MB sample, rather than downloading 52 GB.

Scope is the eight live categories, matching vector_store/s3_docs.py. The
previous version excluded only Old_DataLake, which happened to give the same
set but would silently pick up anything new added at the bucket root.
"""
import csv, io, os, sys, pathlib
from collections import defaultdict
import boto3

# Windows console/file defaults to cp1252; scraped headers contain non-ASCII.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# creds from automation/.env
env = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "automation/.env")
for line in env.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

# The eight live categories. Must stay in step with vector_store/s3_docs.py.
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

BUCKET = os.environ.get("S3_BUCKET", "moine-data")
s3 = boto3.client("s3", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
pg = s3.get_paginator("list_objects_v2")

keys = []
for page in pg.paginate(Bucket=BUCKET):
    for o in page.get("Contents", []):
        k = o["Key"]
        if not k.lower().endswith(".csv"):
            continue
        if "/_runs/" in k:          # staging for an in-flight commit
            continue
        if not k.startswith(CATEGORIES):
            continue
        keys.append((k, o["Size"]))

print(f"# {len(keys)} CSVs across the 8 live categories "
      f"({sum(sz for _, sz in keys) / 1024**3:.1f} GB)\n")

by_src = defaultdict(list)
for k, size in keys:
    parts = k.split("/")
    by_src["/".join(parts[:2])].append((k, size))


SAMPLE = 2_000_000          # bytes read to estimate record density


def row_count(key, size):
    """(rows, exact?) for a CSV, without downloading it.

    S3 Select would have counted this server-side, but AWS has withdrawn it -
    SelectObjectContent now returns MethodNotAllowed. Counting newlines is
    wrong here because these files carry quoted multi-line fields (trial
    summaries, eligibility criteria), so a real CSV reader is used over a
    sample and the record density extrapolated.

    Files smaller than the sample are counted exactly; larger ones are
    estimated and reported as such, since the alternative is parsing 52 GB.
    """
    n = min(size, SAMPLE)
    try:
        body = s3.get_object(Bucket=BUCKET, Key=key,
                             Range=f"bytes=0-{n - 1}")["Body"].read()
    except Exception:
        return None, False
    exact = size <= SAMPLE
    txt = body.decode("utf-8", errors="replace")
    if not exact:
        # The final record is cut mid-way by the range; drop it rather than
        # counting a fragment as a row.
        cut = txt.rfind(chr(10))
        if cut > 0:
            txt = txt[:cut]
    try:
        rows = sum(1 for _ in csv.reader(io.StringIO(txt)))
    except Exception:
        return None, False
    rows = max(rows - 1, 0)                      # drop the header
    if exact:
        return rows, True
    if rows == 0:
        return None, False
    density = len(txt.encode("utf-8", errors="replace")) / rows
    return int((size / density) - 1), False


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
        n, exact = row_count(key, size)
        rows = ("rows unknown" if n is None
                else f"{n:,} rows" if exact else f"~{n:,} rows est")
        print(f"\n-- {rel}  ({h(size)}, {len(cols)} cols, {rows})")
        print(f"   COLS: {cols}")
        if sample:
            trimmed = [(c[:60] + "...") if len(c) > 60 else c for c in sample[:12]]
            print(f"   ROW1: {trimmed}")
