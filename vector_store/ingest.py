#!/usr/bin/env python3
"""Ingest raw documents from S3 into Qdrant, skipping what is already indexed.

    python ingest.py                                    # everything not yet indexed
    python ingest.py --prefix Regulatory_Approvals/ema.europa.eu
    python ingest.py --force                            # re-embed regardless
    python ingest.py --prune                            # also drop vectors for deleted docs

Incremental by ETag. The ETag arrives free with the S3 listing, so deciding to
skip a document costs nothing - no download, no extract, no embed. That matters
twice over: a weekly sync touches only what changed, and a backfill interrupted
at 60% resumes instead of starting again.

Idempotent regardless: chunk ids are hash(doc_id + offset), so a re-ingest
upserts rather than duplicating.
"""
import argparse
import os
import tempfile
from collections import Counter

from tqdm import tqdm

import s3_docs
import extract
import chunk as chunker
import embed
import qdrant_store
from config import COLLECTION


def indexed_etags() -> dict[str, str]:
    """s3_key -> etag for everything already in the collection.

    One scroll over the payloads. At a few million chunks this is seconds and a
    few hundred MB of dict, against hours of re-embedding avoided.
    """
    out: dict[str, str] = {}
    client = qdrant_store.client()
    if not client.collection_exists(COLLECTION):
        return out
    offset = None
    while True:
        points, offset = client.scroll(
            COLLECTION, limit=10_000, offset=offset,
            with_payload=["s3_key", "etag"], with_vectors=False)
        for p in points:
            k = (p.payload or {}).get("s3_key")
            if k:
                out[k] = (p.payload or {}).get("etag", "")
        if offset is None:
            return out


def ingest(prefix: str = "", limit: int | None = None, batch: int = 64,
           force: bool = False, prune: bool = False):
    qdrant_store.ensure_collection()

    docs = list(s3_docs.list_docs(prefix=prefix, limit=limit))
    known = {} if force else indexed_etags()

    todo, unchanged = [], 0
    for d in docs:
        if not force and known.get(d.s3_key, None) == d.etag and d.etag:
            unchanged += 1
        else:
            todo.append(d)

    print(f"{len(docs):,} documents in S3 | {unchanged:,} unchanged (skipped) | "
          f"{len(todo):,} to ingest")

    # Documents that vanished from S3. Only meaningful for mirror:true sources,
    # where a run legitimately removes files; without this their vectors linger
    # and retrieval keeps citing documents that no longer exist.
    if prune and not prefix:
        gone = set(known) - {d.s3_key for d in docs}
        if gone:
            print(f"pruning {len(gone):,} document(s) no longer in S3")
            qdrant_store.delete_by_s3_keys(sorted(gone))

    if not todo:
        print("nothing to do")
        return

    pending, n_chunks, failed = [], 0, 0
    paths = Counter()
    with tempfile.TemporaryDirectory() as tmp:
        for doc in tqdm(todo, desc="docs"):
            try:
                local = s3_docs.download(doc, tmp)
                blocks = extract.extract_blocks(local)
                os.remove(local)
                chunks = chunker.chunk_document(
                    blocks, doc.source, doc.doc_id, doc.s3_key,
                    doc_type=doc.doc_type)
                for c in chunks:
                    c.etag = doc.etag
                    paths[c.chunk_path] += 1
                pending.extend(chunks)
            except Exception as e:  # noqa: BLE001 - one bad doc must not stop the run
                failed += 1
                print(f"  skip {doc.s3_key}: {type(e).__name__}: {e}")
            while len(pending) >= batch:
                take, pending = pending[:batch], pending[batch:]
                _flush(take)
                n_chunks += len(take)
        if pending:
            _flush(pending)
            n_chunks += len(pending)

    print(f"\ningested {n_chunks:,} chunks from {len(todo) - failed:,} documents "
          f"({failed:,} failed)")
    # The distribution is the check that template detection still works. If SPCs
    # stop landing on the spc path, everything still looks healthy - same counts,
    # working retrieval - and only the section filters go quiet.
    print(f"chunk_path: {dict(paths)}")
    print(f"collection now holds "
          f"{qdrant_store.client().get_collection(COLLECTION).points_count:,} points")


def _flush(chunks):
    embs = embed.embed_passages([c.text for c in chunks])
    qdrant_store.upsert(chunks, embs)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", default="")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--force", action="store_true",
                    help="re-embed even if the ETag is unchanged")
    ap.add_argument("--prune", action="store_true",
                    help="delete vectors for documents no longer in S3")
    a = ap.parse_args()
    ingest(a.prefix, a.limit, a.batch, a.force, a.prune)
