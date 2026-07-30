#!/usr/bin/env python3
"""Embed the graph's lookup entities, so a question can find a node it does not
name exactly.

    python graph/embed_entities.py --dir ~/graph-runs/<build>   # write vectors
    python graph/embed_entities.py --dir ... --query "NSCLC"    # try one

**Why this exists.** Full-text search covers exact and near-exact names, and
after synonyms were stored it handles "lung cancer" fine. It cannot cover
abbreviations or paraphrase, because they share no characters with the stored
name: NSCLC vs "Carcinoma, Non-Small-Cell Lung", statins vs "HMG CoA reductase
inhibitor". Both returned nothing from Neo4j's index.

**Why SapBERT and not bge-m3.** Measured on those exact failures, against eight
distractors:

    probe                              bge-m3         SapBERT
    NSCLC                              0.480 rank 1   0.807 rank 1
    COPD                               0.558 rank 1   0.785 rank 1
    GERD                               0.535 rank 1   0.827 rank 1
    statins                            0.426 rank 3   0.735 rank 1
    drugs that block the PD-1 pathway  0.813 rank 1   0.872 rank 1
    heart attack                       0.609 rank 1   0.778 rank 1

bge-m3 gets five of six, but scores correct answers at 0.43-0.61 - too close to
its distractors to threshold, so you must always take the top hit and hope.
SapBERT sits at 0.74-0.87, which leaves room to reject a weak match. It was
trained by self-alignment on UMLS synonym pairs, so acronym-to-canonical is its
training objective rather than a side effect.

The two models are kept apart on purpose. bge-m3 (1024-d) stays the document
embedder in Qdrant; this is 768-d and only ever compares node text to node
text. Mixing them would be meaningless - different spaces.

**What is embedded, and what is not.** Only labels someone starts a question
from, where the stored name is semantically distant from how it gets asked:

    Disease       24,441   NSCLC, COPD - MeSH stores formal headings
    DrugClass      6,996   "statins"
    AdverseEvent   6,981   "heart problems"
    Mechanism      1,967   "blocks the PD-1 pathway"

40,385 nodes, 0.3% of the graph. Deliberately excluded: 7.9M Identifiers
(opaque codes with no semantics), 2.87M unnamed ChEMBL substances (nothing to
embed), 937k Variants and 1.05M trials (reached by traversal, never searched by
name), Products and Companies (exact names, full-text already works).
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import sys

csv.field_size_limit(2**31 - 1)

MODEL = "cambridgeltl/SapBERT-from-PubMedBERT-fulltext"
DIM = 768

# label -> (filename, text columns in priority order)
EMBED = {
    "Disease":      ("Disease.csv",      ("name", "synonyms")),
    "DrugClass":    ("DrugClass.csv",    ("name",)),
    "AdverseEvent": ("AdverseEvent.csv", ("term",)),
    "Mechanism":    ("Mechanism.csv",    ("name",)),
}

# Below this, a match is a guess. Chosen from the measured spread: correct
# answers scored 0.735-0.872 and the best distractor sat well under 0.6.
MIN_SCORE = 0.60


def _encode(texts, batch=64):
    import numpy as np, torch
    from transformers import AutoTokenizer, AutoModel
    tok = AutoTokenizer.from_pretrained(MODEL)
    mod = AutoModel.from_pretrained(MODEL).eval()
    if torch.cuda.is_available():
        mod = mod.cuda()
    out = []
    for i in range(0, len(texts), batch):
        b = tok(texts[i:i + batch], padding=True, truncation=True,
                max_length=48, return_tensors="pt")
        if torch.cuda.is_available():
            b = {k: v.cuda() for k, v in b.items()}
        with torch.no_grad():
            # [CLS], which is the pooling SapBERT was trained with. Mean
            # pooling silently degrades it.
            v = mod(**b).last_hidden_state[:, 0, :]
        out.append(v.cpu().numpy())
        if i and i % 5120 == 0:
            print(f"    {i:,}/{len(texts):,}", flush=True)
    V = np.vstack(out).astype("float32")
    return V / np.linalg.norm(V, axis=1, keepdims=True)


def build(build_dir: pathlib.Path, out: pathlib.Path):
    import numpy as np
    keys, texts, labels = [], [], []
    for label, (fname, cols) in EMBED.items():
        p = build_dir / "nodes" / fname
        if not p.exists():
            print(f"  skip {label}: no {fname}")
            continue
        n = 0
        for r in csv.DictReader(p.open(encoding="utf-8")):
            # Name plus synonyms as one string: an entry term is another way of
            # saying the same concept, so it belongs in the same vector rather
            # than competing as a separate row.
            parts = [r.get(c, "") for c in cols if r.get(c)]
            text = " ; ".join(parts)[:300].strip()
            if len(text) < 2:
                continue
            keys.append(r["key"]); texts.append(text); labels.append(label); n += 1
        print(f"  {label:<14}{n:>8,}")
    print(f"  {'TOTAL':<14}{len(texts):>8,}\n  encoding with {MODEL} ...")
    V = _encode(texts)
    out.mkdir(parents=True, exist_ok=True)
    np.save(out / "vectors.npy", V)
    (out / "keys.json").write_text(
        json.dumps({"model": MODEL, "dim": DIM, "min_score": MIN_SCORE,
                    "keys": keys, "labels": labels, "texts": texts}),
        encoding="utf-8")
    print(f"\nwrote {V.shape[0]:,} x {V.shape[1]} -> {out}  "
          f"({V.nbytes / 1e6:.0f} MB fp32)")


def query(out: pathlib.Path, q: str, k: int = 5):
    import numpy as np
    V = np.load(out / "vectors.npy")
    meta = json.loads((out / "keys.json").read_text(encoding="utf-8"))
    qv = _encode([q])[0]
    sims = V @ qv
    for i in np.argsort(-sims)[:k]:
        flag = "" if sims[i] >= MIN_SCORE else "   (below threshold)"
        print(f"  {sims[i]:.3f}  {meta['labels'][i]:<14}{meta['texts'][i][:64]}{flag}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--query")
    a = ap.parse_args()
    out = pathlib.Path(a.out or (pathlib.Path(a.dir) / "embeddings"))
    if a.query:
        query(out, a.query)
    else:
        build(pathlib.Path(a.dir), out)


if __name__ == "__main__":
    main()
