#!/usr/bin/env python3
"""Draw the platform: scrapers, S3, both clouds, both stores.

    python graph/make_architecture_png.py

Read left to right in four numbered stages. The clouds are drawn as the
regions that enclose things, because which side of that boundary a box sits on
explains most of the design - the graph host derives everything from S3 and is
thrown away and rebuilt, the vector host holds embeddings that cost GPU-hours
and must be migrated.

Kept deliberately sparse. An earlier version put a paragraph in every box and
became unreadable; the detail belongs in GRAPH_TECHNICAL.pdf, and a diagram
that has to be studied is not doing its job. Each box gets a title, a handful
of figures, and at most one line of prose.

Node and edge totals are read from the live graph, so the picture cannot drift
from the database. If it is unreachable the image says so.
"""
from __future__ import annotations

import pathlib
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt                                # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "architecture.png"

INK    = "#080d1a"
PANEL  = "#121c2f"
PANEL2 = "#1a2740"
LINE   = "#27364f"
TEXT   = "#eef2f9"
MUTED  = "#9bafcb"
DIM    = "#65788f"
BLUE   = "#5b9bff"     # the graph
VIOLET = "#b39bfb"     # the document store
CYAN   = "#2ad7f0"     # collection
GREEN  = "#3ddba4"     # orchestration
AMBER  = "#fcc63a"
AWS    = "#ff9f2e"
AZURE  = "#41a7ff"


def live() -> dict:
    snap = {"nodes": "13.68M", "edges": "16.82M", "note": "snapshot 2026-07-31"}
    try:
        sys.path.insert(0, str(HERE.parent / "testPipeline"))
        import ask as A                                        # noqa: PLC0415
        n, _ = A.run_cypher("MATCH (n) RETURN count(n) AS n")
        e, _ = A.run_cypher("MATCH ()-[r]->() RETURN count(r) AS n")
        f = lambda v: f"{v/1_000_000:.2f}M"
        return {"nodes": f(n[0]["n"]), "edges": f(e[0]["n"]), "note": "live"}
    except Exception:                                          # noqa: BLE001
        return snap


def box(ax, x, y, w, h, accent, fill=PANEL, lw=1.1, edge=None):
    ax.add_patch(FancyBboxPatch(
        (x, y), w, h, boxstyle="round,pad=0,rounding_size=0.014",
        facecolor=fill, edgecolor=edge or LINE, linewidth=lw, zorder=2))
    if accent:                       # the coloured spine down the left edge
        ax.add_patch(FancyBboxPatch(
            (x, y), 0.0035, h, boxstyle="round,pad=0,rounding_size=0.001",
            facecolor=accent, edgecolor="none", zorder=3))


def t(ax, x, y, s, size=9, color=TEXT, w="normal", ha="left", va=None, z=5):
    # Multi-line text hangs down from its anchor. Matplotlib centres it, which
    # drops a paragraph on top of the heading above it.
    if va is None:
        va = "top" if "\n" in s else "center"
    ax.text(x, y, s, fontsize=size, color=color, fontweight=w, ha=ha, va=va,
            family="DejaVu Sans", zorder=z, linespacing=1.5)


def rows(ax, x, y, right, pairs, gap=0.030, size=9.2):
    """A label/value list - the densest readable way to show figures."""
    for i, (k, v) in enumerate(pairs):
        yy = y - i * gap
        t(ax, x, yy, k, size=size, color=MUTED)
        t(ax, right, yy, v, size=size, color=TEXT, w="bold", ha="right")


def flow(ax, x0, y0, x1, y1, color, lw=2.4, rad=0.0, ls="-"):
    ax.add_patch(FancyArrowPatch(
        (x0, y0), (x1, y1), arrowstyle="-|>", mutation_scale=15, color=color,
        linewidth=lw, zorder=4, linestyle=ls,
        connectionstyle=f"arc3,rad={rad}", shrinkA=3, shrinkB=3))


def stage(ax, x, y, n, label, color):
    """A numbered stage marker above a column."""
    ax.add_patch(plt.Circle((x, y), 0.0105, facecolor=color, edgecolor="none",
                            zorder=5))
    t(ax, x, y - 0.0005, str(n), size=9.5, color=INK, w="bold", ha="center")
    t(ax, x + 0.019, y, label, size=11, color=color, w="bold")


