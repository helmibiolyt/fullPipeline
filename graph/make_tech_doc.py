#!/usr/bin/env python3
"""Render the graph's technical documentation to HTML, and to PDF if a browser
is available.

    python graph/make_tech_doc.py

The source-by-source tables are generated from graph/sources.py and
graph/emit.py rather than typed out, so the document cannot drift from the code
that builds the graph. That is the point: a hand-written inventory of 77 files
and 32 relationship types is wrong within a week, and a wrong inventory is
worse than none because people trust it.
"""
from __future__ import annotations

import html
import pathlib
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import sources                                   # noqa: E402
from emit import NODE_COLUMNS, EDGE_COLUMNS      # noqa: E402

HERE = pathlib.Path(__file__).parent
OUT_HTML = HERE / "GRAPH_TECHNICAL.html"
OUT_PDF = HERE / "GRAPH_TECHNICAL.pdf"

# Execution order, mirroring build.py's run(). Order is not cosmetic: each
# stage fills dictionaries the next one reads.
ORDER = [
    ("1. Vocabularies", "load_atc",
     "WHO ATC tree. Loaded whole even for a slice - 1,318 rows, and every "
     "substance may point into it."),
    ("2. Substance spine", "load_gsrs",
     "The naming authority. Builds the resolver every later loader matches "
     "names through, so nothing needing a name can run before it."),
    ("3. Resolver finalised", "r.finalise()",
     "The stereo tier is built only once every name is known, so a key two "
     "different substances both reduce to can be blocked rather than merged."),
    ("4. ChEMBL molecules", "load_chembl_molecules",
     "2.9M molecules, plus the molregno -> node-key map everything ChEMBL "
     "needs afterwards."),
    ("5. ChEMBL synonyms", "load_chembl_synonyms",
     "Brand names and research codes, as an alias tier consulted after the "
     "identifier tiers."),
    ("6. Structures", "load_structures",
     "InChIKey - chemistry rather than a name, so the strongest merge signal."),
    ("7. Reference tables", "reference.ALL",
     "ATC level 5, salt/parent hierarchy, RXCUI, SPL setids."),
    ("8. Targets", "load_targets",
     "Keyed by UniProt accession where one exists; the tid -> key map is held "
     "for mechanisms."),
    ("9. Mechanisms", "load_mechanisms",
     "One file gives both TARGETS and HAS_MECHANISM as stated fact."),
    ("10. Disease", "disease.ALL",
     "MeSH is the spine; chembl_indications carries the MeSH/EFO crosswalk "
     "later loaders fold MONDO ids onto. Must precede anything matching "
     "disease prose."),
    ("11. Products", "products.ALL",
     "Ten agencies. Also creates all 189 countries and the 9 regions."),
    ("12. Trials", "trials.ALL",
     "Nine registries. WHO last, so native records win on properties."),
    ("13. Safety", "safety.ALL",
     "Regulatory events, FAERS aggregation, VigiAccess organ classes."),
    ("14. Variants", "variants.ALL",
     "Needs symbol_target from HGNC and efo_mesh from chembl_indications."),
    ("15. Literature", "literature.ALL",
     "Needs a finalised resolver and mesh_by_name to match titles against."),
]

CSS = """
body{font:14px/1.65 -apple-system,Segoe UI,Roboto,sans-serif;color:#1b2631;
 max-width:1080px;margin:0 auto;padding:44px 30px}
h1{font-size:30px;border-bottom:3px solid #1f5fbf;padding-bottom:10px;margin-bottom:4px}
h2{font-size:21px;margin-top:38px;border-bottom:1px solid #d5d8dc;padding-bottom:5px;color:#1f5fbf}
h3{font-size:16px;margin-top:24px;color:#34495e}
table{border-collapse:collapse;width:100%;margin:14px 0;font-size:12.5px}
th{background:#1f5fbf;color:#fff;text-align:left;padding:7px 9px;font-weight:600}
td{border-bottom:1px solid #e5e8ec;padding:6px 9px;vertical-align:top}
tr:nth-child(even) td{background:#fafbfc}
code{background:#f4f6f8;padding:1px 5px;border-radius:3px;font-size:12px;
 font-family:SFMono-Regular,Consolas,monospace}
pre{background:#f4f6f8;border-left:3px solid #1f5fbf;padding:12px 14px;
 overflow-x:auto;font-size:12px;line-height:1.5}
.note{background:#fff8e6;border-left:3px solid #e0a800;padding:10px 14px;margin:14px 0}
.warn{background:#fdeceb;border-left:3px solid #c0392b;padding:10px 14px;margin:14px 0}
.sub{color:#5d6d7e;font-size:13px;margin-top:0}
@media print{body{max-width:none;padding:14px}h2{page-break-after:avoid}
 table{page-break-inside:avoid}}
"""


