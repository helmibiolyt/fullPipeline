#!/usr/bin/env python3
"""Backfill ingest: parallel extract/chunk on CPU, batched embedding on GPU.

The sequential ingest.py runs one document at a time - download, extract, chunk,
embed - and on a 120-core pod that leaves 119 cores idle while the GPU sits at
0% waiting for work. Measured: 1.07 s/document, i.e. 28 hours for 93,430
documents. The GPU was never the bottleneck; feeding it was.

Here the two stages are separated:

    N worker processes   S3 download -> PDF extract -> chunk      (CPU bound)
    main process         embed in large batches -> upsert         (GPU bound)

Workers never load the embedding model. Doing so would need 120 copies of
bge-m3 against 24 GB of VRAM, so workers run SEMANTIC_MODE=paragraph and the
untemplated documents get paragraph boundaries rather than embedding
breakpoints. That affects the ~18% with no template; the structured 82% are
unaffected, since their boundaries come from headings and need no model.

Incremental and resumable exactly as ingest.py: unchanged ETags are skipped
before a worker is handed anything.
"""
from __future__ import annotations

import argparse
import multiprocessing
import os
import time

# A spawned worker is identifiable by its process name, which multiprocessing
# sets while unpacking the parent's state - before it imports this module, so
# the check is already valid here. (sys.argv is not usable for this: spawn
# overwrites the child's argv with the parent's during that same step.)
# Each worker loads a HuggingFace fast tokenizer, whose Rust rayon pool sizes
# itself to the machine - 120 threads here, times 64 workers, is ~7,700 threads and thread creation starts
# failing with EAGAIN. Workers only tokenize to measure chunk length, so one
# thread each is plenty. The parent is left alone deliberately: it tokenizes
# 1024-passage batches for the GPU, where multi-threading is worth real time.
if multiprocessing.current_process().name != "MainProcess":
    for _v in ("RAYON_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
               "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[_v] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed

# Workers must not try to embed - set before chunk.py is imported anywhere.
os.environ.setdefault("SEMANTIC_MODE", "paragraph")

import s3_docs                      # noqa: E402
import qdrant_store                 # noqa: E402
from config import COLLECTION       # noqa: E402
from ingest import indexed_etags    # noqa: E402

_S3 = None
_OCR = False


def _worker_init(ocr=False):
    """One boto3 client per worker; they are not fork-safe to share."""
    global _S3, _OCR
    import boto3
    from config import AWS_REGION
    _S3 = boto3.client("s3", region_name=AWS_REGION)
    _OCR = ocr


def _ocr_pages(doc, dpi=200):
    """Render each page and read it with Tesseract.

    Two populations of document arrive here, and neither yields a single
    character to normal extraction:

    * scanned filings - the page is a photograph of paper (EMA)
    * flattened PDFs - the text was converted to vector outlines, so every
      glyph is a drawing operation rather than a character (MHRA PILs)

    Rendering collapses that distinction: both produce a legible bitmap. 200
    dpi is enough for body text at these page sizes and keeps the bitmaps
    small enough to OCR quickly.
    """
    import pytesseract
    from PIL import Image
    out = []
    for i, page in enumerate(doc, 1):
        pm = page.get_pixmap(dpi=dpi)
        img = Image.frombytes("RGB", (pm.width, pm.height), pm.samples)
        txt = pytesseract.image_to_string(img)
        if txt.strip():
            out.append((i, txt))
    return out


def _process(doc_tuple):
    """Download, extract and chunk one document. Returns (chunks, error)."""
    import io
    import fitz
    import chunk as chunker
    from config import S3_BUCKET

    s3_key, source, doc_id, etag, doc_type = doc_tuple
    try:
        buf = io.BytesIO()
        _S3.download_fileobj(S3_BUCKET, s3_key, buf)
        raw = buf.getvalue()
        suffix = s3_key.rsplit(".", 1)[-1].lower()
        if suffix != "pdf":
            return [], None                     # non-PDF handled by ingest.py
        with fitz.open(stream=raw, filetype="pdf") as d:
            blocks = [(i, p.get_text("text")) for i, p in enumerate(d, 1)]
            # A PDF with no extractable text is not an error and raises
            # nothing - it just produces no chunks, records no ETag, and gets
            # retried forever by every later run. 1,034 documents sat in that
            # state after the backfill. OCR is the only way to read them.
            if _OCR and not any(t.strip() for _, t in blocks):
                blocks = _ocr_pages(d)
        chunks = chunker.chunk_document(blocks, source, doc_id, s3_key,
                                        doc_type=doc_type)
        for c in chunks:
            c.etag = etag
        return chunks, None
    except Exception as e:                       # noqa: BLE001
        return [], f"{type(e).__name__}: {e}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", default="")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=max(8, (os.cpu_count() or 8) // 2))
    ap.add_argument("--embed-batch", type=int, default=512)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--ocr", action="store_true",
                    help="OCR documents that yield no extractable text")
    a = ap.parse_args()

    qdrant_store.ensure_collection()
    docs = list(s3_docs.list_docs(prefix=a.prefix, limit=a.limit))
    known = {} if a.force else indexed_etags()

    todo = [(d.s3_key, d.source, d.doc_id, d.etag, d.doc_type) for d in docs
            if a.force or known.get(d.s3_key) != d.etag or not d.etag]
    print(f"{len(docs):,} documents | {len(docs) - len(todo):,} unchanged (skipped) | "
          f"{len(todo):,} to ingest | {a.workers} workers", flush=True)
    if not todo:
        print("nothing to do")
        return

    import embed                                  # loads bge-m3 on the GPU, once
    t0 = time.time()
    pending, n_chunks, failed, done, lost = [], 0, 0, 0, 0
    paths = Counter()

    def flush(batch):
        # A batch that cannot be written after all retries must not unwind a
        # multi-hour backfill. It is skipped and counted: because the skip test
        # is "is this document's ETag already in Qdrant", the next run simply
        # picks these documents up again.
        nonlocal n_chunks, lost
        if not batch:
            return
        embs = embed.embed_passages([c.text for c in batch])
        try:
            qdrant_store.upsert(batch, embs)
            n_chunks += len(batch)
        except Exception as e:
            lost += len(batch)
            print(f"  upsert failed for {len(batch)} chunks ({lost:,} lost "
                  f"so far): {type(e).__name__}: {e}", flush=True)

    # spawn, not fork. ProcessPoolExecutor creates workers lazily, so the first
    # few fork cleanly and then the first embed call initialises CUDA in the
    # parent - after which every newly forked worker inherits a broken CUDA
    # context and deadlocks. Observed exactly that: ~19,500 chunks ingested,
    # then 3 processes alive instead of 64 and the parent stuck in
    # futex_wait_queue. spawn starts each worker from a clean interpreter.
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=a.workers, initializer=_worker_init,
                             initargs=(a.ocr,), mp_context=ctx) as ex:
        futures = {ex.submit(_process, t): t[0] for t in todo}
        for fut in as_completed(futures):
            chunks, err = fut.result()
            done += 1
            if err:
                failed += 1
                if failed <= 20:
                    print(f"  skip {futures[fut]}: {err}", flush=True)
            for c in chunks:
                paths[c.chunk_path] += 1
            pending.extend(chunks)
            while len(pending) >= a.embed_batch:
                flush(pending[:a.embed_batch])
                pending = pending[a.embed_batch:]
            if done % 500 == 0:
                el = time.time() - t0
                rate = done / el
                eta = (len(todo) - done) / rate / 60 if rate else 0
                print(f"  {done:,}/{len(todo):,} docs  {n_chunks:,} chunks  "
                      f"{rate:.1f} docs/s  ETA {eta:.0f} min", flush=True)
    flush(pending)

    el = time.time() - t0
    print(f"\ningested {n_chunks:,} chunks from {done - failed:,} documents "
          f"({failed:,} failed, {lost:,} chunks lost to upsert errors) "
          f"in {el/60:.1f} min")
    print(f"chunk_path: {dict(paths)}")
    print(f"collection now holds "
          f"{qdrant_store.client().get_collection(COLLECTION).points_count:,} points")


if __name__ == "__main__":
    main()
