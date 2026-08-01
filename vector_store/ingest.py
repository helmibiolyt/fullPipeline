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

from qdrant_client import models
from tqdm import tqdm

import s3_docs
import extract
import chunk as chunker
import embed
import qdrant_store
from config import COLLECTION


def indexed_etags() -> dict[str, str]:
    """s3_key -> etag for everything already in the collection.

    Reads only the first chunk of each document. The map holds one answer per
    document, but a document averages ~34 chunks, so an unfiltered scroll
    fetched 3.2M payloads to learn 93k facts - and with on_disk_payload that is
    3.2M disk reads on a 2 vCPU box. Measured at ~14 minutes, longer than the
    OCR work it was there to protect. Every chunker numbers offsets from 0
    within a document, so offset == 0 selects exactly one chunk per document.

    A document whose offset-0 chunk is missing reads as not indexed and gets
    re-ingested. That is the safe direction to fail: redundant work rather than
    silent omission.
    """
    out: dict[str, str] = {}
    client = qdrant_store.client()
    if not client.collection_exists(COLLECTION):
        return out
    first_chunk = models.Filter(must=[models.FieldCondition(
        key="offset", match=models.MatchValue(value=0))])
    offset = None
    while True:
        points, offset = client.scroll(
            COLLECTION, scroll_filter=first_chunk, limit=10_000, offset=offset,
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
    empty, empty_keys = 0, []
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
                if not chunks:
                    # Counted, not swallowed. A scan with no text layer and a
                    # download that returned an error page both land here, and
                    # both used to be indistinguishable from a document that
                    # ingested cleanly - the run reported success and the
                    # corpus was quietly missing it.
                    empty += 1
                    empty_keys.append(doc.s3_key)
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

    print(f"\ningested {n_chunks:,} chunks from {len(todo) - failed - empty:,} "
          f"documents ({failed:,} failed, {empty:,} produced no text)")
    if empty:
        # Named, not just counted. A scan tesseract could not read and a
        # download that was really an error page both land here, and both used
        # to be folded into the success count - the run reported a clean
        # ingest and the corpus was quietly missing those documents.
        print("  no text extracted - scans OCR could not read, or downloads "
              "that were an error page:")
        for k in empty_keys[:15]:
            print(f"    {k}")
        if len(empty_keys) > 15:
            print(f"    ... and {len(empty_keys) - 15:,} more")
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
