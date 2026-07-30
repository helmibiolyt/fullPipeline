#!/usr/bin/env python3
"""Render the Phase-2 (extended) graph schema as a PNG.

Phase 1 = the 15 nodes / 19 edges specified in SCHEMA.md.
Phase 2 adds five nodes, drawn highlighted:

  AdverseEvent      openfda FAERS ~2.9M reports (aggregated to counts)
  Publication       the literature CSVs
  Patent            Orange Book ~16.3k + Purple Book 424
  Exclusivity       Orange Book 2,265 + Purple Book exclusivity columns
  RegulatoryEvent   EMA referrals/shortages/orphan, FDA recalls, SFDA alerts

The last three came from re-profiling the lake on 2026-07-28, which surfaced
four FDA sources the earlier profile had missed.
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, Circle
import numpy as np

W, H = 21, 16.0
fig, ax = plt.subplots(figsize=(W, H), dpi=150)
ax.set_xlim(0, 100); ax.set_ylim(-28, 110); ax.axis("off")
fig.patch.set_facecolor("white")

CORE   = "#1f5fbf"   # Substance / Product
BIO    = "#c0392b"   # Disease
TARGET = "#7d3ac1"   # Target
CLIN   = "#0e7c8a"   # ClinicalTrial
COMP   = "#c8901a"   # Company
REG    = "#8a4b08"   # Agency / Approval
CLASS  = "#1a8a5a"   # Mechanism / DrugClass / Modality
GREY   = "#5d6d7e"   # Country / Region / Route / Identifier
LIT    = "#8e44ad"   # Publication
SAFE   = "#b03a2e"   # AdverseEvent / RegulatoryEvent
NEW    = "#d13b6e"   # newest additions: Variant, OrganClass

#            x    y    r    colour  label                  new?
# Layout rules that keep this readable, for whoever edits it next:
#   * Substance and Product form a vertical spine; Substance's neighbours fan
#     across the top half and Product's across the bottom, so the two hubs'
#     spokes never cross each other.
#   * Nodes joined to each other (ClinicalTrial-Company-Country,
#     Approval-RegulatoryAgency, Publication-Disease) sit adjacent, so those
#     chords stay short instead of cutting across the middle.
#   * Publication sits left of the Substance->Disease line, not on it.
NODES = {
 "Substance":        (40, 56, 6.4, CORE,   "Substance",           False),
 "Product":          (40, 28, 5.6, CORE,   "Product",             False),
 # --- Substance's neighbours: top half, left to right ---
 "Modality":         (4,  58, 3.4, CLASS,  "Modality",            False),
 "DrugClass":        (6,  70, 4.2, CLASS,  "DrugClass",           False),
 "Mechanism":        (15, 82, 4.2, CLASS,  "Mechanism",           False),
 "Target":           (33, 88, 4.8, TARGET, "Target",              False),
 "Publication":      (48, 74, 4.6, LIT,       "Publication",         False),
 "Disease":          (69, 80, 5.2, BIO,    "Disease",             False),
 "Country":          (96, 74, 3.6, GREY,   "Country",             False),
 # Variant hangs off Target, not off a Gene node: HGNC already maps symbol
 # to the UniProt accession Target is keyed by, so the gene step collapses.
 # Above Publication, not above Target: at x=22,y=96 it was clipped by the
 # canvas top and collided with the title. Here both its edges - to Target
 # and to Disease - stay short and cross nothing.
 "Variant":          (46, 97, 4.2, NEW,    "Variant",             True),
 # Threaded into the one gap on the left: below AdverseEvent, above Route,
 # left of Identifier. x=2 hung the dashed ring off the canvas; x=13 put the
 # circle through Identifier. The ring needs r+0.9 of clearance on every side.
 "OrganClass":       (7,  33, 4.0, NEW,    "Organ\nClass",        True),
 "ClinicalTrial":    (84, 62, 5.6, CLIN,   "ClinicalTrial",       False),
 "AdverseEvent":     (9,  45, 5.0, SAFE,      "Adverse\nEvent",      False),
 # --- shared between the hubs ---
 "RegulatoryEvent":  (62, 46, 5.4, SAFE,      "Regulatory\nEvent",   False),
 "Identifier":       (18, 34, 4.4, GREY,   "Identifier",          False),
 # --- Product's neighbours: bottom half ---
 "Company":          (84, 46, 4.8, COMP,   "Company",             False),
 "Exclusivity":      (84, 30, 5.0, REG,       "Exclusivity",         False),
 "Route":            (7,  22, 3.4, GREY,   "Route",               False),
 "Patent":           (66, 8,  4.4, REG,       "Patent",              False),
 # Bottom right. Region has two edges that come from opposite ends -
 # IN_REGION down the right column from Country, APPROVED_IN across from
 # Product - so it sits where both arrive without crossing a node. That
 # cost moving Exclusivity out of the right column and Patent out of the
 # diagonal; both were sitting exactly on those two paths.
 "Region":           (94, 6,  3.6, GREY,   "Region",              False),
 "Approval":         (38, 2,  4.4, REG,    "Approval",            False),
 "RegulatoryAgency": (56, 4,  3.8, REG,    "Regulatory\nAgency",  False),
}

#      src              dst              label                  curve  new?
EDGES = [
 ("Product","Substance","CONTAINS",                   0.00, False),
 ("Company","Product","DEVELOPS",                     0.00, False),
 ("ClinicalTrial","Company","SPONSORED_BY",           0.00, False),
 ("ClinicalTrial","Disease","STUDIES",                0.00, False),
 ("ClinicalTrial","Country","CONDUCTED_IN",           0.00, False),
 ("Country","Region","IN_REGION",                   0.00, True),
 ("Variant","Target","VARIANT_IN",                  0.00, True),
 ("Variant","Disease","IMPLICATED_IN",              0.06, True),
 ("AdverseEvent","OrganClass","IN_ORGAN_CLASS",     0.00, True),
 ("Substance","ClinicalTrial","TESTED_IN",            0.00, False),
 ("Substance","Disease","INDICATED_FOR",              0.00, False),
 ("Substance","Target","TARGETS",                     0.00, False),
 ("Target","Disease","ASSOCIATED_WITH",               0.00, False),
 ("Substance","Mechanism","HAS_MECHANISM",            0.00, False),
 ("Substance","DrugClass","IN_CLASS",                 0.00, False),
 ("Substance","Modality","HAS_MODALITY",              0.06, False),
 ("Substance","Identifier","HAS_IDENTIFIER",         -0.10, False),
 ("Product","Identifier","HAS_IDENTIFIER",            0.10, False),
 ("Product","Route","HAS_ROUTE",                      0.00, False),
 ("Product","Region","APPROVED_IN",                   0.00, False),
 ("Product","Approval","HAS_APPROVAL",                0.00, False),
 ("Product","RegulatoryAgency","APPROVED_BY",        -0.14, False),
 ("Approval","RegulatoryAgency","ISSUED_BY",          0.00, False),
 # --- Phase 2 ---
 ("Substance","AdverseEvent","HAS_ADVERSE_EVENT",     0.00, False),
 ("Publication","Disease","ABOUT",                    0.00, False),
 ("Publication","Substance","MENTIONS",               0.00, False),
 ("Product","Patent","PROTECTED_BY",                  0.00, False),
 ("Product","Exclusivity","HAS_EXCLUSIVITY",          0.00, False),
 ("Product","RegulatoryEvent","SUBJECT_OF",           0.00, False),
 ("Substance","RegulatoryEvent","SUBJECT_OF",         0.08, False),
]


def edge_pts(a, b, shrink=1.0):
    x1, y1, r1, *_ = NODES[a]
    x2, y2, r2, *_ = NODES[b]
    dx, dy = x2 - x1, y2 - y1
    d = np.hypot(dx, dy) or 1
    ux, uy = dx / d, dy / d
    return (x1 + ux * r1 * shrink, y1 + uy * r1 * shrink), \
           (x2 - ux * r2 * shrink, y2 - uy * r2 * shrink)


# ---- edges ----
for a, b, label, curve, is_new in EDGES:
    p1, p2 = edge_pts(a, b)
    col = NEW if is_new else "#95a5a6"
    ax.add_patch(FancyArrowPatch(
        p1, p2, connectionstyle=f"arc3,rad={curve}",
        arrowstyle="-|>", mutation_scale=17,
        linewidth=2.0 if is_new else 1.4,
        color=col, alpha=0.95 if is_new else 0.75, zorder=1))
    mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
    nx, ny = -(p2[1] - p1[1]), (p2[0] - p1[0])
    n = np.hypot(nx, ny) or 1
    mx += nx / n * curve * 26
    my += ny / n * curve * 26
    ax.text(mx, my, label, fontsize=7.4, ha="center", va="center", zorder=3,
            color="#7d1f45" if is_new else "#2c3e50",
            fontweight="bold" if is_new else "normal",
            bbox=dict(boxstyle="round,pad=0.26", fc="white",
                      ec=NEW if is_new else "#d5d8dc", lw=0.9, alpha=0.97))

# Self-loops: an edge between two DIFFERENT nodes of the same label, never a
# node to itself (there are zero true self-loops in the data). Each is placed
# by angle rather than by named side, because the only thing that matters is
# picking a direction with no edge already running through it - Substance has
# eleven neighbours and almost every quadrant is taken.
#
# angle, loop radius, and how far out to push the label.
# Last field overrides the label position, as an offset from the loop centre.
# Placing a label further along the loop's own angle is fine until something
# else occupies that direction - SAME_STUDY_AS pointed straight into the
# IN_REGION edge running down the right column, so it goes under its circle
# instead, shifted left to clear that line.
for key, lbl, col, ang, out, lab in (
        # Up-left. Straight up puts the label through the subtitle; right is
        # where Region now sits.
        ("Disease",       "SUBTYPE_OF",    BIO,   40,  2.4, None),
        ("Product",       "BIOSIMILAR_OF", NEW,   135, 3.0, None),
        ("Substance",     "IS_SALT_OF",    NEW,   -58, 3.4, None),
        ("ClinicalTrial", "SAME_STUDY_AS", CLIN,  -30, 3.0, (-2.6, -5.4)),
        # Straight up. At 160 deg the loop ran off the left edge of the canvas
        # - DrugClass sits at x=6 and the circle needs 6.8 of clearance.
        ("DrugClass",     "IN_CLASS",      CLASS, 90,  2.6, None)):
    x, y, r, *_ = NODES[key]
    a = np.radians(ang)
    d = r + out
    cx, cy = x + d * np.cos(a), y + d * np.sin(a)
    if lab:
        tx, ty = cx + lab[0], cy + lab[1]
    else:
        # Past the far side of the loop (radius 3), not just past its centre,
        # or the label prints on top of the circle it names.
        tx, ty = x + (d + 6.8) * np.cos(a), y + (d + 6.8) * np.sin(a)
    ax.add_patch(Circle((cx, cy), 3.0, fill=False, ec=col, lw=1.9, zorder=1))
    ax.text(tx, ty, lbl, fontsize=7.4, ha="center", color=col, style="italic",
            fontweight="bold" if col == NEW else "normal")

# ---- nodes ----
for key, (x, y, r, col, label, is_new) in NODES.items():
    if is_new:
        ax.add_patch(Circle((x, y), r + 0.9, fc="none", ec=NEW, lw=2.2,
                            ls=(0, (4, 2)), zorder=2))
    ax.add_patch(Circle((x, y), r, fc=col, ec="white", lw=2.0, zorder=3))
    ax.text(x, y, label, fontsize=9.2 if r > 4.4 else 8.2, ha="center",
            va="center", color="white", fontweight="bold", zorder=4,
            linespacing=0.95)

ax.text(50, 108.0, "Biomedical Knowledge Graph — Phase 2 (extended)",
        fontsize=19, ha="center", fontweight="bold", color="#1b2631")
ax.text(50, 104.0,
        "22 entity types built  ·  32 relationship types      "
        "Phase 1 = 15 nodes / 19 edges  ·  additions shown in pink",
        fontsize=10.5, ha="center", color="#5d6d7e")

leg = [
 (NEW,  "Newest additions — Variant and the reaction hierarchy:"),
 (None, "    Variant          ← ClinVar variant_summary (~21.8M rows, NO header row) + COSMIC 40 files"),
 (None, "                          filtered: gene must be a drug target, significance must be a real call"),
 (None, "    VARIANT_IN       ← gene symbol -> UniProt, so no Gene node is needed"),
 (None, "    IMPLICATED_IN    ← ClinVar PhenotypeIDS MONDO ids, folded onto MeSH"),
 (None, "    OrganClass       ← vigiaccess System Organ Class. meddra.org has no terminology in it:"),
 (None, "                          the scrape reached only news pages, the vocabulary itself is licensed"),
 (LIT,  "Publication — europepmc / pubmed / biorxiv / medrxiv (openalex excluded: API budget exhausted)"),
 (None, "    ABOUT / MENTIONS ← exact dictionary match on TITLE only, never the abstract"),
]
y = -7.0
for col, txt in leg:
    if col:
        ax.add_patch(Circle((5.4, y + 0.35), 1.05, fc=col, ec="white", lw=1.2))
        ax.text(7.6, y + 0.35, txt, fontsize=9.2, va="center",
                color="#1b2631", fontweight="bold")
    else:
        ax.text(7.6, y + 0.35, txt, fontsize=8.4, va="center", color="#5d6d7e")
    y -= 2.4

plt.tight_layout()
out = "graph/schema_phase2.png"
plt.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
print("wrote", out)
