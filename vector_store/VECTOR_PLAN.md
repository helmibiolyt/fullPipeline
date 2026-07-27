# Vector store architecture

Companion to `graph/GRAPH_PLAN.md`. The graph is built from the 440 CSVs; this
layer covers the **93,256 regulatory PDFs** (56.5 GB). The two are linked by
shared identifiers, not by shared storage.

Measured on the real corpus 2026-07-27, not assumed.

## 1. What the corpus actually is

| Corpus | Docs | Avg pages | Avg chars | Text-native | Mandated structure |
|---|---|---|---|---|---|
| MHRA SPC | 23,071 | 16.5 | 31,058 | 4/4 | **yes** — EU SPC template |
| MHRA PIL | 36,260 | 3.0 | 22,744 | 3/4 | yes — patient leaflet template |
| MHRA PAR | 11,228 | 13.5 | 21,100 | 4/4 | no — assessment narrative |
| EMA | 22,150 | 2.5 | 4,848 | 4/4 | mixed |
| PMDA | 547 | 62.2 | 178,357 | 4/4 | yes (Japanese) |

Derived totals: **~731,000 pages, ~1.98 billion characters, ~495M tokens.**

Two measurements drive everything below.

**95% carry a real text layer.** So extraction is a CPU job with a GPU OCR
fallback for the remainder - hours, not days. Running every page through a
GPU layout model (marker, docling, nougat) would cost roughly 100x more for a
5% quality gain on 5% of documents.

**SPCs carry their legally mandated headings** (Directive 2001/83/EC Annex I:
4.1 Therapeutic indications, 4.2 Posology, 4.3 Contraindications, 4.8
Undesirable effects, 5.1 Pharmacodynamic properties, ...). Found in 4/4 sampled
SPCs. This is better than any similarity-based chunker could infer, because the
boundaries are legal requirements rather than statistical guesses.

## 2. Extraction

PyMuPDF (`fitz`) on CPU, parallel across processes.

Detect scans by text density: under ~100 characters per page means no usable
text layer. Route only those to OCR. At ~100 pages/sec/process, 731k pages is
about 2 hours single-threaded, ~20 minutes across 8 workers.

Keep per-page offsets. A citation has to point at a page, not just a document -
"the SPC says X" is not verifiable, "page 7 of this SPC says X" is.

## 3. Chunking - structure first, similarity only as fallback

Not a generic semantic chunker. The structure is already in the documents.

**SPC (23,071 + EMA SPCs)** - split on the numbered template sections. Each
becomes one chunk, sub-split on paragraphs if it exceeds the token budget.
Section 4.8 alone answers most adverse-event questions; splitting it by
character count would scatter that answer across chunks that each look
half-relevant.

**PIL (36,260)** - split on the patient-facing headings ("What X is and what it
is used for", "Before you take X", "How to take X", "Possible side effects",
"How to store X").

**PAR / PMDA / everything else** - no mandated template, so fall back to
recursive splitting on detected headings, then paragraphs. Semantic
(embedding-breakpoint) chunking is an option here only, where structure is
genuinely absent.

Target ~512 tokens with ~15% overlap; allow a section to exceed it rather than
cut mid-sentence. Estimated **~1.15M chunks**.

## 4. Embedding model - BAAI/bge-m3

| Requirement | Why bge-m3 |
|---|---|
| Long sections | 8,192-token context - a whole SPC section fits in one vector |
| Multilingual | PMDA is Japanese; MENA sources carry Arabic. A monolingual English model discards both |
| Exact identifiers | Emits dense **and sparse** vectors in one pass. Drug names, PL numbers and ATC codes need lexical matching; pure dense retrieval misses "PLGB 04416-1656" |
| Cost | 568M params - fits comfortably on any RunPod GPU |

Alternatives considered: `Qwen3-Embedding-4B` scores higher on English MTEB but
is ~7x larger and weaker on the Japanese/Arabic tail. `PubMedBERT` embeddings
are biomedical but capped at 512 tokens, which would force splitting the very
sections we want to keep whole.

On an A100, ~1.15M chunks is roughly **25-40 minutes** of embedding. The GPU is
not the bottleneck - extraction is.

**Reranking:** `BAAI/bge-reranker-v2-m3` over the top ~50 hits. Same family,
and cross-encoder reranking is usually the largest single quality gain
available after retrieval works at all.

