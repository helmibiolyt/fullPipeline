#!/usr/bin/env python3
"""A page with a box. Type a question, watch the router choose.

    python testPipeline/serve.py
    -> http://localhost:8080

Runs on your machine and reaches both production stores over the public
internet: Neo4j on Azure over bolt, and the document search API on AWS over
HTTP. Nothing is installed on either host.

The point of the page is to make the decision visible. It shows which store
the LLM picked and why, the exact query it wrote, and the raw result - so you
can judge whether the graph and the document store are actually worth what it
took to build them, before any of this reaches a research agent.

The LLM has exactly two capabilities and cannot answer from its own knowledge.
That is enforced at the API layer with tool_choice="required", not by asking
it nicely in a prompt.
"""
from __future__ import annotations

import pathlib
import sys
import time

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import ask as A                                             # noqa: E402

app = FastAPI(title="Biolyt · test pipeline")


class Ask(BaseModel):
    question: str
    limit: int = 12


@app.get("/health")
def health():
    return {"ok": True, "router": A.GROQ_MODEL, "provider": A.PROVIDER}


@app.post("/ask")
def ask(req: Ask):
    """Route, execute, and return everything the page needs to show its work."""
    t0 = time.time()
    out = {"question": req.question, "store": None, "why": "", "query": "",
           "rows": [], "columns": [], "chunks": [], "error": "",
           "route_ms": 0, "exec_ms": 0, "tokens": 0,
           "model": A.GROQ_MODEL, "provider": A.PROVIDER}
    try:
        r = A.route(req.question)
        if "error" in r:
            out["error"] = r["error"] + (
                f" — it said: {r['said'][:200]}" if r.get("said") else "")
            return JSONResponse(out)

        args = r["args"]
        out["route_ms"] = r["ms"]
        out["tokens"] = r["tokens"]
        out["why"] = args.get("why", "")

        if r["tool"] == "query_graph":
            out["store"] = "graph"
            q = (args.get("cypher") or "").strip()
            out["query"] = q
            bad = A.check_cypher(q)
            if bad:
                out["error"] = f"query refused — {bad}"
                return JSONResponse(out)
            rows, ms = A.run_cypher(q)
            out["exec_ms"] = ms
            out["columns"] = list(rows[0].keys()) if rows else []
            out["rows"] = [
                {k: A._fmt(v) for k, v in row.items()}
                for row in rows[:req.limit]]
            out["total"] = len(rows)
        else:
            out["store"] = "documents"
            q = args.get("query", "")
            sec = args.get("section")
            out["query"] = q + (f"   [section: {sec}]" if sec else "")
            res, ms = A.run_search(q, sec, req.limit)
            out["exec_ms"] = ms
            hits = res.get("results", [])
            out["total"] = len(hits)
            out["chunks"] = [{
                "score": h.get("score") or h.get("rerank_score") or 0,
                "source": h.get("source", ""),
                "file": (h.get("s3_key") or "").split("/")[-1],
                "heading": h.get("heading") or h.get("section") or "",
                "text": " ".join((h.get("text") or "").split())[:1400],
            } for h in hits[:req.limit]]
    except Exception as e:                                   # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:300]}"
    out["total_ms"] = int((time.time() - t0) * 1000)
    return JSONResponse(out)


