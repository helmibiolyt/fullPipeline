#!/usr/bin/env python3
"""Audit sources without scraping them.

verify_scrapers.py already runs scrape -> collect -> validate for real. That is
the strongest check, but it is also the expensive one, so it cannot be pointed at
all 49 sources casually. This covers the failure modes that a real run would
catch only slowly, or not at all:

  imports   the entrypoint imports and its argparse works. Catches a dependency
            that exists on a laptop but not in the Airflow image, which
            otherwise surfaces as a scrape failure hours into a run.

  outputs   filenames the code writes, compared against what is actually in S3.
            This is the purplebook check: that scraper built its enriched
            product table in memory, declared the path, never wrote the file,
            and logged success. Nothing but reading the code caught it. Any
            filename the code names but S3 lacks is worth a look.

  upstream  the hosts the scraper talks to still answer. Catches a bulk file
            that moved or a login wall that appeared, without downloading it.

None of this proves a scraper's output is still CORRECT - only that it can start,
that its declared outputs exist, and that its sources are alive. Use
verify_scrapers.py for real confidence on a specific source.

    PYTHONPATH=plugins python audit_sources.py --tier 1,3
"""
import argparse
import re
import subprocess
import sys
import urllib.request
from collections import Counter
from pathlib import Path

from scrape_pipeline.registry import load_sources
from scrape_pipeline.settings import S3_BUCKET, SCRAPE_ROOT

# Filenames appearing in code that are inputs or noise rather than declared output.
_IGNORE_NAMES = {"requirements.txt", "manifest.yaml", "readme.md"}
# Hosts that are infrastructure rather than the data source itself.
_IGNORE_HOSTS = {
    "api.openai.com", "generativelanguage.googleapis.com", "openai.com",
    "raw.githubusercontent.com", "github.com", "pypi.org", "schema.org",
    "www.w3.org", "localhost", "127.0.0.1", "amazonaws.com",
}

_CSV_LIT = re.compile(r"""["']([A-Za-z0-9_.\-/\\ ]+\.csv)["']""")
_URL = re.compile(r"""https?://([A-Za-z0-9.\-]+)""")


def _py_files(d: Path):
    return sorted(p for p in d.glob("*.py") if p.name != "__init__.py")


def check_imports(src, root: Path, timeout: int = 90):
    """Run the entrypoint with --help: imports the module and exercises argparse."""
    entry = root / src.entrypoint
    if not entry.exists():
        return "FAIL", f"entrypoint missing: {src.entrypoint}"
    try:
        p = subprocess.run([sys.executable, str(entry), "--help"], cwd=str(root),
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # Some scrapers do work at import time; that is not a failure by itself.
        return "WARN", f"--help did not return within {timeout}s"
    if p.returncode == 0:
        return "PASS", ""
    err = (p.stderr or p.stdout or "").strip().splitlines()
    last = err[-1][:150] if err else f"exit {p.returncode}"
    kind = "FAIL" if "ModuleNotFoundError" in (p.stderr or "") or "ImportError" in (p.stderr or "") else "WARN"
    return kind, last


def declared_csvs(root: Path) -> set:
    names = set()
    for f in _py_files(root):
        for m in _CSV_LIT.findall(f.read_text(encoding="utf-8", errors="ignore")):
            n = Path(m.replace("\\", "/")).name.strip()
            if n and n.lower() not in _IGNORE_NAMES and not n.startswith("."):
                names.add(n)
    return names


def s3_csvs(s3, src) -> set:
    names, token = set(), None
    while True:
        kw = {"Bucket": S3_BUCKET, "Prefix": f"{src.s3_base}/"}
        if token:
            kw["ContinuationToken"] = token
        r = s3.list_objects_v2(**kw)
        for o in r.get("Contents", []):
            names.add(Path(o["Key"]).name)
        if not r.get("IsTruncated"):
            return names
        token = r["NextContinuationToken"]


def check_outputs(src, root: Path, s3):
    declared = declared_csvs(root)
    if not declared:
        return "SKIP", "no CSV filenames found in code"
    try:
        present = s3_csvs(s3, src)
    except Exception as e:  # noqa: BLE001
        return "WARN", f"S3 list failed: {e}"
    if not present:
        return "WARN", "nothing in S3 for this source"
    # Case-insensitive: several scrapers lowercase filenames on the way out, so
    # the code says Neoplasm_Core.csv and S3 holds neoplasm_core.csv. Comparing
    # literally reports those as missing when they are right there.
    present_ci = {n.lower() for n in present}
    missing = sorted(n for n in declared if n.lower() not in present_ci)
    if not missing:
        return "PASS", f"{len(declared)} declared, all present"
    return "CHECK", f"declared but absent in S3: {', '.join(missing[:6])}"


def check_upstream(src, root: Path, timeout: int = 20):
    hosts = Counter()
    for f in _py_files(root):
        for h in _URL.findall(f.read_text(encoding="utf-8", errors="ignore")):
            if h.lower() not in _IGNORE_HOSTS and not h.endswith("amazonaws.com"):
                hosts[h.lower()] += 1
    if not hosts:
        return "SKIP", "no upstream host found in code"
    host = hosts.most_common(1)[0][0]
    req = urllib.request.Request(
        f"https://{host}/",
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/120.0.0.0 Safari/537.36"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return ("PASS", f"{host} -> {r.status}") if r.status < 400 else ("WARN", f"{host} -> {r.status}")
    except Exception as e:  # noqa: BLE001 - a dead host is the finding, not a crash
        return "WARN", f"{host}: {str(e)[:90]}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="1,3", help="1=imports+outputs, 3=upstream")
    ap.add_argument("--only", default="", help="comma-separated slug suffixes")
    ap.add_argument("--include-enabled", action="store_true",
                    help="also audit enabled sources (default: only disabled/untested ones)")
    a = ap.parse_args()
    tiers = {t.strip() for t in a.tier.split(",")}

    srcs = load_sources()
    if not a.include_enabled:
        srcs = [s for s in srcs if not s.enabled]
    if a.only:
        wants = [w.strip() for w in a.only.split(",")]
        srcs = [s for s in srcs if any(s.slug.endswith(w) for w in wants)]

    s3 = None
    if "1" in tiers:
        import boto3
        s3 = boto3.client("s3")

    # Same root the registry discovered these sources from, so this works
    # whether it runs on a laptop or inside the Airflow container.
    scrape_root = Path(SCRAPE_ROOT)
    rows = []
    for s in sorted(srcs, key=lambda x: x.slug):
        root = scrape_root / s.topic / s.source
        r = {"slug": s.slug}
        if "1" in tiers:
            r["imports"] = check_imports(s, root)
            r["outputs"] = check_outputs(s, root, s3)
        if "3" in tiers:
            r["upstream"] = check_upstream(s, root)
        rows.append(r)
        cols = " | ".join(f"{k}={v[0]}" for k, v in r.items() if k != "slug")
        print(f"{s.slug:52} {cols}", flush=True)

    print("\n" + "=" * 100)
    for key in ("imports", "outputs", "upstream"):
        vals = [r[key] for r in rows if key in r]
        if not vals:
            continue
        counts = Counter(v[0] for v in vals)
        print(f"\n{key.upper()}: {dict(counts)}")
        for r in rows:
            if key in r and r[key][0] not in ("PASS", "SKIP"):
                print(f"   {r[key][0]:6} {r['slug']:50} {r[key][1]}")


if __name__ == "__main__":
    main()
