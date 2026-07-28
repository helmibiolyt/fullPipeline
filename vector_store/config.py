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
TOP_K = int(os.environ.get("TOP_K", "50"))       # candidates before rerank
FINAL_K = int(os.environ.get("FINAL_K", "5"))    # after rerank

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
