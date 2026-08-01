"""Download the documents a scraper only wrote down the address of.

Runs after collect() and before validate_local(), for every source.

Several scrapers publish a CSV with a column holding a link to a PDF and stop
there - SFDA's Public Assessment Reports, its safety alerts, its
pharmacovigilance attachments. The row says the document exists and where it
is; the document itself never reaches the lake, so the vector store cannot
index what nobody downloaded.

Fixing that per scraper would mean the same loop written forty-nine times, and
the fiftieth scraper would forget. It belongs here, in the stage every source
already passes through, keyed on nothing scraper-specific: any cell that is a
URL ending in a document extension is a document to fetch.

WHAT IT WILL NOT DO

Fail the run. A dead link, a 403, an HTML error page served with a .pdf suffix
- all are recorded in the manifest and skipped. A scrape that produced good
CSVs must not be thrown away because a regulator's CDN was down, and the
manifest is what makes the difference visible instead of silent.

Re-download. Files already in the run directory are left alone, so a scraper
that fetches its own documents is not doubled up, and a re-run is cheap.
"""
from __future__ import annotations

import csv
import hashlib
import logging
import re
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

from .paths import data_dir
from .registry import Source

log = logging.getLogger(__name__)

#: Extensions worth fetching. Deliberately the same set collect() publishes -
#: adding one here without adding it there downloads a file that is then
#: dropped before upload.
DOC_EXT = (".pdf", ".doc", ".docx", ".ppt", ".pptx", ".dotx")

_URL = re.compile(r"^https?://", re.I)
_EXT = re.compile(r"\.(pdf|docx?|dotx|pptx?)(\?|#|$)", re.I)

#: Magic bytes per extension. A regulator that has lost a file usually serves
#: an HTML error page with the original .pdf URL and a 200, and the only way
#: to tell is to look at the first bytes. Saving that HTML would put a page
#: reading "Page not found" into the vector store as if it were a label.
_MAGIC = {".pdf": b"%PDF", ".doc": b"\xd0\xcf\x11\xe0", ".ppt": b"\xd0\xcf\x11\xe0",
          ".docx": b"PK\x03\x04", ".pptx": b"PK\x03\x04", ".dotx": b"PK\x03\x04"}

MAX_BYTES = 120 * 1024 * 1024      # a single document; anything larger is wrong
HOST_DELAY = 0.7                   # seconds between hits on one host
TIMEOUT = 45
RETRIES = 2
MANIFEST = "linked_documents.csv"

#: Sources whose CSV links are NOT followed.
#:
#: trialsearch.who.int aggregates 50-odd national registries, so its links
#: point wherever those registries host their files. Measured on the 648 it
#: publishes: 254 return HTTP 403 (ANZCTR blocks hotlinking to any user agent,
#: including a full Chrome one), others are dead, and what does come back is
#: whole trial protocols - one of them held both cores for over an hour in the
#: extract stage with nothing to show. The useful fraction is not worth the
#: failure rate or the time.
#:
#: Excluded here rather than by deleting the column: the URLs stay in the CSV,
#: where they are still a fact about the trial, and the graph can use them
#: without anything trying to fetch them.
SKIP_SOURCES = {
    "clinical_trials_pipeline_intelligence_trialsearch_who_int",
}


def _looks_like_doc(value: str) -> bool:
    return bool(value and _URL.match(value) and _EXT.search(value))


def _ext_of(url: str) -> str:
    m = _EXT.search(url)
    return "." + m.group(1).lower() if m else ".pdf"


def _safe_name(url: str, ext: str) -> str:
    """A filename that is stable, unique and safe on every filesystem.

    The basename alone is neither unique nor safe: two regulators both publish
    `report.pdf`, and SFDA's URLs are percent-encoded Arabic. The URL digest
    keeps it collision-free and makes a re-run resolve to the same name, which
    is what lets an existing file be recognised and skipped.
    """
    stem = Path(urlparse(url).path).stem or "document"
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")[:90] or "document"
    return f"{stem}__{hashlib.sha1(url.encode()).hexdigest()[:10]}{ext}"


