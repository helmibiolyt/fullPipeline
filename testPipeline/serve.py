#!/usr/bin/env python3
"""A page with a box. Ask a question, get an answer built from both stores.

    python testPipeline/serve.py
    -> http://localhost:8080

Runs on your machine and reaches both production stores over the public
internet: Neo4j on Azure over bolt, and the document search API on AWS over
HTTP. Nothing is installed on either host.

Every question queries BOTH stores and the answer is written only from what
they return, with its sources named. The page shows the answer first and the
evidence underneath - the two queries, the graph rows, the document chunks -
so any sentence can be checked against the row or the document it came from.
That is the thing worth knowing before this reaches a research agent: not
whether the model sounds right, but whether the stores actually held the
answer.
"""
from __future__ import annotations

import pathlib
import sys

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import ask as A                                             # noqa: E402
import agent as AG                                          # noqa: E402

app = FastAPI(title="Biolyt · ask")


class Ask(BaseModel):
    question: str
    k: int = 6


@app.get("/health")
def health():
    return {"ok": True, "model": A.GROQ_MODEL, "provider": A.PROVIDER,
            "graph": A.NEO4J_URI, "documents": A.VECTOR_API}


@app.post("/ask")
def ask(req: Ask):
    """Answer a question.

    The agent decides which stores to consult, in what order, and whether one
    result should shape the next lookup - a fixed graph-then-documents plan
    cannot answer "what do the labels say about drugs that target EGFR",
    because the drug names ARE the search terms.
    """
    return JSONResponse(AG.run(req.question, k=req.k))


PAGE = HERE / "page.html"


@app.get("/", response_class=HTMLResponse)
def page():
    """The page, read from disk on every request.

    A separate .html file rather than a Python string: generating the script
    from Python meant an escape sequence got eaten somewhere between the two
    languages every time it was edited, and a JavaScript syntax error shows
    up as a button that silently does nothing.

    Read per request so editing the file and reloading is enough - no restart.
    No-store because the page and its script are one document, and a cached
    copy of an older script runs against a newer response shape.
    """
    return HTMLResponse(PAGE.read_text(encoding="utf-8"), headers={
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
    })


if __name__ == "__main__":
    # Runs in the FOREGROUND on purpose. Launched detached - nohup, or a
    # background job - it outlives the terminal and ignores Ctrl+C, and the
    # only way to stop it is to find whatever process holds port 8080.
    #
    #     python testPipeline/serve.py     Ctrl+C stops it
    #     testPipeline\serve.bat stop      if one is already detached
    import uvicorn
    print("\n  http://localhost:8080")
    print("  Ctrl+C to stop\n")
    uvicorn.run(app, host="127.0.0.1", port=8080, log_level="warning")
