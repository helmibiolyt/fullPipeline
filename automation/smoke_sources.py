#!/usr/bin/env python3
"""Run scrapers on a deliberately tiny slice and check the output is real.

audit_sources.py checks that a scraper imports, that its declared filenames are
in S3, and that its hosts answer. None of that would have caught the four
failures found on 2026-07-26, because every one of them exited 0 with a
plausible-looking result:

  purplebook  built its enriched table in memory and never wrote the file
  sfda        wrote five CSVs containing a BOM and a newline
  openalex    crawled for three hours and committed nothing
  mhra        wrote its index under the wrong header, losing 24,906 paths

The only check that catches that class is running the thing and looking at what
comes out. This does that on a slice small enough to take a minute or two.

Each source is copied to a scratch directory first. Every scraper here is
in_place:true, so running one in the repo would write into the real scrape
folder and could collide with a live pipeline run.

What this proves: the scraper runs end to end against the live upstream and
produces a CSV with a header and at least one row. What it does NOT prove: that
the full run completes, or that the numbers are right. Sources whose entrypoint
is a run_all.py wrapper have no argparse at all, so the slice runs the inner
downloader instead - that tests the scraper but not the wrapper's orchestration,
and those rows are marked accordingly.

    PYTHONPATH=plugins python smoke_sources.py --only gsrs,icd
"""
import argparse
import csv
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from scrape_pipeline.settings import SCRAPE_ROOT

# slug suffix -> (topic/source dir, argv for a small slice, wrapper_bypassed)
# argv[0] is the script to run, relative to the source directory.
SMOKE = {
    "clinicaltrials_gov": (
        "Clinical_Trials_Pipeline_Intelligence/clinicaltrials.gov",
        ["clinicaltrials_gov_downloader.py", "--limit", "25"], False),
    "jrct_mhlw_go_jp": (
        "Clinical_Trials_Pipeline_Intelligence/jrct.mhlw.go.jp",
        ["jrct_downloader.py", "--max-trials", "15"], False),
    "gsrs_ncats_nih_gov": (
        "Drug_Substance_Reference/gsrs.ncats.nih.gov",
        ["gsrs_downloader.py", "--limit", "25"], False),
    "atcddd_fhi_no": (
        "Drug_Substance_Reference/atcddd.fhi.no",
        # --limit-branch takes a Level-1 ATC code; V is the smallest branch.
        ["atc_ddd_downloader.py", "--limit-branch", "V"], False),
    "pubmed_ncbi_nlm_nih_gov": (
        "Literature_Evidence/pubmed.ncbi.nlm.nih.gov",
        ["pubmed_downloader.py", "--threads", "3", "--limit", "25"], False),
    "icd_who_int": (
        "Ontologies_Standards/icd.who.int",
        ["who_icd_downloader.py", "--limit", "25"], False),
    "open_fda_gov": (
        "Regulatory_Approvals/open.fda.gov",
        ["openfda_downloader.py", "api-harvest", "--limit", "25"], False),
    "pmda_go_jp": (
        "Regulatory_Approvals/pmda.go.jp",
        ["pmda_collector.py", "--limit", "10"], False),
    # run_all.py wrappers carry no argparse, so the slice runs the inner script.
    "clinicaltrialsregister_eu": (
        "Clinical_Trials_Pipeline_Intelligence/clinicaltrialsregister.eu",
        ["eu_ctr_downloader.py", "--limit", "25"], True),
    "trialsearch_who_int": (
        "Clinical_Trials_Pipeline_Intelligence/trialsearch.who.int",
        ["who_collector.py", "--limit", "25"], True),
    "europepmc_org": (
        "Literature_Evidence/europepmc.org",
        ["europe_pmc_downloader.py", "--limit", "25"], True),
    "dailymed_nlm_nih_gov": (
        "Drug_Substance_Reference/dailymed.nlm.nih.gov",
        ["dailymed_downloader.py", "download-mappings"], True),
}

MIN_CSV_BYTES = 50


