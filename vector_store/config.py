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

# --- Retrieval ---
TOP_K = int(os.environ.get("TOP_K", "50"))       # candidates before rerank
FINAL_K = int(os.environ.get("FINAL_K", "5"))    # after rerank
