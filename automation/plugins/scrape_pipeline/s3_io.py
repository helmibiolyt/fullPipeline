"""Steps 3-6: S3 upload, verification, atomic commit, retention, rollback.

Safety model (never delete live data before the replacement is confirmed):

    <source>/_runs/<run_id>/data/...   immutable, verified copy of one scrape
    <source>/_runs/<run_id>/_MANIFEST.json
    <source>/<relpath>...              the live view consumers read (flat)
    <source>/_LATEST.json             pointer naming the current good run

Commit order: upload -> verify -> copy new into live -> delete stale live ->
flip _LATEST. Live is only ever touched after verification passes, and the
immutable run is retained, so an interrupted commit is always recoverable by
re-syncing from _runs/<run_id>/.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import boto3

from .paths import data_dir, manifest_path, workdir
from .registry import Source
from .settings import (
    AWS_REGION,
    DEFAULT_RETENTION_RUNS,
    LATEST_KEY,
    MIN_COMPLETENESS_RATIO,
    RUNS_PREFIX,
    S3_BUCKET,
)
from .paths import sanitize_run_id

log = logging.getLogger(__name__)


def _client():
    # Credentials come from the standard chain (env / aws_default / ~/.aws).
    return boto3.client("s3", region_name=AWS_REGION)


def _run_prefix(src: Source, run_id: str) -> str:
    return f"{src.s3_base}/{RUNS_PREFIX}/{sanitize_run_id(run_id)}"


def _list(s3, prefix: str) -> dict:
    """Return {key: size} for every object under prefix."""
    out = {}
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=S3_BUCKET, Prefix=prefix
    ):
        for o in page.get("Contents", []):
            out[o["Key"]] = o["Size"]
    return out


# --------------------------------------------------------------------------- #
# Step 0: hydrate — restore the previous run's index/state before scraping
# --------------------------------------------------------------------------- #
def hydrate(src: Source, run_id: str) -> None:
    """Pull `manifest.hydrate` paths from the live S3 view into the scraper dir.

    Scrapers know how to skip work they have already done, but they need last
    run's index to do it: ctri reads completed IDs out of its own CSV, pmda
    checks which documents it already fetched, and so on. The post-commit wipe
    removes those files locally, so without this step every run restarts from
    zero. Only the declared paths are fetched — never the document corpus,
    which would cost more to download than the re-scrape it saves.

    Missing objects are not an error: the first ever run, or a source whose
    layout changed, simply starts fresh.
    """
    if not src.hydrate:
        log.info("[%s] no hydrate paths declared; scraping from scratch", src.slug)
        return

    s3 = _client()
    wd = workdir(src)
    fetched = missing = 0
    total = 0

    def _get(rel: str) -> bool:
        nonlocal total
        dest = wd / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        s3.download_file(S3_BUCKET, f"{src.s3_base}/{rel}", str(dest))
        total += dest.stat().st_size
        return True

    for entry in src.hydrate:
        try:
            if entry.endswith("/"):
                # Prefix form: restore every object beneath it (cdisc's 10
                # terminology CSVs, europepmc's three, ...).
                got = 0
                for page in s3.get_paginator("list_objects_v2").paginate(
                    Bucket=S3_BUCKET, Prefix=f"{src.s3_base}/{entry}"
                ):
                    for o in page.get("Contents", []):
                        rel = o["Key"][len(src.s3_base) + 1:]
                        if rel.endswith("/"):
                            continue
                        _get(rel)
                        got += 1
                if got:
                    fetched += got
                    log.info("[%s] hydrated %d object(s) under %s", src.slug, got, entry)
                else:
                    missing += 1
                    log.info("[%s] nothing under %s in S3 — continuing", src.slug, entry)
            else:
                _get(entry)
                fetched += 1
                log.info("[%s] hydrated %s", src.slug, entry)
        except Exception as e:  # noqa: BLE001 - absence is normal, not fatal
            missing += 1
            log.info("[%s] no prior %s in S3 (%s) — continuing",
                     src.slug, entry, type(e).__name__)
    log.info("[%s] hydrate complete: %d restored (%.1f MB), %d absent",
             src.slug, fetched, total / 1e6, missing)


# --------------------------------------------------------------------------- #
# Step 3: upload the run (immutable staging)
# --------------------------------------------------------------------------- #
def upload_run(src: Source, run_id: str) -> None:
    s3 = _client()
    dd = data_dir(src, run_id)
    rp = _run_prefix(src, run_id)

    files = [p for p in dd.rglob("*") if p.is_file()]
    if not files:
        raise RuntimeError(f"[{src.slug}] nothing to upload from {dd}")

    for p in files:
        rel = p.relative_to(dd).as_posix()
        s3.upload_file(str(p), S3_BUCKET, f"{rp}/data/{rel}")
    s3.upload_file(str(manifest_path(src, run_id)), S3_BUCKET, f"{rp}/_MANIFEST.json")
    log.info("[%s] uploaded %d files to s3://%s/%s/", src.slug, len(files), S3_BUCKET, rp)


# --------------------------------------------------------------------------- #
# Step 4: verify S3 run against the local manifest
# --------------------------------------------------------------------------- #
def verify_run(src: Source, run_id: str) -> None:
    s3 = _client()
    rp = _run_prefix(src, run_id)
    manifest = json.loads(manifest_path(src, run_id).read_text())

    remote = _list(s3, f"{rp}/data/")
    expected = {f"{rp}/data/{e['path']}": e["size"] for e in manifest["files"]}

    missing = [k for k in expected if k not in remote]
    bad_size = [k for k in expected if k in remote and remote[k] != expected[k]]
    if missing or bad_size:
        raise RuntimeError(
            f"[{src.slug}] S3 verify failed: {len(missing)} missing, "
            f"{len(bad_size)} size-mismatch (e.g. {(missing + bad_size)[:5]})"
        )
    log.info("[%s] verified %d objects in _runs/%s", src.slug, len(expected), run_id)


# --------------------------------------------------------------------------- #
# Step 5: atomic commit -> refresh live view, then flip the pointer
# --------------------------------------------------------------------------- #
def commit(src: Source, run_id: str) -> None:
    s3 = _client()
    rp = _run_prefix(src, run_id)
    manifest = json.loads(manifest_path(src, run_id).read_text())

    new_rel = {e["path"] for e in manifest["files"]}
    new_live_keys = {f"{src.s3_base}/{rel}" for rel in new_rel}

    # Snapshot the current live view BEFORE mutating (exclude _runs/ and pointer).
    live_now = _list(s3, f"{src.s3_base}/")
    def _is_live_view(k: str) -> bool:
        tail = k[len(src.s3_base) + 1 :]
        return not tail.startswith(f"{RUNS_PREFIX}/") and tail != LATEST_KEY
    old_live_keys = {k for k in live_now if _is_live_view(k)}

    # 0) Completeness guard (mirror mode only): abort BEFORE any mutation if the
    #    new run looks partial, so live data is left fully intact. The verified
    #    copy still exists under _runs/<run_id>/; rollback_staging drops it.
    #    Compares the new run against the whole current live view (CSV + docs).
    #    Only applies once a PREVIOUS pipeline run exists (a _LATEST pointer): the
    #    first pipeline commit legitimately replaces untrusted pre-pipeline data
    #    (which may have a different file count), so it must not trip the guard.
    old_n = len(old_live_keys)
    old_bytes = sum(live_now[k] for k in old_live_keys)
    new_n = manifest["n_files"]
    new_bytes = manifest["total_bytes"]
    has_prev_pipeline_run = f"{src.s3_base}/{LATEST_KEY}" in live_now
    if src.mirror and has_prev_pipeline_run and old_n and MIN_COMPLETENESS_RATIO > 0:
        # A genuine partial/failed scrape loses DATA — i.e. total bytes drop. Fewer
        # but larger files (format consolidation: many tiny CSVs -> a few big ones)
        # is NOT a loss, so a low file count must not trip the guard on its own; it
        # only counts when the payload also failed to grow.
        files_low = new_n < MIN_COMPLETENESS_RATIO * old_n
        bytes_low = new_bytes < MIN_COMPLETENESS_RATIO * old_bytes
        if bytes_low or (files_low and new_bytes < old_bytes):
            raise RuntimeError(
                f"[{src.slug}] completeness guard tripped: new run has "
                f"{new_n} files / {new_bytes/1e6:.1f} MB vs live "
                f"{old_n} files / {old_bytes/1e6:.1f} MB "
                f"(<{MIN_COMPLETENESS_RATIO:.0%}). Refusing to touch live data — "
                f"likely a partial/failed scrape."
            )

    # 1) Copy new data into the live view (server-side, no re-download).
    for rel in sorted(new_rel):
        s3.copy_object(
            Bucket=S3_BUCKET,
            CopySource={"Bucket": S3_BUCKET, "Key": f"{rp}/data/{rel}"},
            Key=f"{src.s3_base}/{rel}",
        )
    log.info("[%s] copied %d objects into live view", src.slug, len(new_rel))

    # 2) Remove obsolete data — mirror mode only. Additive sources keep everything.
    if not src.mirror:
        log.info("[%s] additive mode: kept all %d prior live objects (no delete)",
                 src.slug, old_n)
    else:
        stale = sorted(old_live_keys - new_live_keys)
        for i in range(0, len(stale), 1000):
            batch = [{"Key": k} for k in stale[i : i + 1000]]
            if batch:
                s3.delete_objects(Bucket=S3_BUCKET, Delete={"Objects": batch, "Quiet": True})
        log.info("[%s] removed %d stale live objects", src.slug, len(stale))

    # 3) Flip the pointer LAST — this is the atomic commit marker.
    pointer = {
        "run_id": run_id,
        "run_prefix": rp,
        "committed_at": datetime.now(timezone.utc).isoformat(),
        "n_files": manifest["n_files"],
        "total_bytes": manifest["total_bytes"],
    }
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=f"{src.s3_base}/{LATEST_KEY}",
        Body=json.dumps(pointer, indent=2).encode(),
        ContentType="application/json",
    )
    log.info("[%s] committed run %s (pointer flipped)", src.slug, run_id)


# --------------------------------------------------------------------------- #
# Step 6: retention — keep the last N immutable runs
# --------------------------------------------------------------------------- #
def prune_runs(src: Source, keep: int = DEFAULT_RETENTION_RUNS) -> None:
    s3 = _client()
    base = f"{src.s3_base}/{RUNS_PREFIX}/"
    run_ids = set()
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=S3_BUCKET, Prefix=base, Delimiter="/"
    ):
        for cp in page.get("CommonPrefixes", []):
            run_ids.add(cp["Prefix"])
    # Lexical sort works because run_ids are timestamp-derived.
    # keep=0 -> delete every run (only live CSVs remain); keep>0 -> keep newest N.
    ordered = sorted(run_ids)
    old = ordered if keep <= 0 else ordered[:-keep]
    for rp in old:
        keys = list(_list(s3, rp))
        for i in range(0, len(keys), 1000):
            batch = [{"Key": k} for k in keys[i : i + 1000]]
            s3.delete_objects(Bucket=S3_BUCKET, Delete={"Objects": batch, "Quiet": True})
        log.info("[%s] pruned old run %s (%d objects)", src.slug, rp, len(keys))
    if not old:
        log.info("[%s] retention: %d runs, nothing to prune", src.slug, len(run_ids))


# --------------------------------------------------------------------------- #
# Failure path: drop only this run's incomplete staging. Never touch live.
# --------------------------------------------------------------------------- #
def rollback_staging(src: Source, run_id: str) -> None:
    s3 = _client()
    rp = _run_prefix(src, run_id)
    keys = list(_list(s3, f"{rp}/"))
    for i in range(0, len(keys), 1000):
        batch = [{"Key": k} for k in keys[i : i + 1000]]
        if batch:
            s3.delete_objects(Bucket=S3_BUCKET, Delete={"Objects": batch, "Quiet": True})
    log.info("[%s] rolled back staging %s (%d objects); live untouched",
             src.slug, run_id, len(keys))
