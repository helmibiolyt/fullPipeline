# Vector store — final architecture

Companion to `graph/GRAPH_PLAN.md`. The graph is built from the 440 CSVs; this
layer covers the **93,256 regulatory PDFs** (56.5 GB). They are linked by shared
identifiers, not shared storage.

Numbers below are measured against the real corpus on 2026-07-27, not assumed.
The model, store and chunking approach were already chosen in the 2026-07-25
implementation in this directory; this document records the decisions, the
evidence for them, and the four changes still needed.

---

## 1. The corpus

| Corpus | Docs | Share | Avg pages | Avg chars | Text-native | Template |
|---|---|---|---|---|---|---|
| MHRA PIL | 36,260 | 39% | 3.0 | 22,744 | 3/4 | yes — patient leaflet |
| MHRA SPC | 23,071 | 25% | 16.5 | 31,058 | 4/4 | **yes — EU SPC** |
| EMA | 22,150 | 24% | 2.5 | 4,848 | 4/4 | **mixed** (1/4 sampled) |
| MHRA PAR | 11,228 | 12% | 13.5 | 21,100 | 4/4 | no |
| PMDA | 547 | 0.6% | 62.2 | 178,357 | 4/4 | no (English, see below) |

**~731,000 pages · ~1.98B chars · ~495M tokens.** Chunk count measured at
42.2 per document in the 500-document trial, so **~3.9M chunks** — not the
~1.15M first estimated from average document sizes.

Two measurements drive the design:

- **95% carry a real text layer** → extraction is CPU work with a GPU OCR
  fallback. Pushing every page through a GPU layout model would cost ~100x more
  for a 5% gain on 5% of documents.
- **SPCs carry their legally mandated headings** (Directive 2001/83/EC Annex I)
  → chunk boundaries are already drawn, by law, and do not need inferring.

---

## 2. Decisions

### 2.1 Scope — documents only
The 440 CSVs / ~300M rows stay with the graph. Embedding tabular rows retrieves
poorly and duplicates what a Cypher query answers exactly. The only CSVs used
here are the document indexes (`mhra_documents.csv`, `ema_documents.csv`), as
join keys.

### 2.2 Extraction — PyMuPDF on CPU, OCR only for scans
Parallel across processes on the EC2. Text density under ~100 chars/page means
no usable text layer; route only those to GPU OCR. ~731k pages is about 20
minutes across 8 workers.

Write per-page text back to S3 as JSONL. This turns a **56 GB pull into ~2 GB**
for the GPU stage, so expensive hardware never waits on I/O. Keep page numbers:
a citation must resolve to a page, not just a document.

### 2.3 Chunking — a cascade, decided per document
Not per source. EMA is mixed — only 1 of 4 sampled documents carried a template
— so a per-source rule mis-handles it either way.

```
1. whitelisted EU section numbers present?  -> structure chunks, labelled 4.1/4.8/...
2. else generic headings present?           -> heading chunks
3. else                                     -> semantic split (embedding breakpoints)
4. else                                     -> fixed size + overlap
```

Expected split: ~64% structured (MHRA SPC + PIL), ~13% semantic (PAR, PMDA),
~24% routed per document (EMA).

**Why structure beats semantic where a template exists.** A semantic chunker
embeds sentences and infers where topics change. In an SPC those boundaries are
already written down. Three consequences:

1. **A structure chunk carries a label.** `section_code = "4.8"` lets a query
   about side effects filter to ~20 chunks instead of searching 3.9M. Semantic
   chunks are anonymous.
2. **4.3 Contraindications and 4.4 Warnings read almost identically** — both are
   risk language. Similarity-based splitting merges them. They are legally
   distinct: "never give this" versus "be careful".
3. **4.8 is ~10,000 chars of similar terms.** Similarity stays high throughout,
   so a semantic splitter emits one oversized chunk or cuts mid-list. Structure
   says "one section", then sub-splits evenly.

Semantic chunking also costs more — every sentence embedded first, then the
chunks — for a worse boundary.

**Detection must be a whitelist.** A generic "number + capitalised text" pattern
fires on table contents; on a real SPC it produced `597 Placebo`,
`0.0033 Hazard ratio**` and `13435 Berlin,` as headings. Match only the ~25
valid EU section numbers (1–10, 4.1–4.9, 5.1–5.3, 6.1–6.6).

Target ~512 tokens, 64 overlap; let a section overrun rather than cut
mid-sentence.

### 2.4 Embedding — BAAI/bge-m3, fp16, dense + sparse
- **8,192-token context** — a whole SPC section fits in one vector.
- **Multilingual** — MENA carries Arabic. (PMDA was assumed Japanese during
  design; measured at 0.0% CJK — see §10.)
- **Sparse vectors alongside dense** — regulatory queries hit exact identifiers
  (`PLGB 04416-1656`, ATC `B05CB01`). Pure dense retrieval misses those.
- 568M params — runs on modest hardware.

Rejected: `Qwen3-Embedding-4B` (higher English scores, ~7x larger, weaker on the
non-English tail); `PubMedBERT` (biomedical but 512-token cap, which would
split the very sections we work to keep whole).

