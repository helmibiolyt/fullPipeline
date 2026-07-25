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
    page: int | None = None
    offset: int = 0               # chunk index within the document
    # --- filter metadata (scopes retrieval) ---
    section: str | None = None    # e.g. "contraindications", "indications"
    language: str = "en"
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
