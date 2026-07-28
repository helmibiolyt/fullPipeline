"""Qdrant collection with hybrid (dense + sparse) vectors and metadata filtering."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from functools import lru_cache

from qdrant_client import QdrantClient, models

from config import (QDRANT_URL, QDRANT_PATH, QDRANT_API_KEY, COLLECTION,
                    EMBED_DIM, MIN_CHUNK_BUCKET)
from schema import Chunk


@lru_cache(maxsize=1)
def client() -> QdrantClient:
    # Server mode if a URL is given (Docker/Cloud); else embedded local — no Docker.
    if QDRANT_URL:
        # The client default is a 5 s timeout, which a multi-MB upsert to a
        # 2 vCPU box exceeds routinely.
        return QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, timeout=120)
    return QdrantClient(path=QDRANT_PATH)


def ensure_collection():
    """Create the collection sized for ~3.9M chunks on a shared 15 GB box.

    Three settings do the work, and without them this does not fit:

    * int8 scalar quantization - 3.9M x 1024 dims as float32 is ~16 GB of RAM.
      Quantized to one byte per dimension it is ~4 GB, which is the difference
      between running and being OOM-killed alongside Airflow, the search API and
      (later) Neo4j. Recall loss at this scale is negligible.
    * on_disk vectors - full-precision copies stay on disk and are read only for
      rescoring; the quantized ones live in RAM. always_ram=True on the
      quantized side is what keeps search fast.
    * on_disk_payload - chunk text averages ~1.3 KB, so 3.9M payloads is ~6 GB.
      That belongs on disk, not in memory; it is fetched only for the handful of
      results actually returned.
    """
    c = client()
    if not c.collection_exists(COLLECTION):
        _create(c)
    _ensure_indexes(c)


def _create(c):
    c.create_collection(
        collection_name=COLLECTION,
        vectors_config={"dense": models.VectorParams(
            size=EMBED_DIM,
            distance=models.Distance.COSINE,
            on_disk=True)},
        sparse_vectors_config={"sparse": models.SparseVectorParams(
            index=models.SparseIndexParams(on_disk=True))},
        quantization_config=models.ScalarQuantization(
            scalar=models.ScalarQuantizationConfig(
                type=models.ScalarType.INT8,
                always_ram=True)),
        on_disk_payload=True,
    )

# Payload indexes for the fields retrieval filters on. Filtering happens before
# search, so "adverse effects of drug X" narrows to a handful of chunks rather
# than scanning millions.
KEYWORD_INDEXES = (
    "source", "doc_id", "doc_type", "section", "section_code",
    "chunk_path", "language", "molecule_id",
    # s3_key and etag drive incremental sync: the skip check reads them, and
    # prune filters on s3_key.
    "s3_key", "etag",
)


def _ensure_indexes(c):
    """Create any missing payload index, on existing collections too.

    This used to run only on creation, so an index added later never reached a
    collection already holding data - and the sync path that needs `offset`
    indexed is exactly that case. Existing indexes are left alone rather than
    recreated, which on a few million points is not free.
    """
    existing = set((c.get_collection(COLLECTION).payload_schema or {}).keys())
    for field in KEYWORD_INDEXES:
        if field not in existing:
            c.create_payload_index(COLLECTION, field,
                                   models.PayloadSchemaType.KEYWORD)
    # Integer, not keyword: indexed_etags() filters on offset == 0 to read one
    # chunk per document instead of all of them.
    for field in ("offset", "len_bucket"):
        # Integer, not keyword: indexed_etags() filters offset == 0 to read one
        # chunk per document, and search filters len_bucket to skip fragments.
        if field not in existing:
            c.create_payload_index(COLLECTION, field,
                                   models.PayloadSchemaType.INTEGER)


# One HTTP request per this many points. A 1024-point batch carries 1024 dense
# floats + sparse + ~1.3 KB of text each - roughly 20 MB of JSON, which a 2 vCPU
# Qdrant takes long enough to serve that the connection gets dropped mid-request.
UPSERT_BATCH = 256
UPSERT_RETRIES = 5


def _upsert_batch(points, wait):
    """Upsert one sub-batch, retrying on transport failures.

    Qdrant closed the connection mid-upsert during the backfill
    ("Server disconnected without sending a response") and the exception
    unwound the whole run. Retries are safe because point ids are chunk_ids:
    replaying a batch that did land is an overwrite, not a duplicate. The
    cached client is dropped between attempts so a poisoned keep-alive
    connection is not reused.
    """
    for attempt in range(UPSERT_RETRIES):
        try:
            client().upsert(COLLECTION, points=points, wait=wait)
            return
        except Exception:
            if attempt == UPSERT_RETRIES - 1:
                raise
            client.cache_clear()
            time.sleep(2 ** attempt)


def upsert(chunks: list[Chunk], embeddings: list[dict], wait: bool = False):
    """Idempotent: point id = chunk_id, so re-ingest overwrites, never duplicates.

    wait=False by default. With wait=True every batch blocks until Qdrant has
    finished indexing it, and over a network hop to a disk-backed collection that
    became the whole pipeline's bottleneck: 96 CPU workers and a 4090 both idle,
    throughput identical at 48 and 96 workers because neither was the constraint.
    Qdrant queues the write and indexes asynchronously; durability is unchanged,
    only the acknowledgement is deferred.
    """
    points = [
        models.PointStruct(
            id=ch.chunk_id,
            vector={
                "dense": emb["dense"],
                "sparse": models.SparseVector(
                    indices=list(emb["sparse"].keys()),
                    values=list(emb["sparse"].values())),
            },
            payload=ch.payload(),
        )
        for ch, emb in zip(chunks, embeddings)
    ]
    for i in range(0, len(points), UPSERT_BATCH):
        _upsert_batch(points[i:i + UPSERT_BATCH], wait)


def delete_by_s3_keys(keys: list[str], batch: int = 200):
    """Remove every chunk belonging to these documents.

    Needed for mirror:true sources, where a run legitimately deletes files. With
    no prune, their vectors survive and retrieval keeps citing documents that are
    no longer in the bucket - stale answers that look perfectly well sourced.
    """
    c = client()
    for i in range(0, len(keys), batch):
        c.delete(
            collection_name=COLLECTION,
            points_selector=models.FilterSelector(filter=models.Filter(
                should=[models.FieldCondition(key="s3_key",
                                              match=models.MatchValue(value=k))
                        for k in keys[i:i + batch]])),
            wait=True,
        )


# Search the int8 vectors quantization keeps in RAM and skip rescoring against
# the full-precision copies, which are on_disk. Worth ~20% (35 ms -> 28 ms
# median over 6 fresh queries each way) with identical results: top-10 overlap
# was 100%.
#
# It is a small win, and it is NOT what makes queries fast. Search latency here
# is governed by whether Qdrant's quantized vectors are actually resident:
# with ~1.9 GB of them paged into swap, dense search took 1.5 s; with the same
# collection fully resident it takes 74 ms. always_ram=True is a request, not a
# guarantee - the kernel will page it out under pressure. Keep vm.swappiness
# low and leave the box enough headroom, or none of this matters.
NO_RESCORE = models.SearchParams(
    quantization=models.QuantizationSearchParams(rescore=False))


@dataclass
class Hit:
    """One search result. Qdrant's ScoredPoint is a pydantic model and rejects
    extra attributes, so the fused and dense scores travel on this instead."""
    id: object
    payload: dict
    fused: float                    # reciprocal-rank fusion of dense + sparse
    cosine: float | None = None     # dense similarity; None if sparse-only

    @property
    def score(self):                # what callers used before fusion moved here
        return self.fused


def dense_vectors(ids: list) -> dict:
    """Dense vector per point id, for comparing results to each other.

    Fetched by id rather than asked for during the search: with_vectors=True on
    query_points cost 680 ms against 69 ms without, while retrieving the same
    vectors by id afterwards is 26 ms for 120. Re-embedding the texts instead
    is not an option here - 120 passages on this CPU did not finish in 500 s.
    """
    recs = client().retrieve(COLLECTION, ids=ids, with_vectors=["dense"],
                             with_payload=False)
    return {r.id: r.vector["dense"] for r in recs}


def hybrid_search(q_emb: dict, top_k: int, flt: dict | None = None):
    """Candidates from dense AND sparse, all ranked by dense cosine.

    Sparse still does the recall work it is there for - exact molecule names,
    licence numbers, anything a 1024-dim embedding blurs - but the final order
    comes from cosine against the query.

    This replaced reciprocal-rank fusion, which lost on every measure tried:

        latency              RRF 27 ms      cosine 20 ms   (one query, not two)
        hits carrying a score    30 of 50        50 of 50
        top-5 for "contraindications of atorvastatin in liver disease"
                             2 of 5 were PAR posology     5 of 5 were 4.3

    The scoring gap is the real argument. RRF returns 1/rank, so a perfect
    match and a nonsense query both come back as [0.5, 0.5, 0.333, ...] - there
    is no way to say "nothing here is relevant", ties are pervasive so the
    order shifts between identical queries, and the 20 hits found only by
    sparse search carried no score at all. Cosine gives every candidate a
    comparable number, which is what MIN_SCORE needs to work.

    What cosine does NOT fix: it compares the query and the chunk after each
    has been compressed to 1024 numbers separately, so it scores topic overlap
    rather than whether the passage answers the question. Measured: for "is
    this medicine safe during pregnancy", a chunk about driving scored 0.696
    while "contraindicated during pregnancy" scored 0.665; and "contraindicated
    of atorvastatin" separates atorvastatin (0.732) from rosuvastatin (0.653)
    by only 0.08. Only a cross-encoder closes that, and it costs 122 s a query
    on this host. Returning 15 chunks to an agent that reads all of them is the
    cheaper answer.
    """
    must = [models.FieldCondition(key=k, match=models.MatchValue(value=v))
            for k, v in (flt or {}).items() if v is not None]
    if MIN_CHUNK_BUCKET > 0:
        # Applied here, not after the search: fragments occupy every top slot
        # for short queries, so filtering the results would leave nothing.
        must.append(models.FieldCondition(
            key="len_bucket", range=models.Range(gte=MIN_CHUNK_BUCKET)))
    qfilter = models.Filter(must=must) if must else None
    res = client().query_points(
        collection_name=COLLECTION,
        prefetch=[
            models.Prefetch(query=q_emb["dense"], using="dense", limit=top_k,
                            filter=qfilter, params=NO_RESCORE),
            models.Prefetch(
                query=models.SparseVector(
                    indices=list(q_emb["sparse"].keys()),
                    values=list(q_emb["sparse"].values())),
                using="sparse", limit=top_k, filter=qfilter, params=NO_RESCORE),
        ],
        query=q_emb["dense"], using="dense",       # rescore every candidate
        limit=top_k, with_payload=True, search_params=NO_RESCORE,
    )
    hits = [Hit(id=p.id, payload=p.payload, fused=p.score, cosine=p.score)
            for p in res.points]
    # Cosine ties are far rarer than RRF ties, but duplicated generic text
    # produces exact ones - break on id so identical queries stay reproducible.
    hits.sort(key=lambda h: (-h.cosine, str(h.id)))
    return hits
