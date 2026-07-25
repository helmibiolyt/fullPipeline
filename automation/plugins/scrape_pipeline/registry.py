"""Auto-discover scrapers by scanning scrape/<Topic>/<source>/manifest.yaml.

Topic and source are derived from the folder path, so a contributor only needs
access to the scrape/ folder — dropping in a scraper + manifest.yaml is enough
for the pipeline to pick it up as a new node. No central registry to edit.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import yaml

from .settings import MANIFEST_NAME, SCRAPE_ROOT

log = logging.getLogger(__name__)


@dataclass
class Source:
    topic: str
    source: str
    entrypoint: str
    in_place: bool = True
    args: str = ""
    output_subdirs: List[str] = field(default_factory=list)
    size_class: str = "small"
    timeout_min: int = 120
    # True = full replace (delete live objects absent from the new run).
    # False = additive/upsert (copy new over old, never delete).
    mirror: bool = True
    # False = discovered but NOT built into the DAG (not yet verified / WIP).
    enabled: bool = True
    # True = scheduled after every other source finishes (runs alone at the end).
    run_last: bool = False
    # True = run under xvfb-run (headful browser needs a virtual X display).
    xvfb: bool = False

    @property
    def slug(self) -> str:
        raw = f"{self.topic}__{self.source}"
        return re.sub(r"[^0-9a-zA-Z]+", "_", raw).strip("_").lower()

    @property
    def s3_base(self) -> str:
        return f"{self.topic}/{self.source}"


def _from_manifest(manifest: Path) -> Source:
    source_dir = manifest.parent
    m = yaml.safe_load(manifest.read_text()) or {}
    if "entrypoint" not in m:
        raise ValueError(f"{manifest}: missing required key 'entrypoint'")
    return Source(
        topic=source_dir.parent.name,          # scrape/<Topic>/<source>/
        source=source_dir.name,
        entrypoint=m["entrypoint"],
        in_place=m.get("in_place", True),
        args=m.get("args", "") or "",
        output_subdirs=m.get("output_subdirs") or [],
        size_class=m.get("size_class", "small"),
        timeout_min=int(m.get("timeout_min", 120)),
        mirror=m.get("mirror", True),
        enabled=m.get("enabled", True),
        run_last=m.get("run_last", False),
        xvfb=m.get("xvfb", False),
    )


def load_sources(scrape_root=SCRAPE_ROOT) -> List[Source]:
    """Discover every scrape/<Topic>/<source>/<MANIFEST_NAME>."""
    root = Path(scrape_root)
    out: List[Source] = []
    for manifest in sorted(root.glob(f"*/*/{MANIFEST_NAME}")):
        topic = manifest.parent.parent.name
        source = manifest.parent.name
        # Skip hidden / template / meta folders.
        if topic.startswith((".", "_")) or source.startswith((".", "_")):
            continue
        try:
            out.append(_from_manifest(manifest))
        except Exception as e:  # noqa: BLE001 - one bad manifest must not hide the rest
            log.error("skipping invalid manifest %s: %s", manifest, e)
    return out
