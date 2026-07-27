"""The Chunk model — one row per document chunk, with provenance + filter metadata."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict


def make_chunk_id(doc_id: str, offset: int) -> str:
    """Stable id so re-ingesting the same doc upserts (never duplicates)."""
    return hashlib.sha256(f"{doc_id}:{offset}".encode()).hexdigest()[:32]


@dataclass
class Chunk:
    chunk_id: str                 # hash(doc_id + offset) — Qdrant point id
    text: str
    # --- provenance (answer -> source) ---
    source: str                   # e.g. "ema.europa.eu"
    doc_id: str                   # stable per document (e.g. s3 key stem)
    s3_key: str                   # exact object in moine-data
    page: int | None = None       # first page this chunk covers
    page_to: int | None = None    # last page; a section can span several
    offset: int = 0               # chunk index within the document
    # --- filter metadata (scopes retrieval) ---
    section: str | None = None    # e.g. "contraindications", "indications"
    section_code: str | None = None  # EU SPC number, e.g. "4.8" — None off the SPC path
    language: str = "en"
    # Which branch of the chunking cascade produced this: spc | pil | heading |
    # semantic | fixed. Recorded so the distribution can be measured after a
    # run. If SPC template detection breaks, everything still looks healthy -
    # same chunk counts, working retrieval - and only section filters go quiet.
    chunk_path: str = "fixed"
    molecule_id: str | None = None  # filled later when the graph links a fact

    def payload(self) -> dict:
        """Qdrant payload (everything except the vectors)."""
        return asdict(self)

    @staticmethod
    def new(text, source, doc_id, s3_key, offset, **meta) -> "Chunk":
        return Chunk(
            chunk_id=make_chunk_id(doc_id, offset),
            text=text, source=source, doc_id=doc_id, s3_key=s3_key, offset=offset,
            **meta,
        )
