#!/bin/bash
# Start the search API. bge-m3 loads once at import and stays resident, so the
# ~200 ms per query is inference only - a fresh process pays 5-30 s to load.
# RERANK stays 0: the cross-encoder costs 122 s per query on this CPU box.
cd /home/ubuntu/fullPipeline/vector_store
export USE_FP16=0
# Secrets come from vector_store/.env, which is gitignored. This line used to
# read `export QDRANT_API_KEY=<the actual key>`, which put a live credential
# in a repo that is pushed to GitHub - readable by anyone who can see it,
# and 6333 is reachable. config.py already calls load_dotenv(), so every
# entry point picks the file up: the API, ingest.py run by hand, and the
# Airflow DAG over SSH.
set -a; [ -f .env ] && . ./.env; set +a
export RERANK=0
export MIN_CHUNK_BUCKET=2
exec /home/ubuntu/vsenv/bin/python -m uvicorn api:app --host 0.0.0.0 --port 8000
