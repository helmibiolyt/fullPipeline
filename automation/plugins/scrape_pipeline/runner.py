"""Step 1: run the scraper and land its artifacts in the run's data dir."""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .paths import data_dir, run_dir, workdir
from .registry import Source
from .settings import (
    ARTIFACT_EXCLUDE_DIRS,
    CONVERT_XLSX,
    CSV_SUFFIXES,
    DOC_SUFFIXES,
    JSONL_SUFFIXES,
    KEEP_LOCAL_RUNS,
    KEEP_SCRAPER_STATE,
    PUBLISH_SUFFIXES,
    RUN_ROOT,
    SPREADSHEET_SUFFIXES,
    STATE_FILE_SUFFIXES,
    STATE_NAME_MARKERS,
    STRUCTURED_DROPPED_SUFFIXES,
    TABULAR_SUFFIXES,
    TSV_SUFFIXES,
    WIPE_SCRAPER_DIR,
)

log = logging.getLogger(__name__)


def _collectable_suffixes() -> set:
    """Suffixes worth copying into the run dir: CSV + raw docs, plus tabular
    formats (xlsx/tsv, converted to CSV afterwards)."""
    return PUBLISH_SUFFIXES | (TABULAR_SUFFIXES if CONVERT_XLSX else set())


def _is_collectable(p: Path) -> bool:
    if not p.is_file():
        return False
    if p.suffix.lower() not in _collectable_suffixes():
        return False
    if any(part in ARTIFACT_EXCLUDE_DIRS for part in p.parts):
        return False
    return True


def _safe_name(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "_", s).strip("_")


def _convert_tabular(root: Path) -> int:
    """Convert every spreadsheet (xlsx/xls/xlsm) and TSV under root to CSV,
    then delete the original. Lossless, local, no external service."""
    import pandas as pd
    made = 0
    for p in list(root.rglob("*")):
        if not p.is_file():
            continue
        suf = p.suffix.lower()
        try:
            if suf in SPREADSHEET_SUFFIXES:
                # Skip if the scraper already produced a same-named CSV (dedup).
                if (p.parent / f"{p.stem}.csv").exists():
                    p.unlink()
                    continue
                xls = pd.ExcelFile(p)
                for sheet in xls.sheet_names:
                    df = xls.parse(sheet)
                    if df.empty:
                        continue
                    # Keep the original stem (matches scraper naming); only sanitise
                    # the sheet suffix for multi-sheet files.
                    name = f"{p.stem}.csv" if len(xls.sheet_names) == 1 \
                        else f"{p.stem}__{_safe_name(sheet)}.csv"
                    df.to_csv(p.parent / name, index=False)
                    made += 1
                p.unlink()
            elif suf in TSV_SUFFIXES:
                if (p.parent / f"{p.stem}.csv").exists():
                    p.unlink()
                    continue
                pd.read_csv(p, sep="\t", dtype=str).to_csv(
                    p.parent / f"{p.stem}.csv", index=False)
                p.unlink()
                made += 1
            elif suf in JSONL_SUFFIXES:      # line-delimited JSON -> CSV
                if (p.parent / f"{p.stem}.csv").exists():
                    p.unlink()
                    continue
                pd.read_json(p, lines=True).to_csv(
                    p.parent / f"{p.stem}.csv", index=False)
                p.unlink()
                made += 1
        except Exception as e:  # noqa: BLE001 - never let one bad file kill the run
            log.warning("%s->csv failed for %s: %s", suf, p.name, e)
            if p.exists():
                p.unlink()
    return made


def _log_dropped_structured(root: Path, src: Source) -> None:
    """Warn if structured (json/xml) files exist that are NOT being published —
    so a scraper whose primary output is json/xml is never silently dropped."""
    dropped = {}
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in STRUCTURED_DROPPED_SUFFIXES:
            if not any(part in ARTIFACT_EXCLUDE_DIRS for part in p.parts):
                dropped[p.suffix.lower()] = dropped.get(p.suffix.lower(), 0) + 1
    if dropped:
        log.warning("[%s] dropped structured files (not published, expected if the "
                    "scraper parses them into CSV): %s", src.slug, dropped)


def _collect_in_place(src: Source, wd: Path, dd: Path) -> int:
    """Snapshot scraper output (written next to the script) into the run dir.

    Uses MOVE (not copy) when WIPE_SCRAPER_DIR is on: the scraper folder is freed
    after commit anyway, so moving frees each file immediately and keeps peak disk
    at one file instead of doubling the whole dataset (critical for multi-GB
    sources). Retries after a MOVE must be cleared from `upload_run`, not `scrape`.
    """
    move = WIPE_SCRAPER_DIR
    n = 0
    roots = [wd / sub for sub in src.output_subdirs] if src.output_subdirs else [wd]
    for root in roots:
        if not root.exists():
            log.warning("output path missing, skipping: %s", root)
            continue
        for p in list(root.rglob("*")) if root.is_dir() else [root]:
            if not _is_collectable(p):
                continue
            dest = dd / p.relative_to(wd)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if move:
                shutil.move(str(p), str(dest))
            else:
                shutil.copy2(p, dest)
            n += 1
    return n