def csv_report(root: Path):
    """Every CSV under root with its size and row count."""
    out = []
    for p in sorted(root.rglob("*.csv")):
        try:
            size = p.stat().st_size
            with open(p, newline="", encoding="utf-8", errors="replace") as f:
                rows = sum(1 for _ in csv.reader(f))
        except Exception:  # noqa: BLE001
            size, rows = -1, -1
        out.append((p.name, size, max(rows - 1, 0)))
    return out


def smoke(slug: str, rel: str, argv: list, timeout: int) -> tuple:
    src_dir = Path(SCRAPE_ROOT) / rel
    if not src_dir.exists():
        return "FAIL", f"source dir missing: {rel}", []

    work = Path(tempfile.mkdtemp(prefix=f"smoke_{slug}_"))
    try:
        # Copy code only. Any pre-existing data would make an empty run look
        # successful, which is precisely the failure being tested for.
        run_dir = work / "src"
        shutil.copytree(src_dir, run_dir,
                        ignore=shutil.ignore_patterns(
                            "*.csv", "*.pdf", "*.db", "*.db-*", "*.json",
                            "__pycache__", "*.log", "*.zip", "*.xlsx"))
        script = run_dir / argv[0]
        if not script.exists():
            return "FAIL", f"script not found: {argv[0]}", []

        t0 = time.time()
        try:
            p = subprocess.run([sys.executable, *argv], cwd=str(run_dir),
                               capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            found = csv_report(run_dir)
            real = [c for c in found if c[1] >= MIN_CSV_BYTES and c[2] > 0]
            # A slice that is still going but has already written real rows is
            # working; it is the slice limit that was too generous, not the code.
            if real:
                return "SLOW", f"still running at {timeout}s but {len(real)} CSV(s) already have rows", real
            return "FAIL", f"no output after {timeout}s", found

        elapsed = time.time() - t0
        found = csv_report(run_dir)
        real = [c for c in found if c[1] >= MIN_CSV_BYTES and c[2] > 0]
        empty = [c for c in found if c[1] < MIN_CSV_BYTES or c[2] == 0]

        if p.returncode != 0:
            tail = (p.stderr or p.stdout or "").strip().splitlines()
            return "FAIL", f"exit {p.returncode}: {tail[-1][:120] if tail else ''}", found
        if not found:
            return "FAIL", f"exit 0 in {elapsed:.0f}s but wrote no CSV at all", []
        if not real:
            return "EMPTY", f"exit 0 in {elapsed:.0f}s, {len(found)} CSV(s), all empty", found
        note = f"{len(real)} CSV(s) with rows in {elapsed:.0f}s"
        if empty:
            note += f"; {len(empty)} empty"
        return "PASS", note, real
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="comma-separated slug suffixes")
    ap.add_argument("--timeout", type=int, default=300, help="per-source cap (s)")
    a = ap.parse_args()

    targets = dict(SMOKE)
    if a.only:
        wants = [w.strip() for w in a.only.split(",")]
        targets = {k: v for k, v in targets.items() if any(w in k for w in wants)}

    results = []
    for slug, (rel, argv, bypass) in targets.items():
        status, note, csvs = smoke(slug, rel, argv, a.timeout)
        results.append((slug, status, note, csvs, bypass))
        flag = " [wrapper bypassed]" if bypass else ""
        print(f"{slug:28} {status:6} {note}{flag}", flush=True)
        for name, size, rows in csvs[:3]:
            print(f"      {name[:52]:52} {size:>10,}B  {rows:>7,} rows", flush=True)

    print("\n" + "=" * 92)
    from collections import Counter
    print("SMOKE:", dict(Counter(r[1] for r in results)))
    for slug, status, note, _, bypass in results:
        if status != "PASS":
            print(f"   {status:6} {slug:28} {note}")
    bypassed = [r[0] for r in results if r[4] and r[1] == "PASS"]
    if bypassed:
        print(f"\n   NOTE: {len(bypassed)} source(s) passed via their inner downloader; the "
              f"run_all.py wrapper the pipeline actually invokes was not exercised:")
        print("        " + ", ".join(bypassed))


if __name__ == "__main__":
    main()
