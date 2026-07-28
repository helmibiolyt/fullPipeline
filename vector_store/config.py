"""Vector-store configuration. All overridable via environment / .env."""
import os
from dotenv import load_dotenv

load_dotenv()

# --- S3 (raw documents live in the same bucket the scraper writes) ---
S3_BUCKET = os.environ.get("S3_BUCKET", "moine-data")
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
# Document suffixes to ingest (CSVs are handled by the graph path, not here).
DOC_SUFFIXES = {".pdf", ".doc", ".docx", ".ppt", ".pptx"}

# --- Qdrant ---
# Two modes:
#   * EMBEDDED (default, NO Docker): set QDRANT_PATH -> vectors in a local folder.
#   * SERVER: set QDRANT_URL (e.g. http://localhost:6333) -> Docker/Cloud/native.
# If QDRANT_URL is set it wins; otherwise the embedded local path is used.
QDRANT_URL = os.environ.get("QDRANT_URL") or None
QDRANT_PATH = os.environ.get("QDRANT_PATH", "./qdrant_data")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY") or None
COLLECTION = os.environ.get("QDRANT_COLLECTION", "biolyt_docs")

# --- Models ---
EMBED_MODEL = os.environ.get("EMBED_MODEL", "BAAI/bge-m3")
RERANK_MODEL = os.environ.get("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
EMBED_DIM = int(os.environ.get("EMBED_DIM", "1024"))     # bge-m3 dense dim
USE_FP16 = os.environ.get("USE_FP16", "1").lower() not in ("0", "false", "no")

# --- Chunking ---
CHUNK_TOKENS = int(os.environ.get("CHUNK_TOKENS", "512"))     # target chunk size
CHUNK_OVERLAP = int(os.environ.get("CHUNK_OVERLAP", "64"))    # token overlap
SEMANTIC_SPLIT = os.environ.get("SEMANTIC_SPLIT", "1").lower() not in ("0", "false", "no")

# How the semantic branch (documents with no heading structure) picks its cuts:
#   "embedding" - embed each sentence with EMBED_MODEL, cut where meaning shifts.
#                 Needs the model, so it only runs where the GPU is.
#   "paragraph" - cut on blank lines. Deterministic, no model, no GPU.
# Falls back to "paragraph" automatically when the model cannot be loaded, so
# chunking still works on a CPU-only box.
SEMANTIC_MODE = os.environ.get("SEMANTIC_MODE", "embedding")
# Cut at distances above this percentile of the document's own distribution.
# Percentile rather than an absolute threshold, because cosine distances differ
# per document and a fixed number would over-cut some and under-cut others.
#
# 88 is measured, not guessed. Swept 75/80/85/88/92/95 over five real documents
# (3 MHRA PARs, 2 EMA, 9-38 pages) with bge-m3 on CPU. Adjacent-sentence cosine
# distance was strikingly stable across all of them - median ~0.42, p90 ~0.55,
# max ~0.70 - which is what makes a percentile behave predictably here.
# Resulting average chunk size: 75 -> ~267 tok, 88 -> ~345, 95 -> ~436. No
# threshold degenerated: none produced chunks that all hit the 512 budget
# (semantic cutting doing nothing) and none collapsed into fragments.
#
# What this does NOT establish is that the cuts land at meaningful topic
# changes - only that the sizes are sane. Retrieval quality is the real test.
SEMANTIC_PERCENTILE = float(os.environ.get("SEMANTIC_PERCENTILE", "88"))

# --- Retrieval ---
# Candidates fetched from Qdrant before dedup. 250, not 50, because ranking by
# cosine sorts identical copies together: for a generic-heavy query the whole
# top-50 window fills with copies of three texts, and dedup then leaves 3
# results where 15 were asked for. Measured distinct results returned:
#
#   TOP_K                 50   100   150   250
#   atorvastatin/liver     3     5     9    15
#   every other query     15    15    15    15
#
# Latency barely moves (238 -> 260 ms median) since one prefetch query serves
# any depth and the 200 ms query embedding dominates.
TOP_K = int(os.environ.get("TOP_K", "250"))
# How many of those candidates are returned. Free to raise: hybrid_search
# already fetches TOP_K candidates with their payloads, so FINAL_K only slices
# a list that is already in memory - measured identical at 5, 15, 30 and 50
# (226-229 ms). Raising TOP_K is the one that costs, since that is the actual
# search and payload fetch.
FINAL_K = int(os.environ.get("FINAL_K", "15"))

# Cross-encoder reranking, OFF by default.
#
# The reranker scores the query jointly with each candidate, so top_k=50 means
# 50 forward passes of a 568M-parameter model rather than the single pass that
# embeds the query. Measured on the 2 vCPU host:
#
#     embed  ~200 ms | search ~20-57 ms | rerank 122,000 ms
#
# and shrinking the candidate set does not rescue it - reranking just 5 still
# took 15.6 s, because the cost is per passage, not overhead. That is 60-500x
# the rest of the pipeline combined, so it is disabled here rather than left
# on as a trap. Keeping the model unloaded also leaves ~2.3 GB free, which is
# what stops the kernel paging out Qdrant's quantized vectors.
#
# Set RERANK=1 when there is a GPU in front of it (~300 ms there). Turning it
# off costs ordering quality, not recall: the same chunks are retrieved, they
# are just returned in fusion order instead of cross-encoder order.
RERANK = os.environ.get("RERANK", "0").lower() not in ("0", "false", "no")

# Minimum dense cosine for a chunk to be returned. Fixed, not per-request.
#
# Without it every query returns FINAL_K results however irrelevant, with full
# provenance attached - "how do I bake sourdough bread" came back with a sodium
# chloride leaflet, page number and S3 key included. For a RAG system over
# regulatory text that is the dangerous failure: not a wrong answer, a
# confidently sourced one.
#
# 0.6 is calibrated, not guessed. Over 18 real clinical questions and 5
# off-domain ones:
#
#     lowest legitimate    0.669   "interaction between warfarin and antibiotics"
#     highest off-domain   0.565   "how to change a car tyre"
#     gap                 +0.104
#
# 0.6 sits in that gap and rejected 0 of 18 good queries and 0 of 5 bad ones.
# Note the floor is ~0.48, not zero - bge-m3 cosine does not bottom out at 0 for
# unrelated text - so the usable band is narrow and this number does not
# transfer to another embedding model or another corpus. Recalibrate if either
# changes.
MIN_SCORE = float(os.environ.get("MIN_SCORE", "0.6"))

# Skip chunks shorter than this, as a bucket index (see backfill_len / the
# len_bucket payload field):
#   0 = <100 chars   1 = 100-149   2 = 150-199   3 = 200-499
#   4 = 500-999      5 = >=1000
#
# 20% of the collection - roughly 634,000 chunks - is under 100 characters,
# because every UK patient leaflet opens with a numbered table of contents and
# the chunker treated each line of it as a section. Those fragments carry no
# information and they win short queries outright: a bare "Lisinopril" matched
# "3. How to take Lisinopril Tablets" (33 chars) ahead of every real paragraph,
# and all 250 candidates came back under 100 characters.
#
# It has to be filtered inside the search rather than after it - the fragments
# occupy every top slot, so a post-filter returns nothing at all. Hence the
# len_bucket payload field, which Qdrant can filter on before ranking.
#
# 2 (>=150 chars) keeps genuinely short but real sections - "4.3
# Contraindications: Hypersensitivity to the active substance..." is ~113
# chars - while dropping the table-of-contents lines, which are 24-59.
MIN_CHUNK_BUCKET = int(os.environ.get("MIN_CHUNK_BUCKET", "2"))

# Collapse results whose embeddings are at least this similar. 0 disables it.
#
# Hashing the text only catches character-identical copies, and the copies are
# not identical - each manufacturer writes its own product name into the
# wording ("stop taking Lisinopril" / "Lisinopril oral solution" / "Lisinopril
# 1 mg/ml oral solution"). Measured on "Signs of an allergic reaction or
# angioedema from Lisinopril": 50 returned results held 24 distinct meanings,
# with the same SPC angioedema paragraph appearing 7 times and the same
# interaction list 6 times.
#
# 0.95 is calibrated against that result set. Real duplicates cluster at
# 0.96-0.99 - the same paragraph from a different licence - well clear of
# genuinely different content, and every merge inspected at this threshold was
# a product variant. Lower is dangerous in a way that is hard to notice: a
# merged chunk disappears silently, so over-merging costs information nobody
# sees missing, while under-merging only costs a visible slot.
DEDUP_COSINE = float(os.environ.get("DEDUP_COSINE", "0.95"))