PAGE = """
<!doctype html><meta charset=utf-8>
<title>Biolyt · ask the graph or the documents</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{
  --ink:#070c18; --panel:#111a2b; --panel2:#18243a; --line:#25334d;
  --text:#ecf1f9; --muted:#93a6c4; --dim:#63768f;
  --blue:#4c8dff; --violet:#a78bfa; --green:#34d399; --amber:#fbbf24;
  --red:#f87171;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ink);color:var(--text);
  font:15px/1.6 -apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:34px 22px 80px}
header{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;
  border-bottom:1px solid var(--line);padding-bottom:16px;margin-bottom:26px}
h1{font-size:20px;margin:0;letter-spacing:.2px}
.sub{color:var(--muted);font-size:13px}
.pill{font-size:11px;color:var(--dim);border:1px solid var(--line);
  padding:2px 9px;border-radius:99px}
form{display:flex;gap:10px}
input[type=text]{flex:1;background:var(--panel);border:1px solid var(--line);
  color:var(--text);padding:15px 17px;border-radius:11px;font-size:16px;
  outline:none;font-family:inherit}
input[type=text]:focus{border-color:var(--blue)}
button{background:var(--blue);border:0;color:#04142e;font-weight:700;
  padding:0 26px;border-radius:11px;cursor:pointer;font-size:15px;
  font-family:inherit}
button:disabled{opacity:.45;cursor:default}
.ex{margin:14px 0 0;display:flex;gap:8px;flex-wrap:wrap}
.ex b{font-weight:500;font-size:12.5px;color:var(--dim);
  border:1px solid var(--line);padding:5px 11px;border-radius:99px;
  cursor:pointer}
.ex b:hover{border-color:var(--blue);color:var(--text)}
#out{margin-top:30px}
.route{display:flex;align-items:center;gap:12px;flex-wrap:wrap;
  margin-bottom:6px}
.badge{font-size:12px;font-weight:700;letter-spacing:.7px;padding:5px 12px;
  border-radius:7px}
.g{background:rgba(76,141,255,.16);color:var(--blue)}
.d{background:rgba(167,139,250,.16);color:var(--violet)}
.why{color:var(--muted);font-size:13.5px}
.meta{color:var(--dim);font-size:12px;margin:4px 0 16px}
pre.q{background:#0c1424;border-left:3px solid var(--blue);margin:0 0 20px;
  padding:14px 16px;border-radius:8px;overflow-x:auto;font-size:13px;
  color:var(--muted);font-family:Consolas,monospace;white-space:pre-wrap;
  word-break:break-word}
pre.q.d{border-left-color:var(--violet)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--blue);font-weight:600;padding:9px 11px;
  border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:9px 11px;border-bottom:1px solid #162033;color:var(--muted);
  vertical-align:top}
tr:hover td{background:var(--panel)}
td:first-child{color:var(--text)}
.chunk{background:var(--panel);border-radius:10px;padding:15px 17px;
  margin-bottom:12px;border-left:3px solid var(--violet)}
.chead{display:flex;gap:11px;align-items:baseline;flex-wrap:wrap;
  margin-bottom:7px}
.score{color:var(--violet);font-weight:700;font-size:13px}
.src{color:var(--dim);font-size:12px}
.head{color:var(--text);font-size:13px;font-weight:600}
.file{color:var(--dim);font-size:11.5px;font-family:Consolas,monospace;
  margin-bottom:8px;word-break:break-all}
.body{font-size:13.5px;color:var(--muted);line-height:1.62}
.err{background:rgba(248,113,113,.1);border-left:3px solid var(--red);
  padding:13px 16px;border-radius:8px;color:#ffc9c9;font-size:14px}
.none{background:rgba(251,191,36,.09);border-left:3px solid var(--amber);
  padding:13px 16px;border-radius:8px;color:#ffe6a8;font-size:14px}
.spin{color:var(--muted);font-size:14px}
.dot{display:inline-block;width:6px;height:6px;border-radius:50%;
  background:var(--blue);margin-right:5px;animation:p 1s infinite}
.dot:nth-child(2){animation-delay:.15s}.dot:nth-child(3){animation-delay:.3s}
@keyframes p{0%,100%{opacity:.25}50%{opacity:1}}
footer{margin-top:40px;color:var(--dim);font-size:12px;
  border-top:1px solid var(--line);padding-top:14px}
</style>

<div class=wrap>
<header>
  <h1>Ask the graph, or the documents</h1>
  <span class=sub>the model may do one of two things &mdash; it never answers itself</span>
  <span class=pill id=router>&hellip;</span>
</header>

<form id=f>
  <input type=text id=q autocomplete=off spellcheck=false
         placeholder="e.g. which drugs target EGFR">
  <button id=go>Ask</button>
</form>

<div class=ex id=ex></div>
<div id=out></div>

<footer>
  Neo4j on Azure &middot; document search on AWS &middot; routed by
  <span id=model></span>. Nothing is answered from the model's own knowledge.
</footer>
</div>

<script>
const EX = [
  "which drugs target EGFR",
  "what are the contraindications of sertraline",
  "how many trials are running in the MENA region",
  "what does the label say about ibuprofen in pregnancy",
  "what adverse events are reported for ibuprofen",
  "which pathogenic variants are implicated in cystic fibrosis"
];
const out = document.getElementById('out');
const qEl = document.getElementById('q');
const go  = document.getElementById('go');

fetch('/health').then(r=>r.json()).then(h=>{
  document.getElementById('router').textContent = h.router;
  document.getElementById('model').textContent = h.router + ' via ' + h.provider;
});

const exBox = document.getElementById('ex');
EX.forEach(t=>{
  const b = document.createElement('b');
  b.textContent = t;
  b.onclick = ()=>{ qEl.value = t; submit(); };
  exBox.appendChild(b);
});

document.getElementById('f').onsubmit = e => { e.preventDefault(); submit(); };

const esc = s => String(s??'').replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

async function submit(){
  const question = qEl.value.trim();
  if(!question) return;
  go.disabled = true;
  out.innerHTML = '<div class=spin><span class=dot></span><span class=dot>'+
    '</span><span class=dot></span> routing&hellip;</div>';
  try{
    const r = await fetch('/ask', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({question, limit:12})});
    render(await r.json());
  }catch(err){
    out.innerHTML = '<div class=err>'+esc(err)+'</div>';
  }
  go.disabled = false;
}

function render(d){
  if(d.error && !d.store){
    out.innerHTML = '<div class=err>'+esc(d.error)+'</div>'; return;
  }
  const isG = d.store === 'graph';
  let h = '<div class=route><span class="badge '+(isG?'g':'d')+'">'+
    (isG?'KNOWLEDGE GRAPH':'DOCUMENTS')+'</span><span class=why>'+
    esc(d.why)+'</span></div>';
  h += '<div class=meta>routed in '+d.route_ms+' ms &middot; '+d.tokens+
       ' tokens &middot; query ran in '+d.exec_ms+' ms';
  if(d.total !== undefined) h += ' &middot; '+d.total+' result'+
       (d.total===1?'':'s');
  h += '</div>';
  h += '<pre class="q'+(isG?'':' d')+'">'+esc(d.query)+'</pre>';

  if(d.error){ h += '<div class=err>'+esc(d.error)+'</div>'; }
  else if(isG){
    if(!d.rows.length){
      h += '<div class=none>The query ran and matched nothing. That usually '+
           'means the starting node was not found, not that the graph lacks '+
           'the data.</div>';
    } else {
      h += '<table><tr>'+d.columns.map(c=>'<th>'+esc(c)+'</th>').join('')+
           '</tr>';
      d.rows.forEach(r=>{
        h += '<tr>'+d.columns.map(c=>'<td>'+esc(r[c])+'</td>').join('')+'</tr>';
      });
      h += '</table>';
      if(d.total > d.rows.length)
        h += '<div class=meta>showing '+d.rows.length+' of '+d.total+'</div>';
    }
  } else {
    if(!d.chunks.length){
      h += '<div class=none>Nothing scored above the 0.6 relevance floor. '+
           'The corpus has nothing to say about this &mdash; the floor is '+
           'fixed so a weak match cannot look confident.</div>';
    }
    d.chunks.forEach(c=>{
      h += '<div class=chunk><div class=chead>'+
           '<span class=score>'+c.score.toFixed(3)+'</span>'+
           '<span class=head>'+esc(c.heading)+'</span>'+
           '<span class=src>'+esc(c.source)+'</span></div>'+
           '<div class=file>'+esc(c.file)+'</div>'+
           '<div class=body>'+esc(c.text)+'</div></div>';
    });
  }
  out.innerHTML = h;
}
</script>
"""


@app.get("/", response_class=HTMLResponse)
def page():
    return PAGE


if __name__ == "__main__":
    import uvicorn
    print("\n  http://localhost:8080\n")
    uvicorn.run(app, host="127.0.0.1", port=8080, log_level="warning")
