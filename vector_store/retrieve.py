#!/usr/bin/env python3
"""Query the vector store: embed -> filtered hybrid search -> rerank -> top chunks.

    python retrieve.py "why is rimegepant contraindicated in hepatic impairment?"
    python retrieve.py "dosage in renal impairment" --molecule <InChIKey> --section posology

Returns chunks with provenance (source, s3_key, page) so every RAG answer is
traceable to its source document.
"""
import argparse
import json

import embed
import qdrant_store
from config import TOP_K, FINAL_K, RERANK


def retrieve(query: str, molecule_id: str = None, section: str = None,
             language: str = None, doc_type: str = None, section_code: str = None,
             top_k: int = TOP_K, final_k: int = FINAL_K):
    q_emb = embed.embed_query(query)
    flt = {"molecule_id": molecule_id, "section": section, "language": language,
           "doc_type": doc_type, "section_code": section_code}
    flt = {k: v for k, v in flt.items() if v}
    hits = qdrant_store.hybrid_search(q_emb, top_k=top_k, flt=flt or None)
    if not hits:
        return []
    if RERANK:
        # Cross-encoder: reads query and passage together, so it orders far
        # better than fusion does - and costs a forward pass per candidate.
        passages = [h.payload["text"] for h in hits]
        scores = embed.rerank(query, passages)
        ranked = sorted(zip(hits, scores), key=lambda x: x[1], reverse=True)[:final_k]
    else:
        # Fusion order. Note the score is RRF (1/rank), so it says where a
        # chunk placed, not how well it matched - identical values come back
        # for a perfect hit and for nonsense. Do not threshold on it.
        ranked = [(h, h.score) for h in hits[:final_k]]
    return [{
        "score": float(s),
        "text": h.payload["text"],
        "source": h.payload["source"],
        "s3_key": h.payload["s3_key"],
        "page": h.payload.get("page"),
        "page_to": h.payload.get("page_to"),
        "section": h.payload.get("section"),
        # These were in the payload but never surfaced, so a caller could filter
        # on them yet not see them in the result - and a citation could not say
        # which SPC section it came from.
        "section_code": h.payload.get("section_code"),
        "doc_type": h.payload.get("doc_type"),
        "chunk_path": h.payload.get("chunk_path"),
    } for h, s in ranked]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--molecule")
    ap.add_argument("--section")
    ap.add_argument("--language")
    a = ap.parse_args()
    for r in retrieve(a.query, a.molecule, a.section, a.language):
        print(f"\n[{r['score']:.3f}] {r['source']}  p{r['page']}  ({r['section']})")
        print(f"  {r['s3_key']}")
        print("  " + r["text"][:300].replace("\n", " "))