### 2.5 Reranking — BAAI/bge-reranker-v2-m3
Top 50 candidates → final 5. Same family as the embedder, and cross-encoder
reranking is the largest single quality gain available once retrieval works.

### 2.6 Store — Qdrant, hybrid, int8 quantized
Native dense+sparse in one query, matching what bge-m3 emits. Payload filtering
applied **before** search. Quantization takes the index from ~16 GB to
**~4 GB** — still a single node, but tighter against the EC2's 5 GB of free
RAM than first estimated; mmap from disk, and move to Qdrant Cloud if it
strains.

### 2.7 Payload — the join to the graph
```
product_name, pl_number / ema_product_number, atc_code, active_substance,
source (mhra|ema|pmda), doc_type (spc|pil|par|epar), section_code, section_title,
page_from, page_to, s3_key, sha256, chunk_path (structure|heading|semantic|fixed)
```
`s3_key` resolves a retrieved chunk back to the document index CSVs, and from
there to a graph node. `chunk_path` exists for instrumentation — see §4.

### 2.8 Incremental re-embedding
`(s3_key, sha256)` set difference against what is already in the collection.
New documents embed; unchanged ones are skipped. A model change re-embeds
everything, which costs under a dollar of GPU time.

---

## 3. Where each stage runs

| Stage | Host | Why |
|---|---|---|
| PDF → text | EC2 (CPU, 8 workers) | 95% text-native; a GPU adds nothing |
| OCR fallback | RunPod GPU | only ~5% of documents |
| Chunking | EC2 (CPU) | string work |
| **Embedding + rerank** | **RunPod GPU** | the only GPU-bound stage |
| **Qdrant** | **EC2 or Qdrant Cloud** | must outlive an ephemeral pod |

**RunPod is a burst resource** — roughly 75–90 min for ~3.9M chunks. Therefore:
- A **4090 (24 GB)** is sufficient; the job is throughput-bound, not
  memory-bound. bge-m3 in fp16 needs ~8 GB.
- Put the model cache on a **network volume**, or every pod start re-downloads
  ~2.5 GB of weights.
- **Never put Qdrant's data on pod-local disk.** Losing a pod must not mean
  re-embedding.

---

## 4. Defects found and fixed in the existing code

All of the below are now fixed and verified; kept as the record of what was
wrong. Three more surfaced only by running the chunker against real PDFs — see
the git history for chunk.py.

1. **`_ntok()` counts whitespace-separated words, not tokens** (`chunk.py`).
   Whitespace counting breaks on Arabic and would break on any CJK ingested
   later. Use bge-m3's own tokenizer.
2. **Chunking resets per page.** The buffer is rebuilt for each page, so a
   section spanning pages is cut regardless of the section logic — and SPCs
   average 16.5 pages. Buffer across the document; record a page range.
3. **Section detection matches keywords, not the numbered template.** Match the
   whitelisted section numbers; keep keywords as the fallback for documents with
   no numbering.
4. **`SEMANTIC_SPLIT` in `config.py` is read by nothing.** Either wire it to
   cascade step 3 or delete it — as it stands it implies behaviour that does not
   exist.

**Instrument the cascade.** Record `chunk_path` per document and report the
distribution after the first run. If 90% of SPCs land on the semantic fallback,
template detection is broken — and nothing in the output would reveal that
otherwise. This is the same failure mode that hid purplebook, sfda and openalex
for months: a run that completes and looks healthy while producing the wrong
thing.


---

## 10. Corrections from the 500-document trial (2026-07-27)

**PMDA is not Japanese.** Asserted repeatedly earlier in the design on the
assumption that Japan's regulator publishes in Japanese. Measured: six PMDA
documents totalling ~1.4M characters contain **0.0% CJK** - they are English
review reports. A 500-document trial sampling 60 PMDA files produced 7,529
chunks and zero Japanese.

This weakens but does not void the multilingual argument for bge-m3: Arabic is
genuinely present (224 chunks from MENA sources), and the tokenizer fix is
still required, since whitespace counting breaks on Arabic exactly as it would
on CJK.

**Trial results, 500 documents, 0 failures, 21,104 chunks:**

| source | chunks | cascade |
|---|---|---|
| mhra-spc | 6,729 | spc 100% |
| mhra-pil | 2,744 | pil 100% |
| pmda | 7,529 | heading 52%, semantic 46% |
| ema | 2,888 | spc 44%, heading 33%, semantic 23% |
| mhra-par | 767 | semantic 98% |
| mena | 333 | semantic 95% |

Two open items:

* **20.7% of chunks are under 60 tokens.** Largely short SPC sections - name,
  pharmaceutical form, shelf life - which are legitimately one line. Whether
  those are precision or noise depends on retrieval: a query filtered to
  `section=shelf_life` wants exactly that chunk. Revisit with retrieval
  results, not before.
* **2 of 500 documents yielded no text** (0.4%), consistent with the ~5% scan
  rate estimated earlier. These are the OCR candidates.
