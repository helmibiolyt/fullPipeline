"""Deterministic run-path helpers.

Every task in a DAG run recomputes the same paths from the Airflow run_id, so
nothing needs to be passed via XCom.
"""
from __future__ import annotations

import re
from pathlib import Path

from .registry import Source
from .settings import RUN_ROOT, SCRAPE_ROOT


def sanitize_run_id(run_id: str) -> str:
    """Airflow run_id -> filesystem/S3-safe token."""
    return re.sub(r"[^0-9a-zA-Z]+", "-", run_id).strip("-")


def workdir(src: Source) -> Path:
    return SCRAPE_ROOT / src.topic / src.source


def run_dir(src: Source, run_id: str) -> Path:
    return RUN_ROOT / src.slug / sanitize_run_id(run_id)


def data_dir(src: Source, run_id: str) -> Path:
    """Where the run's artifacts are collected (the local temporary backup)."""
    return run_dir(src, run_id) / "data"


def manifest_path(src: Source, run_id: str) -> Path:
    return run_dir(src, run_id) / "_MANIFEST.json"