def run_scraper(src: Source, run_id: str, timeout: int | None = None) -> None:
    wd = workdir(src)
    dd = data_dir(src, run_id)
    rd = run_dir(src, run_id)

    if not wd.is_dir():
        raise FileNotFoundError(f"scraper workdir not found: {wd}")

    # Fresh run dir so a retried run never mixes with a previous attempt.
    if rd.exists():
        shutil.rmtree(rd)
    dd.mkdir(parents=True, exist_ok=True)

    cmd = [sys.executable, src.entrypoint]
    if src.args:                     # extra CLI args/subcommand ({run_dir} substituted)
        cmd += src.args.format(run_dir=str(dd)).split()
    if src.xvfb:                     # headful browser needs a virtual X display
        cmd = ["xvfb-run", "-a"] + cmd

    log.info("[%s] running: %s (cwd=%s)", src.slug, " ".join(cmd), wd)
    proc = subprocess.run(cmd, cwd=str(wd), capture_output=True, text=True, timeout=timeout)
    log.info("[%s] stdout tail:\n%s", src.slug, proc.stdout[-2000:])
    if proc.returncode != 0:
        log.error("[%s] stderr tail:\n%s", src.slug, proc.stderr[-4000:])
        raise RuntimeError(f"scraper {src.slug} exited {proc.returncode}")


def collect(src: Source, run_id: str) -> None:
    """Snapshot the scraped artifacts into the run dir.

    Publishes CSV (structured) + raw documents (pdf/doc/ppt — handled downstream
    by the graph/vector pipeline). Spreadsheets are converted to CSV. Docs are
    NOT converted here.
    """
    wd = workdir(src)
    dd = data_dir(src, run_id)

    if src.in_place:
        _log_dropped_structured(wd, src)        # visibility: json/xml left behind
        _collect_in_place(src, wd, dd)          # copies CSV + docs + tabular
    else:
        _log_dropped_structured(dd, src)
        for p in list(dd.rglob("*")):           # direct-write: keep collectable only
            if p.is_file() and p.suffix.lower() not in _collectable_suffixes():
                p.unlink()

    conv = _convert_tabular(dd) if CONVERT_XLSX else 0
    csvs = sum(1 for p in dd.rglob("*") if p.is_file() and p.suffix.lower() in CSV_SUFFIXES)
    docs = sum(1 for p in dd.rglob("*") if p.is_file() and p.suffix.lower() in DOC_SUFFIXES)
    log.info("[%s] run dir has %d CSV (+%d converted from xlsx/tsv) and %d raw docs",
             src.slug, csvs, conv, docs)

    if csvs + docs == 0:
        raise RuntimeError(f"scraper {src.slug} produced no artifacts")


def _is_state_file(p: Path) -> bool:
    """True for a scraper's resume state (checkpoint / progress / SQLite).

    These are deliberately preserved by the post-commit wipe. Every scraper here
    already knows how to resume — via checkpoint.json, *_progress.json,
    tracker.json or a per-source SQLite DB — but wiping that state after each
    commit meant the next run always re-crawled the whole source from scratch.
    The state is KB-to-MB; the data it describes is GB.
    """
    name = p.name.lower()
    if p.suffix.lower() in STATE_FILE_SUFFIXES:
        return True
    if p.suffix.lower() not in {".json", ".txt", ".jsonl"}:
        return False
    return any(marker in name for marker in STATE_NAME_MARKERS)


def _wipe_workdir_data(src: Source) -> None:
    """Delete the scraper's own downloaded data (keep code/manifest/structure).

    Total scrape data exceeds local disk, so once a source is committed to S3
    (the source of truth) its local folder is freed. Runs only on the success
    path. Set WIPE_SCRAPER_DIR=0 to keep folders (uses more disk, enables resume).
    Resume state is kept regardless unless KEEP_SCRAPER_STATE=0.
    """
    wd = workdir(src)
    keep_names = {"requirements.txt", "manifest.yaml", ".gitignore", ".env", ".gitkeep"}
    keep_suffixes = {".py", ".md", ".yaml"}
    freed = 0
    kept_state = 0
    for p in wd.rglob("*"):
        if not p.is_file():
            continue
        if p.name in keep_names or p.suffix.lower() in keep_suffixes:
            continue
        if any(part in ARTIFACT_EXCLUDE_DIRS for part in p.parts):
            continue
        if KEEP_SCRAPER_STATE and _is_state_file(p):
            kept_state += 1
            continue
        try:
            freed += p.stat().st_size
            p.unlink()
        except OSError:
            pass
    if freed:
        log.info("[%s] freed %.1f MB of local scraper data (S3 has it)", src.slug, freed / 1e6)
    if kept_state:
        log.info("[%s] kept %d resume-state file(s) for the next run", src.slug, kept_state)


def cleanup_local(src: Source, run_id: str, keep: int | None = None) -> None:
    """Delete the local run snapshot after a successful S3 commit.

    Runs only on the success path, so a failed run keeps its local data for
    retry/inspection without re-scraping. S3 `_runs/` holds the durable backup.
    """
    keep = KEEP_LOCAL_RUNS if keep is None else keep
    slug_root = RUN_ROOT / src.slug

    if WIPE_SCRAPER_DIR:                 # free the scraper's own folder (disk mgmt)
        _wipe_workdir_data(src)

    if keep <= 0:
        rd = run_dir(src, run_id)
        if rd.exists():
            shutil.rmtree(rd)
            log.info("[%s] removed local run %s (S3 has the durable copy)", src.slug, rd)
        return

    # Keep the last `keep` local snapshots, delete older ones.
    if not slug_root.exists():
        return
    runs = sorted(p for p in slug_root.iterdir() if p.is_dir())
    for old in runs[:-keep] if len(runs) > keep else []:
        shutil.rmtree(old)
        log.info("[%s] pruned old local run %s", src.slug, old)
