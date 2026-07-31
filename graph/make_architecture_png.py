#!/usr/bin/env python3
"""Draw the whole platform: scrapers, S3, both clouds, both stores.

    python graph/make_architecture_png.py

One picture answering "where does anything actually run". The two clouds are
drawn as the enclosing regions they are, because which side of that boundary a
box sits on is the fact that explains most of the design - the graph host
holds no irreplaceable state and is thrown away and rebuilt, the vector host
holds ~27 GB of embeddings and has to be migrated.

Figures are read from the live graph where possible so the diagram cannot
drift; if the database is unreachable it falls back to a dated snapshot and
says so on the image.
"""
from __future__ import annotations

import pathlib
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt                              # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "architecture.png"

# Palette shared with the deck, so the two read as one system.
INK     = "#070c18"
PANEL   = "#111a2b"
PANEL2  = "#18243a"
LINE    = "#25334d"
TEXT    = "#ecf1f9"
MUTED   = "#93a6c4"
DIM     = "#63768f"
BLUE    = "#4c8dff"      # the graph
VIOLET  = "#a78bfa"      # the document store
CYAN    = "#22d3ee"      # acquisition
GREEN   = "#34d399"      # orchestration
AMBER   = "#fbbf24"
AWS     = "#ff9900"
AZURE   = "#3aa0ff"


def live_figures() -> dict:
    """Node and edge totals from Neo4j, or a dated snapshot."""
    snap = {"nodes": "13.7M", "edges": "16.8M", "trials": "1.02M",
            "note": "snapshot 2026-07-31"}
    try:
        sys.path.insert(0, str(HERE.parent / "testPipeline"))
        import ask as A                                      # noqa: PLC0415
        n, _ = A.run_cypher("MATCH (n) RETURN count(n) AS n")
        e, _ = A.run_cypher("MATCH ()-[r]->() RETURN count(r) AS n")
        t, _ = A.run_cypher("MATCH (n:ClinicalTrial) RETURN count(n) AS n")
        m = lambda v: f"{v/1_000_000:.2f}M" if v >= 1_000_000 else f"{v:,}"
        return {"nodes": m(n[0]["n"]), "edges": m(e[0]["n"]),
                "trials": m(t[0]["n"]), "note": "live"}
    except Exception:                                        # noqa: BLE001
        return snap


def box(ax, x, y, w, h, *, fill=PANEL, edge=None, lw=1.2, r=0.018, z=2):
    p = FancyBboxPatch((x, y), w, h,
                       boxstyle=f"round,pad=0,rounding_size={r}",
                       facecolor=fill, edgecolor=edge or LINE,
                       linewidth=lw, zorder=z)
    ax.add_patch(p)
    return p


def bar(ax, x, y, h, color, w=0.004, z=3):
    ax.add_patch(FancyBboxPatch((x, y), w, h,
                                boxstyle="round,pad=0,rounding_size=0.002",
                                facecolor=color, edgecolor="none", zorder=z))


def txt(ax, x, y, s, size=9, color=TEXT, weight="normal", ha="left",
        va=None, family="DejaVu Sans", z=5):
    """Text at (x, y).

    A multi-line block hangs DOWN from its anchor by default. Centred - which
    is matplotlib's behaviour and was the original default here - a five-line
    paragraph climbs half its height above the anchor and lands on the heading
    above it. Every overlap in the first render was that.
    """
    if va is None:
        va = "top" if "\n" in s else "center"
    ax.text(x, y, s, fontsize=size, color=color, fontweight=weight, ha=ha,
            va=va, family=family, zorder=z, linespacing=1.45)


def arrow(ax, xy0, xy1, color=LINE, lw=1.6, style="-|>", rad=0.0, z=4,
          ls="-"):
    ax.add_patch(FancyArrowPatch(
        xy0, xy1, arrowstyle=style, mutation_scale=12, color=color,
        linewidth=lw, zorder=z, linestyle=ls,
        connectionstyle=f"arc3,rad={rad}", shrinkA=2, shrinkB=2))


def cloud(ax, x, y, w, h, label, color):
    """A dashed region marking which cloud something runs in."""
    ax.add_patch(FancyBboxPatch(
        (x, y), w, h, boxstyle="round,pad=0,rounding_size=0.02",
        facecolor="none", edgecolor=color, linewidth=1.6,
        linestyle=(0, (6, 4)), zorder=1, alpha=0.85))
    txt(ax, x + 0.012, y + h - 0.022, label, size=10.5, color=color,
        weight="bold")