## 5. Vector store - Qdrant

Already the choice in `graph/GRAPH_PLAN.md`; the measurements support it.

- **Native hybrid** dense + sparse in one query, matching what bge-m3 emits.
- **Payload filtering** that scales - the common query is "adverse effects of
  drug X" which should filter to that product before searching, not search 1.15M
  chunks and hope.
- **Scalar quantization (int8)** cuts vectors from ~4.7 GB to ~1.2 GB with
  negligible recall loss at this scale.

Sizing: 1.15M chunks x 1024 dims x 4 bytes = **~4.7 GB** float32, **~1.2 GB**
quantized. Small. A single node is sufficient; this does not need a cluster.

## 6. Payload - where this meets the graph

Every chunk carries the identifiers the graph is keyed on, so a graph traversal
can constrain a vector search and vice versa:

```
product_name, pl_number / ema_product_number, atc_code, active_substance,
source (mhra|ema|pmda), doc_type (spc|pil|par|epar), section_code (e.g. "4.8"),
section_title, page_from, page_to, s3_key, sha256
```

`s3_key` is the link back to the document index CSVs already in S3
(`mhra_documents.csv`, `ema_documents.csv`), which is how a retrieved chunk
resolves to a graph node.

## 7. What does NOT go in

The 440 CSVs and their ~300M rows. Those are the graph. Embedding tabular rows
produces vectors that retrieve poorly and duplicate what a Cypher query answers
exactly. The only CSVs that matter here are the document indexes, and only as
join keys.

## 8. Open decisions

1. **Where Qdrant lives** - RunPod alongside the GPU, or a separate always-on
   host. Embedding is a burst job; the store needs to stay up.
2. **Re-embedding policy** - when a scraper adds documents, embed only the new
   ones (`s3_key` + `sha256` make that a set difference), or re-embed on a model
   change.
3. **PIL vs SPC weighting** - PILs are patient-facing and simplified; SPCs are
   clinical. For a clinical question the SPC is authoritative. Worth encoding as
   a retrieval preference rather than treating all 93k documents as equal.

## 9. RunPod shape

The GPU is needed for a **burst**, not continuously. Embedding 1.15M chunks is
25-40 minutes of A100 time; extraction is CPU-bound and the store must stay up
after the GPU is released. So the GPU pod and the vector store should not be the
same machine.

| Stage | Where | Why |
|---|---|---|
| PDF -> text | CPU, parallel workers (the existing EC2 is fine) | 95% text-native, ~20 min across 8 processes. A GPU adds nothing |
| OCR fallback | RunPod GPU, same pod as embedding | only ~5% of documents |
| Chunking | CPU | pure string work |
| Embedding + rerank | **RunPod GPU pod** | the only genuinely GPU-bound stage |
| Qdrant | **persistent host, not the GPU pod** | must outlive the burst |

**Pod sizing.** bge-m3 is 568M params; in fp16 with batching it fits in ~8 GB.
An A100 40GB is comfortable but oversized - a **4090 (24 GB)** or **A6000** runs
it at similar throughput for a fraction of the hourly rate, since the workload is
throughput-bound, not memory-bound. Reserve the A100 only if the reranker runs
in the same pass over a large candidate set.

**Storage.** RunPod pods are ephemeral. Two consequences:
- Use a **network volume** for the model cache, or every pod start re-downloads
  ~2.5 GB of weights.
- Never let Qdrant's data live on pod-local disk. Either run it on the EC2 (it
  has 76 GB free and the quantized index is ~1.2 GB), or use Qdrant Cloud.
  Losing the pod must not mean re-embedding.

**Data path.** The pod should read PDFs (or pre-extracted text) directly from S3
and write vectors straight to Qdrant. Do not stage 56 GB onto pod disk.
Pre-extracting text on the EC2 first is better still: it turns 56 GB of PDFs
into roughly 2 GB of text, so the GPU pod pulls 2 GB instead of 56 and the
expensive hardware is never idle waiting on I/O.

**Cost sketch.** At roughly $0.40-0.70/hr for a 4090, one full embedding pass is
well under a dollar of GPU time. The pass is cheap enough that re-embedding on a
model change is not a decision worth agonising over - which argues for picking a
model on quality rather than on lock-in.