def find_links(root: Path) -> list[dict]:
    """Every document URL in every CSV under root, with where it came from."""
    seen: set[str] = set()
    out: list[dict] = []
    for path in sorted(root.rglob("*.csv")):
        if path.name == MANIFEST:
            continue
        try:
            with path.open(encoding="utf-8-sig", errors="replace", newline="") as fh:
                for i, row in enumerate(csv.DictReader(fh)):
                    for col, val in row.items():
                        val = (val or "").strip()
                        if not _looks_like_doc(val) or val in seen:
                            continue
                        seen.add(val)
                        out.append({"url": val, "source_csv": path.name,
                                    "column": col or "", "row": i})
        except Exception as e:                                 # noqa: BLE001
            log.warning("linked_docs: cannot read %s (%s)", path.name, e)
    return out


def _download(session, url: str, dest: Path) -> tuple[bool, str]:
    ext = dest.suffix.lower()
    for attempt in range(RETRIES + 1):
        try:
            r = session.get(url, timeout=TIMEOUT, stream=True,
                            headers={"User-Agent": "biolyt-pipeline/1.0"})
            if r.status_code != 200:
                return False, f"http {r.status_code}"
            body = bytearray()
            for chunk in r.iter_content(65536):
                body += chunk
                if len(body) > MAX_BYTES:
                    return False, f"larger than {MAX_BYTES // 1024 // 1024} MB"
            if not body:
                return False, "empty body"
            magic = _MAGIC.get(ext)
            if magic and not bytes(body[:len(magic)]) == magic:
                # Served with a 200 and the right extension, but it is not the
                # file - almost always an HTML error page.
                return False, "content is not " + ext.lstrip(".")
            dest.write_bytes(bytes(body))
            return True, ""
        except Exception as e:                                 # noqa: BLE001
            if attempt == RETRIES:
                return False, f"{type(e).__name__}: {str(e)[:80]}"
            time.sleep(2 * (attempt + 1))
    return False, "unreachable"


def fetch_linked(src: Source, run_id: str, limit: int | None = None) -> dict:
    """Fetch every document linked from this run's CSVs. Never raises."""
    import requests

    if src.slug in SKIP_SOURCES:
        log.info("[%s] linked_docs: source is on the skip list, not following "
                 "its links", src.slug)
        return {"found": 0, "downloaded": 0, "skipped": 0, "failed": 0}

    dd = data_dir(src, run_id)
    links = find_links(dd)
    if not links:
        log.info("[%s] linked_docs: no document URLs in this run's CSVs", src.slug)
        return {"found": 0, "downloaded": 0, "skipped": 0, "failed": 0}

    outdir = dd / "documents"
    outdir.mkdir(parents=True, exist_ok=True)
    # Anything the scraper already fetched, by name, so its own downloads are
    # not repeated under a second filename.
    already = {p.name for p in dd.rglob("*") if p.is_file()}

    session = requests.Session()
    last_hit: dict[str, float] = defaultdict(float)
    rows, got, skipped, failed = [], 0, 0, 0

    for link in links[:limit] if limit else links:
        url = link["url"]
        ext = _ext_of(url)
        name = _safe_name(url, ext)
        dest = outdir / name
        if dest.exists() or name in already:
            skipped += 1
            rows.append({**link, "file": name, "bytes": dest.stat().st_size
                         if dest.exists() else "", "status": "already present",
                         "sha256": ""})
            continue

        host = urlparse(url).netloc
        wait = HOST_DELAY - (time.time() - last_hit[host])
        if wait > 0:
            time.sleep(wait)
        ok, why = _download(session, url, dest)
        last_hit[host] = time.time()

        if ok:
            got += 1
            data = dest.read_bytes()
            rows.append({**link, "file": name, "bytes": len(data),
                         "status": "ok",
                         "sha256": hashlib.sha256(data).hexdigest()})
        else:
            failed += 1
            rows.append({**link, "file": "", "bytes": "", "status": why,
                         "sha256": ""})

    # The manifest is the point, not a by-product: it is the only record of
    # which row of which CSV a document belongs to, and the only place a link
    # that has rotted is visible. It ships with the run.
    with (dd / MANIFEST).open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["url", "file", "bytes", "sha256",
                                           "status", "source_csv", "column", "row"])
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in w.fieldnames})

    log.info("[%s] linked_docs: %d urls -> %d downloaded, %d already present, "
             "%d failed", src.slug, len(links), got, skipped, failed)
    return {"found": len(links), "downloaded": got, "skipped": skipped,
            "failed": failed}