def main():
    fig = live_figures()
    f, ax = plt.subplots(figsize=(19.2, 10.4), dpi=150)
    f.patch.set_facecolor(INK)
    ax.set_facecolor(INK)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    txt(ax, 0.028, 0.962, "Biolyt — platform architecture", size=17,
        weight="bold")
    txt(ax, 0.028, 0.934,
        "49 scrapers → one S3 lake → a knowledge graph and a document store, "
        "kept current by dataset-triggered pipelines", size=10.2, color=MUTED)
    txt(ax, 0.972, 0.962, f"figures: {fig['note']}", size=8.5, color=DIM,
        ha="right")

    # ---------------------------------------------------------- 1. sources
    box(ax, 0.028, 0.60, 0.20, 0.285, fill=PANEL)
    bar(ax, 0.028, 0.60, 0.285, CYAN)
    txt(ax, 0.045, 0.862, "SOURCES", size=9.5, color=CYAN, weight="bold")
    txt(ax, 0.045, 0.838, "49 scrapers · 8 categories", size=9, color=TEXT)
    cats = [("Drug & substance", "5"), ("Clinical trials", "9"),
            ("Regulatory approvals", "7"), ("Safety / PV", "2"),
            ("Targets & genomics", "5"), ("Ontologies", "3"),
            ("Literature", "4"), ("MENA / GCC", "6")]
    for i, (c, n) in enumerate(cats):
        yy = 0.812 - i * 0.0235
        txt(ax, 0.045, yy, c, size=8.2, color=MUTED)
        txt(ax, 0.218, yy, n, size=8.2, color=DIM, ha="right")
    txt(ax, 0.045, 0.622, "41 in use · 8 excluded, with reasons", size=7.8,
        color=DIM)

    # ------------------------------------------------- 2. the scraper DAG
    box(ax, 0.028, 0.30, 0.20, 0.265, fill=PANEL2)
    bar(ax, 0.028, 0.30, 0.265, GREEN)
    txt(ax, 0.045, 0.542, "scrapers_pipeline", size=9.5, color=GREEN,
        weight="bold")
    txt(ax, 0.045, 0.519, "@weekly — the only clock", size=8.4, color=MUTED)
    stages = [("hydrate", "restore cursors + last CSV"),
              ("scrape", "API, bulk file or pages"),
              ("normalise", "CSV with a stable header"),
              ("stage", "write under _runs/<id>/"),
              ("commit", "copy to the stable key"),
              ("emit", "Dataset(s3://moine-data/…)")]
    for i, (s, d) in enumerate(stages):
        yy = 0.489 - i * 0.0295
        txt(ax, 0.048, yy, f"{i+1}", size=7.6, color=GREEN, weight="bold")
        txt(ax, 0.063, yy, s, size=8.4, color=TEXT, weight="bold")
        txt(ax, 0.115, yy, d, size=7.6, color=DIM)
    txt(ax, 0.045, 0.313, "a commit REPLACES — no incremental append",
        size=7.6, color=AMBER)

    arrow(ax, (0.128, 0.598), (0.128, 0.568), color=CYAN, lw=2)

    # ------------------------------------------------------------- 3. S3
    box(ax, 0.262, 0.585, 0.185, 0.30, fill=PANEL2, edge=AWS, lw=1.4)
    bar(ax, 0.262, 0.585, 0.30, AWS)
    txt(ax, 0.279, 0.862, "S3 · moine-data", size=10, color=AWS,
        weight="bold")
    txt(ax, 0.279, 0.838, "the one place data lands", size=8.4, color=MUTED)
    for i, (k, v) in enumerate([("CSV files", "432"),
                                ("documents (PDF)", "93,505"),
                                ("sources", "49"),
                                ("categories", "8")]):
        yy = 0.806 - i * 0.026
        txt(ax, 0.279, yy, k, size=8.4, color=MUTED)
        txt(ax, 0.437, yy, v, size=8.4, color=TEXT, ha="right", weight="bold")
    txt(ax, 0.279, 0.688,
        "Full snapshots, overwritten each run.\nNo history is kept, and no\n"
        "double counting is possible.", size=7.8, color=DIM)
    txt(ax, 0.279, 0.612, "_LATEST.json records the current run", size=7.6,
        color=DIM)

    arrow(ax, (0.232, 0.43), (0.352, 0.43), color=GREEN, lw=1.8, rad=-0.0)
    arrow(ax, (0.352, 0.43), (0.352, 0.578), color=GREEN, lw=1.8)
    txt(ax, 0.238, 0.447, "publish", size=7.8, color=GREEN)

    # ------------------------------------------- 4. dataset events fan out
    box(ax, 0.262, 0.30, 0.185, 0.255, fill=PANEL)
    bar(ax, 0.262, 0.30, 0.255, GREEN)
    txt(ax, 0.279, 0.532, "Dataset events", size=9.5, color=GREEN,
        weight="bold")
    txt(ax, 0.279, 0.508,
        "Each commit emits one event.\nThe sync pipelines have NO\n"
        "clock — they wake when data\nactually lands, so a sync\n"
        "cannot race its own scrape.",
        size=8, color=MUTED)
    txt(ax, 0.279, 0.398, "graph_sync", size=8.6, color=BLUE, weight="bold")
    txt(ax, 0.437, 0.398, "35 CSV sources", size=8, color=DIM, ha="right")
    txt(ax, 0.279, 0.374, "vector_store_sync", size=8.6, color=VIOLET,
        weight="bold")
    txt(ax, 0.437, 0.374, "10 doc sources", size=8, color=DIM, ha="right")
    txt(ax, 0.279, 0.335,
        "DatasetAny (OR). A plain list\nmeans AND — it would fire never.",
        size=7.5, color=AMBER)

    # ================================================== AWS region
    cloud(ax, 0.478, 0.055, 0.245, 0.60, "AWS  ·  us-east-1", AWS)

    box(ax, 0.494, 0.455, 0.213, 0.155, fill=PANEL)
    bar(ax, 0.494, 0.455, 0.155, GREEN)
    txt(ax, 0.510, 0.585, "Airflow 2.10", size=9.5, color=GREEN,
        weight="bold")
    txt(ax, 0.510, 0.562, "scheduler · webserver · Postgres", size=7.8,
        color=MUTED)
    txt(ax, 0.510, 0.535,
        "Runs BOTH sync DAGs. Drives the\ngraph host over SSH — a local\n"
        "BashOperator has no graph code,\nno Neo4j and no spare memory.",
        size=7.6, color=DIM)

    box(ax, 0.494, 0.232, 0.213, 0.205, fill=PANEL)
    bar(ax, 0.494, 0.232, 0.205, VIOLET)
    txt(ax, 0.510, 0.410, "Qdrant — document store", size=9.5, color=VIOLET,
        weight="bold")
    for i, (k, v) in enumerate([("chunks", "3,240,756"),
                                ("documents", "92,397"),
                                ("model", "bge-m3, 1024-d"),
                                ("on disk", "~27 GB")]):
        yy = 0.383 - i * 0.024
        txt(ax, 0.510, yy, k, size=8.2, color=MUTED)
        txt(ax, 0.697, yy, v, size=8.2, color=TEXT, ha="right")
    txt(ax, 0.510, 0.300,
        "extract → chunk → embed → upsert\nIncremental by S3 ETag; only\n"
        "changed documents are re-embedded.", size=7.6, color=DIM)

    box(ax, 0.494, 0.085, 0.213, 0.135, fill=PANEL2)
    bar(ax, 0.494, 0.085, 0.135, VIOLET)
    txt(ax, 0.510, 0.196, "search API  :8000", size=9.2, color=VIOLET,
        weight="bold")
    txt(ax, 0.510, 0.172,
        "POST /search — filtered hybrid\nretrieval with rerank, 0.6 floor.\n"
        "Returns chunks with provenance.", size=7.6, color=DIM)
    txt(ax, 0.510, 0.108, "irreplaceable state — back up before replacing",
        size=7.4, color=AMBER)

    # ================================================== Azure region
    cloud(ax, 0.742, 0.055, 0.232, 0.60, "AZURE  ·  graph host", AZURE)

    box(ax, 0.757, 0.395, 0.201, 0.215, fill=PANEL)
    bar(ax, 0.757, 0.395, 0.215, BLUE)
    txt(ax, 0.772, 0.585, "graph build", size=9.5, color=BLUE, weight="bold")
    txt(ax, 0.772, 0.562, "2 vCPU · 16 GB · 29 GB disk", size=7.8,
        color=MUTED)
    for i, (s, d) in enumerate([("build", "stream 96 CSVs → node/edge tables"),
                                ("validate", "integrity, fixtures, coverage"),
                                ("stage", "types, headers, newlines"),
                                ("import", "neo4j-admin, replaces the store"),
                                ("test", "182 answer checks, or it fails")]):
        yy = 0.532 - i * 0.0255
        txt(ax, 0.772, yy, s, size=8.2, color=TEXT, weight="bold")
        txt(ax, 0.828, yy, d, size=7.4, color=DIM)
    txt(ax, 0.772, 0.405, "~35 min end to end · rebuilt, never patched",
        size=7.5, color=DIM)

    box(ax, 0.757, 0.145, 0.201, 0.225, fill=PANEL)
    bar(ax, 0.757, 0.145, 0.225, BLUE)
    txt(ax, 0.772, 0.345, "Neo4j 5.26 — knowledge graph", size=9.5,
        color=BLUE, weight="bold")
    for i, (k, v) in enumerate([("nodes", fig["nodes"]),
                                ("relationships", fig["edges"]),
                                ("entity types", "22"),
                                ("relationship types", "32"),
                                ("full-text indexes", "3"),
                                ("entity embeddings", "40,432 SapBERT")]):
        yy = 0.317 - i * 0.0235
        txt(ax, 0.772, yy, k, size=8.2, color=MUTED)
        txt(ax, 0.948, yy, v, size=8.2, color=TEXT, ha="right")
    txt(ax, 0.772, 0.172,
        "bolt :7687\nno irreplaceable state — rebuildable from S3 in ~35 min",
        size=7.3, color=GREEN)

    # ---- flows into the two stores
    # Stops at the Qdrant edge - drawn to 0.60 it ran through the box.
    arrow(ax, (0.449, 0.392), (0.492, 0.335), color=VIOLET, lw=2, rad=-0.10)
    txt(ax, 0.452, 0.352, "documents", size=7.6, color=VIOLET)
    # Routed above both cloud regions rather than across them - drawn through,
    # the line and its label sat on top of the AWS box and the AZURE heading.
    ax.plot([0.449, 0.462], [0.418, 0.712], color=BLUE, lw=2, zorder=4)
    ax.plot([0.462, 0.858], [0.712, 0.712], color=BLUE, lw=2, zorder=4)
    arrow(ax, (0.858, 0.712), (0.858, 0.660), color=BLUE, lw=2)
    txt(ax, 0.520, 0.742,
        "CSV  ·  graph_sync drives the Azure host over SSH",
        size=7.8, color=BLUE)

    # ---- consumer
    box(ax, 0.262, 0.062, 0.185, 0.205, fill=PANEL2)
    bar(ax, 0.262, 0.062, 0.205, AMBER)
    txt(ax, 0.279, 0.248, "Research agent / test rig", size=9.5, color=AMBER,
        weight="bold")
    txt(ax, 0.279, 0.228,
        "Every question queries BOTH:\n"
        "  · Cypher over bolt :7687\n"
        "  · semantic search over :8000\n"
        "then answers from those two\nresult sets only, citing sources.",
        size=7.8, color=MUTED)
    txt(ax, 0.279, 0.118,
        "Joined by identifiers, not shared\nstorage — MHRA filenames carry "
        "the\nlicence number the graph indexes.", size=7.3, color=DIM)

    arrow(ax, (0.449, 0.15), (0.492, 0.15), color=AMBER, lw=1.5, ls="--")
    arrow(ax, (0.449, 0.20), (0.755, 0.235), color=AMBER, lw=1.5, ls="--",
          rad=-0.06)

    # ---- footnote
    txt(ax, 0.028, 0.032,
        "Two clouds because the memory profiles conflict: Qdrant holds ~8 GB "
        "of vectors resident and Neo4j wants 4 GB heap + 4 GB page cache, so "
        "on one 16 GB box the larger evicts the smaller.",
        size=8, color=DIM)
    txt(ax, 0.028, 0.012,
        "The asymmetry that matters: the graph host derives everything from "
        "S3 and is thrown away and rebuilt; the vector host holds embeddings "
        "that cost GPU-hours and must be migrated.",
        size=8, color=DIM)

    f.savefig(OUT, facecolor=INK, bbox_inches="tight", pad_inches=0.22)
    print(f"wrote {OUT}  ({OUT.stat().st_size/1024:.0f} KB)  figures: "
          f"{fig['note']}")


if __name__ == "__main__":
    main()
