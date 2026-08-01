#!/usr/bin/env python3
"""One-off: fetch the documents the CSVs already in S3 only link to.

    python automation/backfill_linked_docs.py --out /tmp/linked-docs
    python automation/backfill_linked_docs.py --out /tmp/linked-docs --dry-run
    python automation/backfill_linked_docs.py --out /tmp/linked-docs \
           --source sfda.gov.sa

RUNS NO SCRAPER AND TRIGGERS NO DAG. It reads CSVs that are already in the
lake and downloads what they point at. Every scraper stays disabled.

This exists because the fix went in downstream of the problem: the
fetch_linked_docs stage runs after a scrape, and no scrape is going to happen.
The documents linked from the CSVs collected months ago would sit unreferenced
until each source is next enabled. This walks what is already there, once.

After this, the stage keeps it current and this script should not be needed
again - if it is, something is wrong with the stage rather than with the lake.

Writes to a plain directory, not to S3. Publishing is a separate decision:
these files should be looked at before they are added to the corpus the vector
store indexes, and a temp directory is the thing you can delete without
consequence.
"""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import os
import pathlib
import re
import sys
import time
from urllib.parse import urlparse

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "plugins"))
sys.path.insert(0, str(HERE.parent / "graph"))

import lake                                                    # noqa: E402
from scrape_pipeline.linked_docs import (                      # noqa: E402
    _download, _ext_of, _safe_name, DOC_EXT)

CATEGORIES = (
    "Clinical_Trials_Pipeline_Intelligence", "Drug_Substance_Reference",
    "Literature_Evidence", "MENA_GCC_Regulatory_Market",
    "Ontologies_Standards", "Regulatory_Approvals",
    "Safety_Pharmacovigilance", "Targets_Genomics_Biomarkers",
)

_DOCURL = re.compile(r"\.(pdf|docx?|dotx|pptx?)(\?|#|$)", re.I)
HOST_DELAY = 0.7


def scan(prefixes: list[str], sample: int | None) -> list[dict]:
    """Every document URL in every CSV under the given prefixes.

    `sample` exists to make a survey cheap, and is off by default because a
    sampled scan is how the first survey missed SFDA's Pharmacovigilance file
    entirely - its links start below row 40. For the real run, read it all.
    """
    keys: list[str] = []
    for p in prefixes:
        keys += [k for k in lake.list_keys(p) if k.lower().endswith(".csv")]
    print(f"scanning {len(keys)} csvs", flush=True)

    out, seen = [], set()
    for i, key in enumerate(keys, 1):
        try:
            for n, row in enumerate(lake.stream_csv(key, limit=sample)):
                for col, val in row.items():
                    val = str(val or "").strip()
                    if (val.lower().startswith(("http://", "https://"))
                            and _DOCURL.search(val) and val not in seen):
                        seen.add(val)
                        out.append({"url": val, "source_csv": key,
                                    "column": col or "", "row": n})
        except Exception as e:                                 # noqa: BLE001
            print(f"  ! {key.split('/')[-1]}: {type(e).__name__}", flush=True)
        if i % 50 == 0:
            print(f"  {i}/{len(keys)}  urls so far: {len(out):,}", flush=True)
    return out


