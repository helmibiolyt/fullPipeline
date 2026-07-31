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
import pipeline as P                                        # noqa: E402

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
    return JSONResponse(P.run(req.question, k=req.k))


PAGE = r"""
<!doctype html><meta charset=utf-8>
<title>Biolyt · ask</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{--ink:#070c18;--panel:#111a2b;--panel2:#18243a;--line:#25334d;
 --text:#ecf1f9;--muted:#93a6c4;--dim:#63768f;--blue:#4c8dff;--violet:#a78bfa;
 --green:#34d399;--amber:#fbbf24;--red:#f87171}
*{box-sizing:border-box}
body{margin:0;background:var(--ink);color:var(--text);
 font:15px/1.6 -apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1000px;margin:0 auto;padding:34px 22px 90px}
header{display:flex;align-items:baseline;gap:13px;flex-wrap:wrap;
 border-bottom:1px solid var(--line);padding-bottom:15px;margin-bottom:24px}
h1{font-size:19px;margin:0}
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
.ex{margin:13px 0 0;display:flex;gap:8px;flex-wrap:wrap}
.ex b{font-weight:500;font-size:12.5px;color:var(--dim);
 border:1px solid var(--line);padding:5px 11px;border-radius:99px;cursor:pointer}
.ex b:hover{border-color:var(--blue);color:var(--text)}
#out{margin-top:28px}
.answer{background:var(--panel);border-radius:13px;padding:22px 24px;
 border-left:3px solid var(--green);font-size:15.5px;line-height:1.7}
.answer p{margin:0 0 12px}.answer p:last-child{margin:0}
.srcs{margin-top:16px;padding-top:14px;border-top:1px solid var(--line);
 display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.srcs em{font-style:normal;color:var(--dim);font-size:12px;
 letter-spacing:.5px}
.tag{font-size:11.5px;font-weight:700;padding:4px 10px;border-radius:6px;
 background:rgba(52,211,153,.15);color:var(--green)}
.meta{color:var(--dim);font-size:12px;margin:12px 2px 0}
.ev{margin-top:26px}
.evh{display:flex;align-items:center;gap:10px;cursor:pointer;
 padding:11px 0;border-top:1px solid var(--line);user-select:none}
.evh:hover .t{color:var(--text)}
.evh .t{font-size:13px;font-weight:600;color:var(--muted)}
.evh .n{font-size:12px;color:var(--dim);margin-left:auto}
.car{color:var(--dim);font-size:11px;transition:transform .15s}
.open .car{transform:rotate(90deg)}
.evb{display:none;padding:4px 0 18px}
.open .evb{display:block}
pre.q{background:#0c1424;border-left:3px solid var(--blue);margin:0 0 14px;
 padding:12px 14px;border-radius:8px;overflow-x:auto;font-size:12.5px;
 color:var(--muted);font-family:Consolas,monospace;white-space:pre-wrap;
 word-break:break-word}
pre.q.v{border-left-color:var(--violet)}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;color:var(--blue);font-weight:600;padding:8px 10px;
 border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:8px 10px;border-bottom:1px solid #162033;color:var(--muted);
 vertical-align:top}
td:first-child{color:var(--text)}
tr:hover td{background:var(--panel)}
.chunk{background:var(--panel);border-radius:9px;padding:13px 15px;
 margin-bottom:10px;border-left:3px solid var(--violet)}
.chead{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;
 margin-bottom:6px}
.score{color:var(--violet);font-weight:700;font-size:12.5px}
.src{color:var(--dim);font-size:11.5px}
.head{color:var(--text);font-size:12.5px;font-weight:600}
.file{color:var(--dim);font-size:11px;font-family:Consolas,monospace;
 margin-bottom:7px;word-break:break-all}
.body{font-size:13px;color:var(--muted);line-height:1.6}
.err{background:rgba(248,113,113,.1);border-left:3px solid var(--red);
 padding:13px 16px;border-radius:8px;color:#ffc9c9;font-size:14px}
.none{background:rgba(251,191,36,.09);border-left:3px solid var(--amber);
 padding:11px 14px;border-radius:8px;color:#ffe6a8;font-size:13px}
.spin{color:var(--muted);font-size:14px;display:flex;align-items:center;
 gap:9px}
.dot{width:6px;height:6px;border-radius:50%;background:var(--blue);
 animation:p 1s infinite}
.dot:nth-child(2){animation-delay:.15s}.dot:nth-child(3){animation-delay:.3s}
@keyframes p{0%,100%{opacity:.25}50%{opacity:1}}
footer{margin-top:44px;color:var(--dim);font-size:12px;
 border-top:1px solid var(--line);padding-top:14px}
</style>

<div class=wrap>
<header>
  <h1>Ask</h1>
  <span class=sub>every question queries the knowledge graph <b>and</b> the
    regulatory documents</span>
  <span class=pill id=router>&hellip;</span>
</header>

<form id=f>
  <input type=text id=q autocomplete=off spellcheck=false
         placeholder="e.g. what are the side effects of atorvastatin">
  <button id=go>Ask</button>
</form>
<div class=ex id=ex></div>
<div id=out></div>

<footer>
  Neo4j on Azure &middot; 3.24M document chunks on AWS &middot;
  <span id=model></span>. The model sees only what the two stores return &mdash;
  nothing is answered from its own knowledge.
</footer>
</div>

<script>
const EX = [
 "what are the side effects of atorvastatin",
 "which drugs target EGFR",
 "what are the contraindications of sertraline",
 "how many trials are running in the MENA region",
 "what does the label say about ibuprofen in pregnancy",
 "which pathogenic variants are implicated in cystic fibrosis"
];
const out=document.getElementById('out'), qEl=document.getElementById('q'),
      go=document.getElementById('go');

fetch('/health').then(r=>r.json()).then(h=>{
  document.getElementById('router').textContent=h.model;
  document.getElementById('model').textContent=h.model+' via '+h.provider;
});
const exBox=document.getElementById('ex');
EX.forEach(t=>{const b=document.createElement('b');b.textContent=t;
  b.onclick=()=>{qEl.value=t;submit();};exBox.appendChild(b);});
document.getElementById('f').onsubmit=e=>{e.preventDefault();submit();};

const esc=s=>String(s??'').replace(/[&<>"]/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

async function submit(){
  const question=qEl.value.trim(); if(!question) return;
  go.disabled=true;
  out.innerHTML='<div class=spin><span class=dot></span><span class=dot>'+
    '</span><span class=dot></span> asking the graph and the documents'+
    '&hellip;</div>';
  try{
    const r=await fetch('/ask',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({question,k:6})});
    render(await r.json());
  }catch(err){ out.innerHTML='<div class=err>'+esc(err)+'</div>'; }
  go.disabled=false;
}

function section(title,count,inner,open){
  const id='s'+Math.random().toString(36).slice(2);
  return '<div class="ev'+(open?' open':'')+'" id='+id+'>'+
    '<div class=evh onclick="document.getElementById(\''+id+
      '\').classList.toggle(\'open\')">'+
      '<span class=car>&#9656;</span><span class=t>'+title+'</span>'+
      '<span class=n>'+count+'</span></div>'+
    '<div class=evb>'+inner+'</div></div>';
}

function render(d){
  if(d.error){ out.innerHTML='<div class=err>'+esc(d.error)+'</div>'; return; }

  let h='<div class=answer>'+
    (d.answer||'(the model returned nothing)').split(/\n\s*\n/)
      .map(p=>'<p>'+esc(p).replace(/\n/g,'<br>')+'</p>').join('');
  if(d.sources && d.sources.length)
    h+='<div class=srcs><em>SOURCES</em>'+
       d.sources.map(s=>'<span class=tag>'+esc(s)+'</span>').join('')+'</div>';
  h+='</div>';

  const g=d.graph||{}, dc=d.docs||{};
  h+='<div class=meta>planned in '+d.plan_ms+' ms &middot; graph '+
     (g.ms||0)+' ms &middot; documents '+(dc.ms||0)+
     ' ms &middot; answered in '+d.answer_ms+' ms &middot; '+d.tokens+
     ' tokens</div>';

  // ---- graph evidence
  let gi='<pre class=q>'+esc(d.cypher)+'</pre>';
  if(g.error) gi+='<div class=err>'+esc(g.error)+'</div>';
  else if(!g.total) gi+='<div class=none>The query ran and matched nothing. '+
    'That usually means the starting node was not found, not that the graph '+
    'lacks the data.</div>';
  else{
    gi+='<table><tr>'+g.columns.map(c=>'<th>'+esc(c)+'</th>').join('')+'</tr>';
    g.rows.forEach(r=>{gi+='<tr>'+g.columns.map(c=>'<td>'+esc(r[c])+
      '</td>').join('')+'</tr>';});
    gi+='</table>';
    if(g.total>g.rows.length)
      gi+='<div class=meta>showing '+g.rows.length+' of '+g.total+'</div>';
  }
  h+=section('Knowledge graph', (g.total||0)+' rows', gi, false);

  // ---- document evidence
  let di='<pre class="q v">'+esc(d.document_query)+
    (d.section?'   [section: '+esc(d.section)+']':'')+'</pre>';
  if(dc.error) di+='<div class=err>'+esc(dc.error)+'</div>';
  else if(!dc.total) di+='<div class=none>Nothing scored above the 0.6 '+
    'relevance floor &mdash; the corpus has nothing to say about this.</div>';
  else dc.chunks.forEach(c=>{
    di+='<div class=chunk><div class=chead><span class=score>'+
      c.score.toFixed(3)+'</span><span class=head>'+esc(c.heading)+
      '</span><span class=src>'+esc(c.source)+'</span></div>'+
      '<div class=file>'+esc(c.file)+'</div>'+
      '<div class=body>'+esc(c.text)+'</div></div>';
  });
  h+=section('Regulatory documents', (dc.total||0)+' chunks', di, false);

  out.innerHTML=h;
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
