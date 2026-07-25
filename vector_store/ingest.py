#!/usr/bin/env python3
"""Ingest raw documents from S3 into Qdrant.

    python ingest.py                 # all docs in the bucket
    python ingest.py --prefix Regulatory_Approvals/ema.europa.eu --limit 20

Idempotent: chunk ids are content-stable, so re-running upserts (no duplicates).
"""
import argparse
import tempfile
import os

from tqdm import tqdm

import s3_docs
import extract
import chunk as chunker
import embed
import qdrant_store


def ingest(prefix: str = "", limit: int | None = None, batch: int = 64):
    qdrant_store.ensure_collection()
    docs = list(s3_docs.list_docs(prefix=prefix, limit=limit))
    print(f"{len(docs)} documents to ingest")

    pending_chunks, n_chunks = [], 0
    with tempfile.TemporaryDirectory() as tmp:
        for doc in tqdm(docs, desc="docs"):
            try:
                local = s3_docs.download(doc, tmp)
                blocks = extract.extract_blocks(local)
                os.remove(local)
                chunks = chunker.chunk_document(blocks, doc.source, doc.doc_id, doc.s3_key)
                pending_chunks.extend(chunks)
            except Exception as e:  # noqa: BLE001 - one bad doc must not stop ingest
                print(f"  skip {doc.s3_key}: {e}")
            while len(pending_chunks) >= batch:
                take, pending_chunks = pending_chunks[:batch], pending_chunks[batch:]
                _flush(take)
                n_chunks += len(take)
        if pending_chunks:
            _flush(pending_chunks)
            n_chunks += len(pending_chunks)
    print(f"ingested {n_chunks} chunks into Qdrant")


def _flush(chunks):
    embs = embed.embed_passages([c.text for c in chunks])
    qdrant_store.upsert(chunks, embs)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", default="")
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()
    ingest(a.prefix, a.limit)
