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
from config import TOP_K, FINAL_K


def retrieve(query: str, molecule_id: str = None, section: str = None,
             language: str = None, top_k: int = TOP_K, final_k: int = FINAL_K):
    q_emb = embed.embed_query(query)
    flt = {"molecule_id": molecule_id, "section": section, "language": language}
    flt = {k: v for k, v in flt.items() if v}
    hits = qdrant_store.hybrid_search(q_emb, top_k=top_k, flt=flt or None)
    if not hits:
        return []
    # rerank the candidates for precision
    passages = [h.payload["text"] for h in hits]
    scores = embed.rerank(query, passages)
    ranked = sorted(zip(hits, scores), key=lambda x: x[1], reverse=True)[:final_k]
    return [{
        "score": float(s),
        "text": h.payload["text"],
        "source": h.payload["source"],
        "s3_key": h.payload["s3_key"],
        "page": h.payload.get("page"),
        "section": h.payload.get("section"),
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