def esc(s):
    return html.escape(str(s or ""))


def table(rows, headers, widths=None):
    w = widths or []
    out = ["<table><tr>"]
    for i, h in enumerate(headers):
        style = f' style="width:{w[i]}"' if i < len(w) else ""
        out.append(f"<th{style}>{h}</th>")
    out.append("</tr>")
    for r in rows:
        out.append("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>")
    out.append("</table>")
    return "".join(out)


def main():
    p = [f"<style>{CSS}</style>",
         "<h1>Biomedical Knowledge Graph &mdash; Technical Documentation</h1>",
         f"<p class=sub>{len(NODE_COLUMNS)} node labels &middot; "
         f"{len(EDGE_COLUMNS)} relationship types &middot; "
         f"{len(sources.INCLUDED)} source files &middot; "
         f"13.7M nodes / 16.8M relationships</p>"]

    p.append("<h2>1. Architecture</h2>")
    p.append("<p>The graph is built <b>to files, not to a database</b>. Every "
             "loader writes CSV; a validator reads those CSVs; only a build "
             "that passes validation is imported. That ordering is the design's "
             "core: a bulk import replaces the store outright with no "
             "transaction, so an unchecked build silently becomes the live "
             "graph and the failure mode is a confident wrong answer rather "
             "than an error.</p>")
    p.append("<pre>S3 (moine-data)\n"
             "   |  77 declared CSVs\n"
             "   v\n"
             "build.py            stream each file, resolve names, emit nodes/edges\n"
             "   v\n"
             "~/graph-runs/&lt;ts&gt;/  nodes/*.csv  edges/*.csv  manifest.json\n"
             "   v\n"
             "validate.py         referential integrity, key collisions, fixtures\n"
             "   v  (exit 0 required)\n"
             "stage_for_neo4j.py  strip headers, collapse newlines, enforce types\n"
             "   v\n"
             "neo4j-admin import  -&gt; Neo4j 'biolyt'\n"
             "   v\n"
             "schema.cypher       constraints, indexes, 3 full-text indexes</pre>")
    p.append("<p>Nothing in the build talks to Neo4j. The same CSVs a human "
             "inspects are the ones imported, so every claim about the graph "
             "is a claim about a table that can be checked without "
             "infrastructure.</p>")

    p.append("<h2>2. Identity and keys</h2>")
    p.append("<p>Every node key is <code>NAMESPACE:VALUE</code> and globally "
             "unique. <code>neo4j-admin</code> resolves all endpoints in one "
             "id space, so two labels sharing a key become one node. That "
             "happened: <code>MESH:D000544</code> was both a Disease and an "
             "Identifier until every identifier was routed through a single "
             "<code>Writer.identifier()</code> that prefixes <code>ID:</code>.</p>")
    p.append(table([
        ("Substance", "<code>UNII:36TN91XZ0V</code>",
         "gsrs UNII; falls back to CHEMBL: then NAME:"),
        ("Product", "<code>CA:02245678</code>", "the agency's own licence number"),
        ("ClinicalTrial", "<code>NCT:NCT01045135</code>",
         "registry id, canonicalised by trial_key()"),
        ("Disease", "<code>MESH:D002289</code>",
         "MeSH descriptor; ICD11: where no MeSH match"),
        ("Target", "<code>UNIPROT:P04035</code>", "UniProt accession"),
        ("Variant", "<code>CLINVAR:12345</code>",
         "ClinVar VariationID, or COSMIC MutationID"),
        ("Identifier", "<code>ID:UNII:36TN91XZ0V</code>",
         "always ID:-prefixed, so it cannot collide with the entity it identifies"),
    ], ["Label", "Example key", "Source of the value"], ["16%", "26%"]))
    p.append("<div class=note><b>Why trial keys repeat themselves.</b> "
             "933,232 of 1,048,841 look like <code>NCT:NCT01045135</code>, "
             "because 19 of 22 registries already embed their prefix in the "
             "id. One rule applied uniformly beats a conditional one that "
             "breaks when registry 23 arrives, and EUCTR/CTIS need the "
             "namespace regardless: <code>2004-000010-11</code> identifies "
             "nothing on its own.</div>")

    p.append("<h2>3. Name resolution</h2>")
    p.append("<p>Product ingredient lists, trial interventions and event "
             "subjects are free text. A tiered resolver maps them to substance "
             "keys, and <b>every edge records which tier matched</b> in "
             "<code>match_method</code>, so a stated fact and an inferred one "
             "are never confused downstream.</p>")
    p.append(table([
        ("<code>unii</code>", "exact match on the folded name", "highest"),
        ("<code>salt</code>",
         "salt/hydrate tokens stripped &mdash; <i>atorvastatin calcium</i> "
         "&rarr; atorvastatin", ""),
        ("<code>stereo</code>",
         "stereo prefix stripped; built only after every name is known", ""),
        ("<code>synonym</code>", "ChEMBL alias tier", ""),
        ("<code>provisional</code>",
         "no match &mdash; a <code>NAME:</code> key, visibly unresolved", "lowest"),
    ], ["Tier", "Rule", "Precedence"], ["14%", "62%"]))
    p.append("<div class=warn><b>A false merge is silent and permanent; a "
             "missed merge is two visible nodes.</b> The stereo tier is built "
             "only after all names load, and any key two different substances "
             "both reduce to is blocked &mdash; otherwise "
             "<i>Levo-cetirizine</i> merges into cetirizine, which are "
             "different drugs. 35,692 keys are blocked this way."
             "<br><br>Free prose never creates nodes. A product ingredient "
             "list may mint a provisional substance because it is a short "
             "controlled field; 1.6M trial interventions may not.</div>")

    p.append("<h2>4. Node labels</h2>")
    p.append(table(
        [(f"<code>{esc(l)}</code>",
          ", ".join(f"<code>{esc(c)}</code>" for c in cols))
         for l, cols in sorted(NODE_COLUMNS.items())],
        ["Label", "Properties (plus source, run_id on every row)"], ["16%"]))

    p.append("<h2>5. Relationship types</h2>")
    p.append(table(
        [(f"<code>{esc(e)}</code>",
          ", ".join(f"<code>{esc(c)}</code>" for c in cols
                    if c not in ("src", "dst")) or "&mdash;")
         for e, cols in sorted(EDGE_COLUMNS.items())],
        ["Type", "Properties beyond the endpoints"], ["22%"]))
    p.append("<p>Five connect two nodes of the <i>same</i> label: "
             "<code>IS_SALT_OF</code> (122,125), <code>SAME_STUDY_AS</code> "
             "(32,545), <code>SUBTYPE_OF</code> (8,086), "
             "<code>IN_CLASS</code>'s ATC tree (6,982) and "
             "<code>BIOSIMILAR_OF</code> (90). None is a self-loop &mdash; "
             "there are zero edges where src equals dst.</p>")

    p.append("<h2>6. Execution order</h2>")
    p.append("<p>Order is not cosmetic. Each stage fills dictionaries the next "
             "reads; running them out of order produces a graph that validates "
             "but resolves against half-built lookups.</p>")
    p.append(table([(esc(s), f"<code>{esc(f)}</code>", esc(w))
                    for s, f, w in ORDER],
                   ["Stage", "Entry point", "Why here"], ["17%", "18%"]))

    p.append("<h2>7. Every source file, and what it builds</h2>")
    p.append(f"<p>Generated from <code>graph/sources.py</code>. "
             f"{len(sources.INCLUDED)} files. A trailing <code>/</code> means a "
             f"prefix whose contents are discovered from S3 at build time, so "
             f"a new file is picked up without a code change.</p>")
    by_topic = {}
    for d in sources.INCLUDED:
        by_topic.setdefault(d["file"].split("/")[0], []).append(d)
    for topic in sorted(by_topic):
        p.append(f"<h3>{esc(topic.replace('_', ' '))}</h3>")
        p.append(table(
            [("<code>" + esc("/".join(d["file"].split("/")[1:])) + "</code>",
              f"{d['rows']:,}" if d.get("rows") else "&mdash;",
              ", ".join(f"<code>{esc(b)}</code>"
                        for b in d.get("builds", [])) or "&mdash;",
              esc(d.get("note", ""))[:190])
             for d in sorted(by_topic[topic], key=lambda x: x["file"])],
            ["File", "Rows", "Builds", "Note"], ["30%", "7%", "24%"]))

    p.append("<h2>8. What is deliberately not loaded</h2>")
    p.append(table(
        [(f"<code>{esc(k.split(',')[0])}</code>", esc(v)[:340])
         for k, v in sorted(sources.EXCLUDED.items())],
        ["Source", "Reason"], ["27%"]))

    p.append("<h2>9. How the graph meets the vector store</h2>")
    p.append("<p>Two stores on two hosts, joined by identifiers rather than by "
             "shared storage.</p>")
    p.append("<pre>Qdrant chunk payload              Graph\n"
             "  source   ema.europa.eu     &lt;-&gt;  Product     agency=EMA\n"
             "  s3_key   ...PL34771...pdf  &lt;-&gt;  Identifier  scheme=MHRA_PL\n"
             "  doc_type spc | pil | par\n"
             "  molecule_id  (reserved)    &lt;-&gt;  Substance key</pre>")
    p.append("<p>MHRA document filenames embed the licence number "
             "(<code>ABACAVIRLAMIVUDINE_PL347710263_par.pdf</code>) and the "
             "graph holds 39,002 <code>MHRA_PL</code> identifiers &mdash; that "
             "is the join covering the largest slice of the corpus. The chunk "
             "payload reserves <code>molecule_id</code> for a direct link, "
             "currently unpopulated.</p>")
    p.append("<div class=note><b>SPL setids do not join to the vector "
             "store.</b> DailyMed publishes only CSVs to S3, no documents, so "
             "its 393,581 setids identify labels that were never indexed. They "
             "remain the canonical handle for a US label and dereference to a "
             "DailyMed URL.</div>")
    p.append("<p>Entity lookup uses a second, smaller embedding: 40,432 "
             "Disease, DrugClass, AdverseEvent and Mechanism nodes under "
             "<b>SapBERT</b> (768-d), kept apart from the documents' bge-m3 "
             "(1024-d). Full-text handles exact and near-exact names; it "
             "cannot bridge <i>NSCLC</i> to <i>Carcinoma, Non-Small-Cell "
             "Lung</i>, which share no characters.</p>")

    OUT_HTML.write_text("".join(p), encoding="utf-8")
    print(f"wrote {OUT_HTML}  ({OUT_HTML.stat().st_size / 1024:.0f} KB)")

    for exe in ("chrome", "google-chrome", "chromium", "msedge",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Google\Chrome\Application\chrome.exe"):
        path = shutil.which(exe) or (exe if pathlib.Path(exe).exists() else None)
        if not path:
            continue
        try:
            subprocess.run([path, "--headless", "--disable-gpu",
                            f"--print-to-pdf={OUT_PDF}", "--no-pdf-header-footer",
                            OUT_HTML.as_uri()], check=True, timeout=180,
                           capture_output=True)
            if OUT_PDF.exists():
                print(f"wrote {OUT_PDF}  ({OUT_PDF.stat().st_size / 1024:.0f} KB)")
                return
        except Exception as e:
            print(f"  {exe}: {type(e).__name__}")
    print("no browser found - open the HTML and print to PDF")


if __name__ == "__main__":
    main()
