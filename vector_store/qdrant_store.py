"""Qdrant collection with hybrid (dense + sparse) vectors and metadata filtering."""
from __future__ import annotations

from functools import lru_cache

from qdrant_client import QdrantClient, models

from config import QDRANT_URL, QDRANT_PATH, QDRANT_API_KEY, COLLECTION, EMBED_DIM
from schema import Chunk


@lru_cache(maxsize=1)
def client() -> QdrantClient:
    # Server mode if a URL is given (Docker/Cloud); else embedded local — no Docker.
    if QDRANT_URL:
        return QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
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
    if c.collection_exists(COLLECTION):
        return
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
    # Payload indexes for the fields retrieval filters on. Filtering happens
    # before search, so "adverse effects of drug X" narrows to a handful of
    # chunks rather than scanning millions.
    for field in ("source", "doc_id", "doc_type", "section", "section_code",
                  "chunk_path", "language", "molecule_id",
                  # s3_key and etag drive incremental sync: the skip check
                  # scrolls them, and prune filters on s3_key.
                  "s3_key", "etag"):
        c.create_payload_index(COLLECTION, field, models.PayloadSchemaType.KEYWORD)


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
    client().upsert(COLLECTION, points=points, wait=wait)


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


def hybrid_search(q_emb: dict, top_k: int, flt: dict | None = None):
    """Fuse dense + sparse results (RRF). `flt` = {field: value} exact filters."""
    qfilter = None
    if flt:
        qfilter = models.Filter(must=[
            models.FieldCondition(key=k, match=models.MatchValue(value=v))
            for k, v in flt.items() if v is not None
        ])
    res = client().query_points(
        collection_name=COLLECTION,
        prefetch=[
            models.Prefetch(query=q_emb["dense"], using="dense", limit=top_k, filter=qfilter),
            models.Prefetch(
                query=models.SparseVector(
                    indices=list(q_emb["sparse"].keys()),
                    values=list(q_emb["sparse"].values())),
                using="sparse", limit=top_k, filter=qfilter),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=top_k, with_payload=True,
    )
    return res.points
