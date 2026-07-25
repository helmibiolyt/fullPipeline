#!/usr/bin/env python3
"""Verify scrapers end-to-end LOCALLY: scrape -> collect -> validate.

The S3 half (upload/verify/commit) is identical code for every source and is
already proven (uniprot, dha), so per-source risk lives entirely in
scrape+collect+validate — that's what this checks. Writes a pass/fail matrix.

    PYTHONPATH=plugins python verify_scrapers.py --enabled-only --timeout 240
"""
import argparse
import time
import traceback

from scrape_pipeline.registry import load_sources
from scrape_pipeline import runner, validation, paths


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--enabled-only", action="store_true", help="skip gated (enabled: false) sources")
    ap.add_argument("--timeout", type=int, default=240, help="per-scraper cap (s); heavy ones flagged")
    ap.add_argument("--only", default="", help="comma-separated slug suffixes to run (else all)")
    a = ap.parse_args()

    srcs = load_sources()
    if a.enabled_only:
        srcs = [s for s in srcs if s.enabled]
    if a.only:
        wants = [w.strip() for w in a.only.split(",")]
        srcs = [s for s in srcs if any(s.slug.endswith(w) for w in wants)]

    rows = []
    for s in sorted(srcs, key=lambda x: x.slug):
        rid = "verify"
        t0 = time.time()
        status, detail = "PASS", ""
        try:
            runner.run_scraper(s, rid, timeout=a.timeout)
            runner.collect(s, rid)
            m = validation.validate_local(s, rid)
            csv = sum(1 for f in m["files"] if f["path"].lower().endswith(".csv"))
            doc = m["n_files"] - csv
            detail = f"{csv} csv, {doc} docs, {m['total_bytes']/1e6:.1f}MB"
        except Exception as e:  # noqa: BLE001
            status = "TIMEOUT" if "TimeoutExpired" in repr(e) else "FAIL"
            detail = str(e).splitlines()[-1][:140]
        dt = time.time() - t0
        rows.append((s.slug, status, f"{dt:.0f}s", detail))
        print(f"{status:8} {s.slug:52} {dt:5.0f}s  {detail}", flush=True)

    print("\n==== SUMMARY ====")
    for st in ("PASS", "FAIL", "TIMEOUT"):
        hits = [r for r in rows if r[1] == st]
        print(f"{st}: {len(hits)}")
        for r in hits:
            print(f"   {r[0]}  ({r[2]})  {r[3]}")


if __name__ == "__main__":
    main()
