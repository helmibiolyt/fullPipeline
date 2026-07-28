#!/usr/bin/env python3
"""HTTP API for the vector store — what the researcher agent calls.

  POST /search   {query, molecule_id?, section?, language?, final_k?}  -> ranked chunks
  POST /ingest   {prefix?, limit?}   -> kick off ingestion (background)
  GET  /health
  GET  /testvectorstore   browser form for trying queries by hand

Run locally:  uvicorn api:app --port 8000
In Docker:    docker compose up  (see docker-compose.yaml)
"""
from __future__ import annotations

import html
import time

from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

import retrieve
import ingest as ingest_mod
import qdrant_store
from config import COLLECTION, FINAL_K, TOP_K

app = FastAPI(title="Biolyt Vector Store", version="1.0")


class SearchReq(BaseModel):
    """What the researcher agent sends.

    Every filter here has a payload index behind it, so filtering happens
    before the vector search rather than after - "adverse effects of drug X"
    narrows to a handful of chunks instead of scanning 3.2M.
    """
    query: str
    section: str | None = None        # indications, contraindications, posology...
    section_code: str | None = None   # EU SPC number, e.g. "4.8"
    doc_type: str | None = None       # spc | pil | par  (unreliable for EMA docs)
    molecule_id: str | None = None
    language: str | None = None
    final_k: int = FINAL_K            # results returned; ~44-50 distinct exist
    top_k: int = TOP_K                # candidates fetched before dedup
    # No min_score here on purpose. The relevance floor is fixed at 0.6 in
    # config so a caller cannot lower it and get confident-looking answers to
    # questions the corpus has nothing to say about.


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
        section_code=req.section_code, doc_type=req.doc_type,
        language=req.language, top_k=req.top_k, final_k=req.final_k)
    return {"query": req.query, "count": len(results), "results": results}


@app.post("/ingest")
def do_ingest(req: IngestReq, bg: BackgroundTasks):
    """Ingest raw docs from S3 (runs in the background; poll /stats for progress)."""
    bg.add_task(ingest_mod.ingest, req.prefix, req.limit)
    return {"status": "ingest started", "prefix": req.prefix, "limit": req.limit}


SECTIONS = ["", "indications", "posology", "contraindications", "warnings",
            "interactions", "pregnancy", "adverse_effects", "overdose",
            "pharmacodynamics", "pharmacokinetics", "storage", "excipients"]

_PAGE = """<!doctype html><meta charset=utf-8><title>Biolyt vector store</title>
<style>
 :root{{color-scheme:light dark}}
 body{{font:15px/1.5 system-ui,sans-serif;max-width:1000px;margin:2rem auto;padding:0 1rem}}
 form{{display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:1rem}}
 input[type=text]{{flex:1;min-width:320px;padding:.55rem .7rem;font-size:15px}}
 select,input[type=number]{{padding:.55rem}}
 button{{padding:.55rem 1.2rem;font-size:15px;cursor:pointer}}
 .meta{{opacity:.7;font-size:13px;margin-bottom:1rem}}
 .hit{{border:1px solid #8884;border-radius:6px;padding:.7rem .9rem;margin:.6rem 0}}
 .hdr{{font-size:12.5px;opacity:.8;margin-bottom:.4rem;display:flex;gap:.8rem;flex-wrap:wrap}}
 .tag{{background:#8882;border-radius:3px;padding:.05rem .4rem}}
 .src{{font-family:ui-monospace,monospace;font-size:11.5px;opacity:.65;word-break:break-all;margin-top:.4rem}}
 .txt{{white-space:pre-wrap}}
 .none{{padding:1rem;border:1px dashed #8886;border-radius:6px;opacity:.8}}
</style>
<h2>Biolyt vector store</h2>
<form method=get action=/testvectorstore>
  <input type=text name=q placeholder="ask something, e.g. contraindications in hepatic impairment" value="{q}" autofocus>
  <select name=section>{sections}</select>
  <input type=number name=k value="{k}" min=1 max=50 title="results">
  <button>Search</button>
</form>
<div class=meta>{meta}</div>
{body}
"""


@app.get("/testvectorstore", response_class=HTMLResponse)
def test_page(q: str = "", section: str = "", k: int = FINAL_K):
    """A form for trying queries by hand, so retrieval can be judged by eye.

    Numbers say latency is fine; only reading the chunks says whether the right
    ones came back. Kept dependency-free (no JS, no CDN) so it works from any
    browser without the page itself becoming something to maintain.
    """
    opts = "".join(
        f'<option value="{s}"{" selected" if s == section else ""}>{s or "any section"}</option>'
        for s in SECTIONS)
    if not q:
        return _PAGE.format(q="", sections=opts, k=k,
                            meta=f"collection: {COLLECTION}", body="")
    t0 = time.time()
    res = retrieve.retrieve(q, section=section or None, final_k=k)
    ms = (time.time() - t0) * 1000

    if not res:
        body = ('<div class=none>No results. Either nothing in the corpus matches, '
                'or every candidate fell below MIN_SCORE.</div>')
    else:
        parts = []
        for i, r in enumerate(res, 1):
            cos = f'{r["cosine"]:.3f}' if r.get("cosine") is not None else "sparse-only"
            dups = len(r.get("duplicates") or [])
            tags = [f'<span class=tag>#{i}</span>',
                    f'cosine <b>{cos}</b>',
                    f'{r.get("doc_type") or "?"}']
            if r.get("section"):
                tags.append(f'{r["section"]}'
                            + (f' ({r["section_code"]})' if r.get("section_code") else ""))
            if r.get("page"):
                tags.append(f'p{r["page"]}')
            if dups:
                tags.append(f'<span class=tag>+{dups} identical in other products</span>')
            parts.append(
                f'<div class=hit><div class=hdr>{" ".join(tags)}</div>'
                f'<div class=txt>{html.escape(r["text"][:1400])}</div>'
                f'<div class=src>{html.escape(r["s3_key"])}</div></div>')
        body = "".join(parts)
    meta = f"{len(res)} results in {ms:.0f} ms &middot; collection {COLLECTION}"
    return _PAGE.format(q=html.escape(q, quote=True), sections=opts, k=k,
                        meta=meta, body=body)