def publish(out: pathlib.Path, rows: list[dict]) -> None:
    """Upload each downloaded file to S3 beside the source that linked it.

    The path is derived from the CSV the link came from, so an SFDA assessment
    report lands under MENA_GCC_Regulatory_Market/sfda.gov.sa/ and not in some
    parallel folder of its own. That matters because the vector store's
    list_docs derives `source` from the first two segments of the key: put
    these anywhere else and every chunk is attributed to the wrong regulator.

    `linked_documents/` rather than the source root so it is obvious these
    arrived by following a link rather than from the scraper itself, and
    NOT `_runs/` - list_docs skips that prefix by design.

    Nothing is deleted here. The temp directory is removed once the vectors
    exist and have been looked at, which is a separate decision.
    """
    import boto3
    bucket = os.environ.get("S3_BUCKET", "moine-data")
    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    done = failed = 0
    for r in rows:
        if r.get("status") != "ok" or not r.get("file"):
            continue
        src_prefix = "/".join(str(r["source_csv"]).split("/")[:2])
        key = f"{src_prefix}/linked_documents/{pathlib.Path(r['file']).name}"
        try:
            s3.upload_file(str(out / r["file"]), bucket, key)
            done += 1
        except Exception as e:                                 # noqa: BLE001
            failed += 1
            print(f"  ! upload {key}: {type(e).__name__}: {str(e)[:70]}")
    print(f"\npublished {done:,} to s3://{bucket}/ ({failed} failed)")
    print("next: python vector_store/ingest.py --prefix <category>/<source>")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="directory to download into")
    ap.add_argument("--source", default="",
                    help="only this publisher, e.g. sfda.gov.sa")
    ap.add_argument("--dry-run", action="store_true",
                    help="scan and report, download nothing")
    ap.add_argument("--sample", type=int, default=None,
                    help="rows per CSV to scan (default: all)")
    ap.add_argument("--max-per-host", type=int, default=0,
                    help="cap per host, 0 = no cap. OpenAlex links open-access "
                         "full text, which is neither regulatory nor small.")
    ap.add_argument("--publish", action="store_true",
                    help="after downloading, upload to S3 beside the source "
                         "that linked each file, so ingest.py finds them")
    a = ap.parse_args()

    links = scan(list(CATEGORIES), a.sample)
    if a.source:
        links = [l for l in links if a.source in l["source_csv"]
                 or a.source in urlparse(l["url"]).netloc]

    byhost = collections.Counter(urlparse(l["url"]).netloc for l in links)
    print(f"\n{len(links):,} document URLs across {len(byhost)} hosts")
    for h, c in byhost.most_common():
        print(f"   {h:<40} {c:>8,}")

    if a.max_per_host:
        kept, per = [], collections.Counter()
        for l in links:
            h = urlparse(l["url"]).netloc
            if per[h] < a.max_per_host:
                per[h] += 1
                kept.append(l)
        print(f"\ncapped at {a.max_per_host}/host: {len(kept):,} of {len(links):,}")
        links = kept

    if a.dry_run:
        print("\ndry run - nothing downloaded")
        return

    import requests
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    last: dict[str, float] = collections.defaultdict(float)
    rows, got, failed, skipped = [], 0, 0, 0
    t0 = time.time()

    for i, link in enumerate(links, 1):
        url = link["url"]
        ext = _ext_of(url)
        host = urlparse(url).netloc
        dest = out / host / _safe_name(url, ext)
        dest.parent.mkdir(parents=True, exist_ok=True)

        if dest.exists() and dest.stat().st_size:
            skipped += 1
            rows.append({**link, "file": str(dest.relative_to(out)),
                         "bytes": dest.stat().st_size, "sha256": "",
                         "status": "already present"})
            continue

        wait = HOST_DELAY - (time.time() - last[host])
        if wait > 0:
            time.sleep(wait)
        ok, why = _download(session, url, dest)
        last[host] = time.time()

        if ok:
            got += 1
            data = dest.read_bytes()
            rows.append({**link, "file": str(dest.relative_to(out)),
                         "bytes": len(data), "status": "ok",
                         "sha256": hashlib.sha256(data).hexdigest()})
        else:
            failed += 1
            rows.append({**link, "file": "", "bytes": "", "sha256": "",
                         "status": why})

        if i % 25 == 0 or i == len(links):
            rate = i / max(1e-9, time.time() - t0)
            print(f"  [{i}/{len(links)}] ok={got} skip={skipped} fail={failed}"
                  f"  {rate:.1f}/s", flush=True)

    man = out / "backfill_manifest.csv"
    with man.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["url", "file", "bytes", "sha256",
                                           "status", "source_csv", "column", "row"])
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in w.fieldnames})

    total = sum(r["bytes"] for r in rows if isinstance(r.get("bytes"), int))
    print(f"\ndownloaded {got:,}   already present {skipped:,}   failed {failed:,}")
    print(f"{total / 1024 / 1024:.0f} MB in {out}")
    print(f"manifest: {man}")

    if a.publish:
        publish(out, rows)
    if failed:
        why = collections.Counter(r["status"] for r in rows if r["status"] not in
                                  ("ok", "already present"))
        print("\nfailures by reason:")
        for k, v in why.most_common(8):
            print(f"   {v:>6}  {k}")


if __name__ == "__main__":
    main()
