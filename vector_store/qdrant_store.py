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
    c = client()
    if c.collection_exists(COLLECTION):
        return
    c.create_collection(
        collection_name=COLLECTION,
        vectors_config={"dense": models.VectorParams(
            size=EMBED_DIM, distance=models.Distance.COSINE)},
        sparse_vectors_config={"sparse": models.SparseVectorParams(
            index=models.SparseIndexParams())},
    )
    # payload indexes for fast filtering
    for field in ("source", "doc_id", "section", "language", "molecule_id"):
        c.create_payload_index(COLLECTION, field, models.PayloadSchemaType.KEYWORD)


def upsert(chunks: list[Chunk], embeddings: list[dict]):
    """Idempotent: point id = chunk_id, so re-ingest overwrites, never duplicates."""
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
    client().upsert(COLLECTION, points=points, wait=True)


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
