#!/usr/bin/env python3
"""Render DATA_SOURCES.pdf - the scrapers, the lake, and every file in it.

    python graph/sample_lake.py --out graph/lake_sample.json   # on an S3 host
    python graph/make_data_doc.py

Every column list and every sample row comes from lake_sample.json, which was
read out of the bucket. Nothing here is transcribed by hand, so a source that
changes its columns shows the change the next time this is regenerated.

Sample rows are printed TRANSPOSED - column names down the left, five records
across. A ClinVar row has 43 columns and a trial row has long free text; as a
normal table either runs off the page or crushes every column to three
characters. Transposed, the widest file is a tall table instead of an
unreadable one, and the column list and the sample become the same table.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import sources as graph_sources                    # noqa: E402
from column_notes import (FILE_NOTE, SOURCE_PURPOSE,  # noqa: E402
                          choose_columns, notes_for)
from docgen import E, embed_png, page, render, table   # noqa: E402

HERE = pathlib.Path(__file__).parent
SAMPLE = HERE / "lake_sample.json"
OUT_HTML = HERE / "DATA_SOURCES.html"
OUT_PDF = HERE / "DATA_SOURCES.pdf"

CATEGORY_NOTE = {
    "Clinical_Trials_Pipeline_Intelligence":
        "Trial registries. Nine of them, deliberately overlapping: the same "
        "study is often registered in two places, and the second registration "
        "carries the sponsor or the enrolment the first one omitted.",
    "Drug_Substance_Reference":
        "What a substance <i>is</i>. Names, structures, identifiers, salt and "
        "parent relationships. Everything else in the lake refers to drugs by "
        "name, so this category is what makes those names resolvable.",
    "Literature_Evidence":
        "Papers and preprints. Titles and abstracts, no full text.",
    "MENA_GCC_Regulatory_Market":
        "Gulf and wider MENA registers. Small files, many of them, and the "
        "only coverage of these markets anywhere in the lake.",
    "Ontologies_Standards":
        "Controlled vocabularies. These supply the nodes that let two sources "
        "which never agree on wording point at the same concept.",
    "Regulatory_Approvals":
        "Agency registers, and the source of nearly every document in the "
        "lake. Products, approvals, patents, exclusivities, labels.",
    "Safety_Pharmacovigilance":
        "Adverse events, recalls, shortages, safety communications.",
    "Targets_Genomics_Biomarkers":
        "Proteins, genes, variants, and the assay data linking compounds to "
        "targets.",
}

DOC_KIND = {
    "ema.europa.eu": "EPARs - European Public Assessment Reports. Assessment "
                     "history, SmPC and package leaflet per product.",
    "mhra.gov.uk": "PARs - Public Assessment Reports, plus SPC and PIL. The "
                   "filename embeds the PL licence number, which is the only "
                   "reliable join from a document back to the graph.",
    "pmda.go.jp": "Japanese review reports and package inserts, mostly "
                  "Japanese-language.",
    "sfda.gov.sa": "Saudi FDA circulars, guidelines and product lists.",
    "who.int": "WHO guidance and prequalification documents.",
    "iso.org": "Standards abstracts.",
}


def human(n: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return ""


def declared_map() -> dict[str, dict]:
    """s3 file path -> its declaration in sources.py, if the graph reads it."""
    out = {}
    for d in graph_sources.INCLUDED:
        out[d["file"]] = d
    return out


def excluded_map() -> dict[str, str]:
    """path -> reason. EXCLUDED keys may be a file or a whole source prefix."""
    return dict(getattr(graph_sources, "EXCLUDED", {}))


def csv_block(cat: str, src: str, name: str, meta: dict,
              declared: dict, excluded: dict) -> str:
    p = []
    key = meta.get("key", "")
    p.append(f'<h4><code>{E(name)}</code></h4>')

    bits = [human(meta.get("size", 0))]
    if "n_columns" in meta:
        bits.append(f"{meta['n_columns']} columns")

    why = ""
    used = declared.get(key)
    # sources.py may declare a prefix rather than an exact key.
    if used is None:
        for f, d in declared.items():
            if f.endswith("/") and key.startswith(f):
                used = d
                break
    if used:
        bits.append("<b>read by the graph</b>")
    else:
        # EXCLUDED keys may name a file or a whole source prefix.
        why = excluded.get(key) or next(
            (r for f, r in excluded.items() if key.startswith(f)), "")
        bits.append("not read by the graph")
    p.append(f'<div class=meta>{" &middot; ".join(bits)}</div>')

    # What the file is for. For a file the graph reads, sources.py states it
    # exactly - what it builds and why. For one it does not, the exclusion
    # reason is the answer, and failing that the source's own purpose.
    if used:
        builds = ", ".join(f"<code>{E(b)}</code>"
                           for b in used.get("builds", []))
        serves = used.get("note", "")
        p.append(f"<p><b>Serves:</b> {serves or 'read by the build.'}"
                 + (f"<br><b>Builds:</b> {builds}" if builds else "") + "</p>")
    elif why:
        p.append(f"<p><b>Serves:</b> not used by the graph &mdash; "
                 f"{E(why)}</p>")
    else:
        pur = SOURCE_PURPOSE.get(src, "")
        p.append("<p><b>Serves:</b> published by the scraper and available in "
                 "the lake, but nothing in the graph schema consumes it."
                 + (f" {pur}" if pur else "") + "</p>")

    if "error" in meta:
        p.append(f'<div class=warn>could not sample: {E(meta["error"])}</div>')
        return "".join(p)

    cols = meta.get("columns", [])
    rows = meta.get("rows", [])
    if not cols:
        p.append('<div class=warn>no header row</div>')
        return "".join(p)

    if name in FILE_NOTE:
        p.append(f'<div class=note>{FILE_NOTE[name]}</div>')

    show, hidden = choose_columns(name, cols)
    if hidden:
        p.append(f'<div class=note>{len(cols):,} columns. Showing the '
                 f'{len(show)} that identify a record; <b>{hidden:,} '
                 f'hidden</b>.</div>')

    if not rows:
        p.append('<div class=warn>No data row could be sampled &mdash; the '
                 'file is header-only, or its first record is larger than the '
                 'read window.</div>')

    # Transposed: one row per column, one column per sample record.
    head = ["column"] + [f"row {i + 1}" for i in range(len(rows))]
    body = []
    for i in show:
        cells = [E(cols[i])]
        for r in rows:
            cells.append(E(r[i]) if i < len(r) else "<i>&mdash;</i>")
        body.append(cells)
    p.append(table(head, body, cls="tp"))

    # Explanations, after the table, for the columns that need one.
    notes = notes_for([cols[i] for i in show])
    if notes:
        p.append(table(["column", "what it means"],
                       [[f"<code>{E(c)}</code>", E(x)] for c, x in notes]))
    return "".join(p)


def main():
    if not SAMPLE.exists():
        sys.exit(f"missing {SAMPLE} - run graph/sample_lake.py on an S3 host")
    data = json.loads(SAMPLE.read_text(encoding="utf-8"))
    declared, excluded = declared_map(), excluded_map()

    n_src = sum(len(c) for c in data.values())
    n_csv = sum(len(s["csvs"]) for c in data.values() for s in c.values())
    n_doc = sum(sum(s["docs"].values()) for c in data.values()
                for s in c.values())

    p = ["<h1>The data, and how it connects</h1>",
         "<p class=sub>Every source, every file, every column &mdash; with "
         "real rows out of the bucket. Companion to "
         "<i>Graph Technical Reference</i>, which covers what happens to this "
         "data once it is read.</p>",
         f"<p class=meta>{n_src} sources &middot; {n_csv} CSV files &middot; "
         f"{n_doc:,} documents &middot; bucket <code>moine-data</code></p>"]

    # ---------------------------------------------------------------- 1
    p.append("<h2>1. What a scraper is</h2>")
    p.append("<p>One folder per source under <code>scrape/</code>, holding a "
             "<code>manifest.yaml</code> and the code to fetch. The manifest "
             "is the contract &mdash; the runner reads it, nothing else "
             "hardcodes a source.</p>")
    p.append("<pre>scrape/&lt;Category&gt;/&lt;site&gt;/\n"
             "    manifest.yaml     name, s3_base, enabled, schedule\n"
             "    scrape.py         fetch, normalise, write CSV\n"
             "</pre>")
    p.append("<p><code>enabled</code> is the switch. A disabled source is "
             "skipped entirely: it is not fetched, it publishes nothing, and "
             "so it triggers nothing downstream. Every source is disabled "
             "unless deliberately turned on.</p>")

    p.append("<h3>What a run does</h3>")
    p.append("<pre>1  fetch          the source's API, bulk file or pages\n"
             "2  normalise      into CSV with a stable header\n"
             "3  stage          write under _runs/&lt;run_id&gt;/\n"
             "4  commit         copy to the source's stable path\n"
             "                  rewrite _LATEST.json\n"
             "5  emit dataset   Dataset(\"s3://moine-data/&lt;s3_base&gt;\")</pre>")
    p.append("<div class=note><b>A commit replaces, it does not append.</b> "
             "Each source publishes a full snapshot at a stable key, "
             "overwritten each run. ClinicalTrials.gov holds exactly two "
             "objects: a 2.9 GB CSV and <code>_LATEST.json</code>. This is "
             "what makes double-counting structurally impossible &mdash; "
             "there is no incremental append to get wrong, and a rebuild from "
             "the same snapshot yields the same counts. Two consecutive "
             "builds on 2026-07-30 both produced exactly 1,049,701 "
             "ClinicalTrial nodes.</div>")
    p.append("<p>The cost of that choice is that the lake keeps no history: "
             "<code>_LATEST.json</code> records only the current run, so the "
             "gap between two scrapes cannot be recovered after the fact.</p>")

    # ---------------------------------------------------------------- 2
    p.append("<h2>2. How the two stores stay in step</h2>")
    p.append("<p>Step 5 above is the whole mechanism. A commit emits an "
             "Airflow <b>Dataset</b>, and the two sync DAGs are subscribed to "
             "datasets rather than to a clock.</p>")
    p.append("<pre>@weekly --&gt; scrapers_pipeline\n"
             "              commit task emits Dataset(\"s3://moine-data/&lt;s3_base&gt;\")\n"
             "                   |\n"
             "                   +--&gt; graph_sync          35 CSV-publishing sources\n"
             "                   |      build  ~24 min --&gt; validate --&gt; import ~8 min\n"
             "                   |\n"
             "                   +--&gt; vector_store_sync    8 document sources\n"
             "                          chunk --&gt; embed --&gt; upsert to Qdrant</pre>")
    p.append("<p>Neither sync DAG has a schedule of its own. They wake when "
             "data lands, so a sync cannot race the scrape that feeds it, and "
             "a week where nothing publishes costs nothing.</p>")
    p.append("<div class=note><b>Both use <code>DatasetAny</code>.</b> A plain "
             "list of datasets means AND in Airflow &mdash; the DAG would wait "
             "for <i>every</i> listed dataset. For <code>graph_sync</code> "
             "that means firing only when all 35 sources publish in one "
             "window, which is to say never, and silently.</div>")
    p.append("<p><code>graph_sync</code>'s trigger set is derived from "
             "<code>graph/sources.py</code> at DAG-parse time rather than "
             "listed, so declaring a new file there updates the trigger with "
             "nothing to keep in step by hand.</p>")

    p.append("<h3>Rebuild, not update</h3>")
    p.append("<p>The graph is not patched in place. Every sync rebuilds the "
             "whole graph from the current snapshots, validates it, and "
             "replaces the store. <code>neo4j-admin database import</code> "
             "has no transaction &mdash; it writes a fresh store &mdash; so "
             "the validator is what stands between a bad build and a live "
             "graph that answers confidently and wrongly.</p>")
    p.append("<p>The vector store <i>is</i> incremental, because re-embedding "
             "93,000 documents costs hours on GPU. Ingest compares each S3 "
             "object's ETag against what is indexed and touches only what "
             "changed; chunk IDs are deterministic, so re-ingesting a known "
             "document overwrites in place instead of duplicating.</p>")

    # ---------------------------------------------------------------- 3
    p.append("<h2>3. The graph schema</h2>")
    p.append(embed_png(HERE / "schema_phase2.png"))
    p.append("<p class=meta>Generated by <code>graph/make_schema_png.py</code>. "
             "22 node labels, 32 relationship types.</p>")

    # ---------------------------------------------------------------- 4
    p.append("<h2>4. The lake</h2>")
    p.append("<p>Eight categories. A source lives in exactly one, and its "
             "<code>s3_base</code> is <code>&lt;Category&gt;/&lt;site&gt;/"
             "</code>.</p>")
    rows = []
    for cat, srcs in data.items():
        nc = sum(len(s["csvs"]) for s in srcs.values())
        nd = sum(sum(s["docs"].values()) for s in srcs.values())
        by = sum(s["csv_bytes"] + s["doc_bytes"] for s in srcs.values())
        rows.append([f'<a href="#{cat}">{E(cat.replace("_", " "))}</a>',
                     str(len(srcs)), str(nc), f"{nd:,}" if nd else "&mdash;",
                     human(by)])
    p.append(table(["category", "sources", "CSVs", "documents", "size"], rows))

    p.append(f"<p>Of the {n_src} sources, <b>35 feed the graph</b> (77 "
             "declared files) and <b>8 publish documents</b> to the vector "
             "store. Three &mdash; ema, mhra, pmda &mdash; do both, so 40 "
             "sources are used and 9 are not. Exclusions are recorded with "
             "reasons in <code>graph/sources.py</code> and repeated against "
             "each file below.</p>")

    # ---------------------------------------------------------------- 5
    p.append("<h2>5. Every source, every file</h2>")
    p.append("<p>Columns are the real header. Rows are the first records of "
             "the real file, transposed so wide files stay readable; long "
             "values are cut at 90 characters.</p>")

    for cat, srcs in data.items():
        p.append(f'<h2 id="{cat}">{E(cat.replace("_", " "))}</h2>')
        if cat in CATEGORY_NOTE:
            p.append(f"<p>{CATEGORY_NOTE[cat]}</p>")
        for src in sorted(srcs):
            s = srcs[src]
            p.append(f'<h3>{E(src)}</h3>')
            meta = []
            if s["csvs"]:
                meta.append(f'{len(s["csvs"])} CSV '
                            f'({human(s["csv_bytes"])})')
            if s["docs"]:
                kinds = ", ".join(f"{n:,} {e}" for e, n in
                                  sorted(s["docs"].items(),
                                         key=lambda x: -x[1]))
                meta.append(f'{kinds} ({human(s["doc_bytes"])})')
            p.append(f'<div class=meta>{" &middot; ".join(meta) or "empty"}</div>')

            if s["docs"]:
                kind = DOC_KIND.get(src, "")
                if kind:
                    p.append(f"<p><b>Documents:</b> {kind}</p>")
                if s["examples"]:
                    ex = "<br>".join(f"<code>{E(x)}</code>"
                                     for x in s["examples"])
                    p.append(f"<p class=meta>example filenames:<br>{ex}</p>")

            for name in sorted(s["csvs"]):
                p.append('<div class=src>')
                p.append(csv_block(cat, src, name, s["csvs"][name],
                                   declared, excluded))
                p.append('</div>')

    render(page("The data, and how it connects", "".join(p)),
           OUT_HTML, OUT_PDF)


if __name__ == "__main__":
    main()
