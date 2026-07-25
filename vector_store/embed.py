"""bge-m3 embeddings (dense + sparse) and bge-reranker-v2-m3.

Models are loaded lazily and cached, so importing this module is cheap and the
(heavy) model only loads on first use.
"""
from __future__ import annotations

from functools import lru_cache

from config import EMBED_MODEL, RERANK_MODEL, USE_FP16


@lru_cache(maxsize=1)
def _embedder():
    from FlagEmbedding import BGEM3FlagModel
    return BGEM3FlagModel(EMBED_MODEL, use_fp16=USE_FP16)


@lru_cache(maxsize=1)
def _reranker():
    from FlagEmbedding import FlagReranker
    return FlagReranker(RERANK_MODEL, use_fp16=USE_FP16)


def embed_passages(texts: list[str]) -> list[dict]:
    """Return [{'dense': [...], 'sparse': {idx: weight}}] for each text."""
    out = _embedder().encode(
        texts, return_dense=True, return_sparse=True, return_colbert_vecs=False)
    dense = out["dense_vecs"]
    sparse = out["lexical_weights"]     # list of {token_id(str): weight}
    return [
        {"dense": dense[i].tolist(),
         "sparse": {int(k): float(v) for k, v in sparse[i].items()}}
        for i in range(len(texts))
    ]


def embed_query(text: str) -> dict:
    return embed_passages([text])[0]


def rerank(query: str, passages: list[str]) -> list[float]:
    """Relevance scores for (query, passage) pairs — higher is better."""
    if not passages:
        return []
    scores = _reranker().compute_score([[query, p] for p in passages], normalize=True)
    return scores if isinstance(scores, list) else [scores]
