#!/usr/bin/env python3
"""Query the vector store: embed -> filtered hybrid search -> rerank -> top chunks.

    python retrieve.py "why is rimegepant contraindicated in hepatic impairment?"
    python retrieve.py "dosage in renal impairment" --molecule <InChIKey> --section posology

Returns chunks with provenance (source, s3_key, page) so every RAG answer is
traceable to its source document.
"""
import argparse
import hashlib
import json
import re

import embed
import qdrant_store
from config import TOP_K, FINAL_K, RERANK, MIN_SCORE


_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _dedup_key(text: str, n: int = 600) -> str:
    """Identity of a chunk's *content*, ignoring formatting noise.

    Generic medicines are why this exists. UK law requires a generic's SPC to
    repeat the originator's wording, so a dozen manufacturers file the same
    paragraph - measured: one atorvastatin contraindications text appeared 48
    times across 83 licensed products in a single top-100. Hashing the raw text
    barely helps, because PDF extraction leaves the copies differing by a
    hyphen or a space ("excipients listed" vs "excipients of this", 519 vs 541
    characters). Stripping to alphanumerics collapses them properly.

    Only affects generics: the same query shape against a biologic (CAR-T,
    pembrolizumab) returns 97 distinct chunks in 100, and nothing is merged.
    """
    return hashlib.md5(_NON_ALNUM.sub("", text.lower())[:n].encode()).hexdigest()


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
        ranked = [(h, h.fused) for h in hits]

    # Drop weak matches so the caller can be told "nothing relevant" instead of
    # being handed five confident-looking citations. Measured cosine: 0.72 for
    # an in-domain query, 0.53 for "how do I bake sourdough bread", 0.49 for
    # gibberish - and all three produced identical fusion scores, which is why
    # the threshold is on cosine. Sparse-only hits have no cosine and are kept.
    if MIN_SCORE > 0:
        # Gate on the best cosine in the whole candidate set, not per hit.
        # Filtering hit-by-hit leaves sparse-only matches (no cosine) in place,
        # so an off-domain query still returns keyword hits and the threshold
        # does nothing. If nothing dense clears the bar, the corpus has no
        # answer and the honest response is an empty list.
        best = max((h.cosine for h, _ in ranked if h.cosine is not None),
                   default=0.0)
        if best < MIN_SCORE:
            return []
        ranked = [(h, sc) for h, sc in ranked
                  if h.cosine is None or h.cosine >= MIN_SCORE]

    # Collapse duplicate content, keeping the best-ranked copy and recording
    # the others as corroborating sources rather than discarding them: "every
    # UK atorvastatin licence says this" is stronger evidence than one citation.
    merged, seen = [], {}
    for h, s in ranked:
        k = _dedup_key(h.payload["text"])
        if k in seen:
            seen[k]["duplicates"].append(h.payload["s3_key"])
            continue
        seen[k] = {"hit": h, "score": s, "duplicates": []}
        merged.append(seen[k])
        if len(merged) == final_k:
            break
    ranked = [(m["hit"], m["score"]) for m in merged]
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
        # Real similarity, unlike `score` which is a fused rank. This is the
        # number to threshold on when deciding whether an answer is supported.
        "cosine": getattr(h, "cosine", None),
        "duplicates": seen[_dedup_key(h.payload["text"])]["duplicates"],
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