def region(ax, x, y, w, h, label, color):
    ax.add_patch(FancyBboxPatch(
        (x, y), w, h, boxstyle="round,pad=0,rounding_size=0.016",
        facecolor="#0d1424", edgecolor=color, linewidth=1.5,
        linestyle=(0, (7, 5)), zorder=1))
    t(ax, x + w - 0.014, y + h - 0.026, label, size=10.5, color=color,
      w="bold", ha="right")


def main():
    g = live()
    fig, ax = plt.subplots(figsize=(20, 10.2), dpi=150)
    fig.patch.set_facecolor(INK)
    ax.set_facecolor(INK)
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")

    t(ax, 0.030, 0.960, "Biolyt — platform architecture", size=19, w="bold")
    t(ax, 0.030, 0.928,
      "49 scrapers  →  one S3 lake  →  a knowledge graph and a document "
      "store, kept current without anyone touching them",
      size=11, color=MUTED)
    t(ax, 0.970, 0.960, f"figures: {g['note']}", size=9, color=DIM, ha="right")

    # Column geometry.
    #
    # Every y is chosen against a budget rather than by eye: the cloud regions
    # occupy 0.270-0.855, the stage markers sit clear above them at 0.900, and
    # the CSV flow runs in the corridor between so it crosses nothing. The
    # first version packed text into heights nobody had checked and five cards
    # overflowed their boxes.
    C = [0.030, 0.268, 0.505, 0.742]
    W = 0.212
    PAD = 0.018
    R_TOP, R_BOT = 0.855, 0.270

    stage(ax, C[0] + 0.010, 0.900, 1, "COLLECT", CYAN)
    stage(ax, C[1] + 0.010, 0.900, 2, "LAND", AWS)
    stage(ax, C[2] + 0.010, 0.900, 3, "PROCESS", GREEN)
    stage(ax, C[3] + 0.010, 0.900, 4, "SERVE", BLUE)

    # ---------------------------------------------------------- 1 COLLECT
    box(ax, C[0], 0.600, W, 0.255, CYAN)
    t(ax, C[0] + PAD, 0.828, "49 scrapers", size=13, w="bold")
    t(ax, C[0] + PAD, 0.803, "one folder and a manifest each", size=8.6,
      color=DIM)
    rows(ax, C[0] + PAD, 0.773, C[0] + W - PAD,
         [("Clinical trials", "9"), ("Regulatory approvals", "7"),
          ("MENA / GCC", "6"), ("Drug & substance", "5"),
          ("Targets & genomics", "5"), ("Literature", "4"),
          ("Ontologies", "3"), ("Safety / PV", "2")], gap=0.0200, size=8.6)
    t(ax, C[0] + PAD, 0.617, "41 in use  \u00b7  8 excluded, with reasons",
      size=8.4, color=CYAN)

    box(ax, C[0], 0.335, W, 0.245, GREEN, fill=PANEL2)
    t(ax, C[0] + PAD, 0.552, "scrapers_pipeline", size=12, w="bold",
      color=GREEN)
    t(ax, C[0] + PAD, 0.527, "@weekly \u2014 the only clock in the system",
      size=8.6, color=DIM)
    for i2, (st, d) in enumerate(
            [("hydrate", "restore cursors"), ("scrape", "fetch"),
             ("normalise", "stable CSV header"), ("stage", "write aside"),
             ("commit", "publish atomically"), ("emit", "dataset event")]):
        yy = 0.492 - i2 * 0.0232
        t(ax, C[0] + 0.020, yy, str(i2 + 1), size=8, color=GREEN, w="bold")
        t(ax, C[0] + 0.034, yy, st, size=8.8, w="bold")
        t(ax, C[0] + 0.100, yy, d, size=8.2, color=DIM)
    t(ax, C[0] + PAD, 0.350, "a commit REPLACES \u2014 never appends",
      size=8.4, color=AMBER)

    # ------------------------------------------------------------- 2 LAND
    box(ax, C[1], 0.600, W, 0.255, AWS, fill=PANEL2, edge=AWS, lw=1.4)
    t(ax, C[1] + PAD, 0.828, "S3  \u00b7  moine-data", size=13, w="bold",
      color=AWS)
    t(ax, C[1] + PAD, 0.803, "the one place data lands", size=8.6, color=DIM)
    rows(ax, C[1] + PAD, 0.766, C[1] + W - PAD,
         [("CSV files", "432"), ("documents", "93,505"),
          ("sources", "49"), ("categories", "8")], gap=0.031, size=9.6)
    t(ax, C[1] + PAD, 0.652,
      "Full snapshots, overwritten each run.\nNo append, so no double "
      "counting.", size=8.4, color=MUTED)

    box(ax, C[1], 0.335, W, 0.245, GREEN)
    t(ax, C[1] + PAD, 0.552, "Dataset events", size=12, w="bold", color=GREEN)
    t(ax, C[1] + PAD, 0.527, "each commit emits exactly one", size=8.6,
      color=DIM)
    t(ax, C[1] + PAD, 0.489, "graph_sync", size=10, w="bold", color=BLUE)
    t(ax, C[1] + W - PAD, 0.489, "35 CSV sources", size=8.6, color=MUTED,
      ha="right")
    t(ax, C[1] + PAD, 0.457, "vector_store_sync", size=10, w="bold",
      color=VIOLET)
    t(ax, C[1] + W - PAD, 0.457, "10 doc sources", size=8.6, color=MUTED,
      ha="right")
    t(ax, C[1] + PAD, 0.420,
      "No clock of their own \u2014 they wake when\ndata lands, so a sync "
      "cannot race the\nscrape that feeds it.", size=8.4, color=MUTED)

    # --------------------------------------------------------- 3 PROCESS
    region(ax, C[2] - 0.014, R_BOT, W + 0.028, R_TOP - R_BOT, "AWS", AWS)

    box(ax, C[2], 0.700, W, 0.135, GREEN)
    t(ax, C[2] + PAD, 0.812, "Airflow", size=12.5, w="bold", color=GREEN)
    t(ax, C[2] + PAD, 0.788, "scheduler \u00b7 webserver \u00b7 Postgres",
      size=8.4, color=DIM)
    t(ax, C[2] + PAD, 0.760,
      "Runs both sync pipelines and drives\nthe Azure host over SSH.",
      size=8.4, color=MUTED)

    box(ax, C[2], 0.480, W, 0.195, VIOLET)
    t(ax, C[2] + PAD, 0.650, "Qdrant", size=12.5, w="bold", color=VIOLET)
    t(ax, C[2] + PAD, 0.626, "document store", size=8.4, color=DIM)
    rows(ax, C[2] + PAD, 0.594, C[2] + W - PAD,
         [("chunks", "3,240,756"), ("documents", "92,397"),
          ("model", "bge-m3"), ("on disk", "~27 GB")], gap=0.026, size=9.2)
    t(ax, C[2] + PAD, 0.502, "extract \u2192 chunk \u2192 embed "
      "\u2192 upsert", size=8.2, color=DIM)

    box(ax, C[2], 0.290, W, 0.165, VIOLET, fill=PANEL2)
    t(ax, C[2] + PAD, 0.430, "search API   :8000", size=11.5, w="bold",
      color=VIOLET)
    t(ax, C[2] + PAD, 0.400,
      "Filtered hybrid retrieval with rerank\nand a fixed 0.6 relevance "
      "floor.\nReturns chunks with provenance.", size=8.4, color=MUTED)
    t(ax, C[2] + PAD, 0.304, "irreplaceable \u2014 back up before replacing",
      size=8.2, color=AMBER)

    # ----------------------------------------------------------- 4 SERVE
    region(ax, C[3] - 0.014, R_BOT, W + 0.028, R_TOP - R_BOT, "AZURE", AZURE)

    box(ax, C[3], 0.620, W, 0.215, BLUE)
    t(ax, C[3] + PAD, 0.812, "graph build", size=12.5, w="bold", color=BLUE)
    t(ax, C[3] + PAD, 0.788, "2 vCPU \u00b7 16 GB \u00b7 rebuilt, never "
      "patched", size=8.4, color=DIM)
    for i2, (st, d) in enumerate(
            [("build", "96 CSVs \u2192 tables"),
             ("validate", "or it stops here"),
             ("import", "replaces the store"),
             ("test", "182 answer checks")]):
        yy = 0.753 - i2 * 0.027
        t(ax, C[3] + PAD, yy, st, size=9.2, w="bold")
        t(ax, C[3] + 0.086, yy, d, size=8.4, color=DIM)
    t(ax, C[3] + PAD, 0.636, "~35 minutes end to end", size=8.4, color=BLUE)

    box(ax, C[3], 0.290, W, 0.305, BLUE)
    t(ax, C[3] + PAD, 0.568, "Neo4j", size=12.5, w="bold", color=BLUE)
    t(ax, C[3] + PAD, 0.544, "knowledge graph   \u00b7   bolt :7687",
      size=8.4, color=DIM)
    rows(ax, C[3] + PAD, 0.508, C[3] + W - PAD,
         [("nodes", g["nodes"]), ("relationships", g["edges"]),
          ("entity types", "22"), ("relationship types", "32"),
          ("full-text indexes", "3"), ("embeddings", "40,432")],
         gap=0.028, size=9.2)
    t(ax, C[3] + PAD, 0.332,
      "No irreplaceable state \u2014 every node is\nderived from S3 and "
      "rebuildable.", size=8.4, color=GREEN)

    # ------------------------------------------------------------- flows
    flow(ax, C[0] + W, 0.728, C[1] - 0.003, 0.728, CYAN)
    flow(ax, C[0] + 0.106, 0.580, C[0] + 0.106, 0.596, CYAN, lw=2)
    flow(ax, C[1] + 0.106, 0.580, C[1] + 0.106, 0.596, GREEN, lw=2)

    # Documents: straight in, at a height clear of both AWS cards.
    flow(ax, C[1] + W, 0.470, C[2] - 0.016, 0.470, VIOLET)
    t(ax, C[1] + W + 0.006, 0.452, "documents", size=8.4, color=VIOLET)

    # CSV: up into the corridor above both regions, across, and down - drawn
    # straight it went through the AWS box and its label sat on the heading.
    XR = C[1] + W + 0.006
    ax.plot([C[1] + W, XR], [0.500, 0.500], color=BLUE, lw=2.4, zorder=4)
    ax.plot([XR, XR], [0.500, 0.872], color=BLUE, lw=2.4, zorder=4)
    ax.plot([XR, C[3] + 0.106], [0.872, 0.872], color=BLUE, lw=2.4, zorder=4)
    flow(ax, C[3] + 0.106, 0.872, C[3] + 0.106, 0.842, BLUE)
    t(ax, C[2] + 0.030, 0.884,
      "CSV  \u00b7  graph_sync drives the Azure host over SSH", size=8.6,
      color=BLUE)

    # --------------------------------------------------------- the consumer
    box(ax, C[0], 0.095, W + 0.237, 0.150, AMBER, fill=PANEL2)
    t(ax, C[0] + PAD, 0.220, "Research agent", size=12.5, w="bold",
      color=AMBER)
    t(ax, C[0] + PAD, 0.192,
      "Every question queries BOTH stores, then answers only from what they "
      "returned \u2014\nCypher over bolt :7687 and semantic search over "
      ":8000, with the sources named.\nThe two are joined by identifiers, "
      "not shared storage: an MHRA filename\ncarries the licence number the "
      "graph indexes.", size=8.8, color=MUTED)

    flow(ax, C[0] + W + 0.239, 0.170, C[2] + 0.100, 0.282, AMBER, lw=1.8,
         ls=(0, (5, 4)), rad=-0.15)
    flow(ax, C[0] + W + 0.239, 0.150, C[3] + 0.100, 0.282, AMBER, lw=1.8,
         ls=(0, (5, 4)), rad=-0.07)

    # ------------------------------------------------------------ footnote
    t(ax, 0.030, 0.055,
      "Two clouds because the memory profiles conflict — Qdrant holds ~8 GB "
      "of vectors resident and Neo4j wants 4 GB heap plus 4 GB page cache, so "
      "on one 16 GB box the larger evicts the smaller.",
      size=9, color=DIM)
    t(ax, 0.030, 0.030,
      "The asymmetry that matters — the graph host can be destroyed and "
      "rebuilt from S3 in half an hour; the vector host holds embeddings that "
      "cost GPU-hours and has to be migrated.",
      size=9, color=DIM)

    fig.savefig(OUT, facecolor=INK, bbox_inches="tight", pad_inches=0.25)
    print(f"wrote {OUT}  ({OUT.stat().st_size/1024:.0f} KB)  "
          f"figures: {g['note']}")


if __name__ == "__main__":
    main()
