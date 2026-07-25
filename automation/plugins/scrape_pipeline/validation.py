"""Steps 2-3: validate the local write succeeded and build a manifest.

The manifest (sha256 + size per file) is the contract that S3 verification
checks against before the pipeline is allowed to touch live data.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .paths import data_dir, manifest_path
from .registry import Source

log = logging.getLogger(__name__)


def _sha256(p: Path, chunk=1 << 20) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def validate_local(src: Source, run_id: str) -> dict:
    dd = data_dir(src, run_id)
    if not dd.is_dir():
        raise FileNotFoundError(f"data dir missing: {dd}")

    files = [p for p in dd.rglob("*") if p.is_file()]
    if not files:
        raise ValueError(f"[{src.slug}] no files in data dir {dd}")

    entries = []
    empty = []
    for p in sorted(files):
        size = p.stat().st_size
        if size == 0:
            empty.append(str(p.relative_to(dd)))
        entries.append(
            {
                "path": str(p.relative_to(dd)),
                "size": size,
                "sha256": _sha256(p),
            }
        )

    if empty:
        # Zero-byte files almost always mean a broken scrape; fail loudly.
        raise ValueError(f"[{src.slug}] {len(empty)} empty file(s): {empty[:10]}")

    manifest = {
        "run_id": run_id,
        "source": src.s3_base,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "n_files": len(entries),
        "total_bytes": sum(e["size"] for e in entries),
        "files": entries,
    }
    mp = manifest_path(src, run_id)
    mp.write_text(json.dumps(manifest, indent=2))
    log.info(
        "[%s] validated %d files, %.1f MB -> %s",
        src.slug,
        manifest["n_files"],
        manifest["total_bytes"] / 1e6,
        mp,
    )
    return manifest
