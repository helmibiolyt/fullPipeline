#!/usr/bin/env python3
"""HTTP API for the vector store — what the researcher agent calls.

  POST /search   {query, molecule_id?, section?, language?, final_k?}  -> ranked chunks
  POST /ingest   {prefix?, limit?}   -> kick off ingestion (background)
  GET  /health

Run locally:  uvicorn api:app --port 8000
In Docker:    docker compose up  (see docker-compose.yaml)
"""
from __future__ import annotations

from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel

import retrieve
import ingest as ingest_mod
import qdrant_store
from config import COLLECTION, FINAL_K

app = FastAPI(title="Biolyt Vector Store", version="1.0")


class SearchReq(BaseModel):
    query: str
    molecule_id: str | None = None
    section: str | None = None
    language: str | None = None
    final_k: int = FINAL_K


class IngestReq(BaseModel):
    prefix: str = ""
    limit: int | None = None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/stats")
def stats():
    try:
        info = qdrant_store.client().get_collection(COLLECTION)
        return {"collection": COLLECTION, "points": info.points_count}
    except Exception as e:  # noqa: BLE001
        return {"collection": COLLECTION, "points": 0, "note": str(e)}


@app.post("/search")
def search(req: SearchReq):
    """Filtered hybrid retrieval + rerank. Returns chunks with provenance."""
    results = retrieve.retrieve(
        req.query, molecule_id=req.molecule_id, section=req.section,
        language=req.language, final_k=req.final_k)
    return {"query": req.query, "results": results}


@app.post("/ingest")
def do_ingest(req: IngestReq, bg: BackgroundTasks):
    """Ingest raw docs from S3 (runs in the background; poll /stats for progress)."""
    bg.add_task(ingest_mod.ingest, req.prefix, req.limit)
    return {"status": "ingest started", "prefix": req.prefix, "limit": req.limit}
