#!/bin/bash
# Start the search API. bge-m3 loads once at import and stays resident, so the
# ~200 ms per query is inference only - a fresh process pays 5-30 s to load.
# RERANK stays 0: the cross-encoder costs 122 s per query on this CPU box.
cd /home/ubuntu/fullPipeline/vector_store
export USE_FP16=0
export QDRANT_URL=http://localhost:6333
export QDRANT_API_KEY=xy8MA09Vzslr9HbM1iMNFfrLRaotBm2qkmz9TJewGz0
export RERANK=0
export MIN_CHUNK_BUCKET=2
exec /home/ubuntu/vsenv/bin/python -m uvicorn api:app --host 0.0.0.0 --port 8000
