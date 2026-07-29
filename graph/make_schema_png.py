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

W, H = 21, 15.5
fig, ax = plt.subplots(figsize=(W, H), dpi=150)
ax.set_xlim(0, 100); ax.set_ylim(-24, 100); ax.axis("off")
fig.patch.set_facecolor("white")

CORE   = "#1f5fbf"   # Substance / Product
BIO    = "#c0392b"   # Disease
TARGET = "#7d3ac1"   # Target
CLIN   = "#0e7c8a"   # ClinicalTrial
COMP   = "#c8901a"   # Company
REG    = "#8a4b08"   # Agency / Approval
CLASS  = "#1a8a5a"   # Mechanism / DrugClass / Modality
GREY   = "#5d6d7e"   # Country / Region / Route / Identifier
NEW    = "#d13b6e"   # Phase-2 additions

#            x    y    r    colour  label                  new?
NODES = {
 "Substance":        (44, 56, 6.4, CORE,   "Substance",           False),
 "Product":          (44, 30, 5.6, CORE,   "Product",             False),
 "Disease":          (66, 80, 5.2, BIO,    "Disease",             False),
 "Target":           (38, 84, 4.8, TARGET, "Target",              False),
 "ClinicalTrial":    (72, 60, 5.6, CLIN,   "ClinicalTrial",       False),
 "Company":          (74, 34, 4.8, COMP,   "Company",             False),
 "Country":          (92, 72, 3.6, GREY,   "Country",             False),
 "Mechanism":        (18, 84, 4.2, CLASS,  "Mechanism",           False),
 "DrugClass":        (8,  72, 4.2, CLASS,  "DrugClass",           False),
 "Modality":         (5,  58, 3.4, CLASS,  "Modality",            False),
 "Route":            (9,  34, 3.4, GREY,   "Route",               False),
 "Identifier":       (20, 26, 4.4, GREY,   "Identifier",          False),
 "Region":           (22, 12, 3.6, GREY,   "Region",              False),
 "Approval":         (40, 10, 4.4, REG,    "Approval",            False),
 "RegulatoryAgency": (57, 10, 3.8, REG,    "Regulatory\nAgency",  False),
 # --- Phase 2 ---
 "AdverseEvent":     (16, 45, 5.0, NEW,    "Adverse\nEvent",      True),
 "Publication":      (86, 88, 4.6, NEW,    "Publication",         True),
 "RegulatoryEvent":  (57, 46, 5.4, NEW,    "Regulatory\nEvent",   True),
 "Patent":           (77,  9, 4.4, NEW,    "Patent",              True),
 "Exclusivity":      (93, 23, 5.0, NEW,    "Exclusivity",         True),
}

#      src              dst              label                  curve  new?
EDGES = [
 ("Product","Substance","CONTAINS",                   0.00, False),
 ("Company","Product","DEVELOPS",                     0.00, False),
 ("ClinicalTrial","Company","SPONSORED_BY",           0.12, False),
 ("ClinicalTrial","Disease","STUDIES",                0.00, False),
 ("ClinicalTrial","Country","CONDUCTED_IN",           0.00, False),
 ("Substance","ClinicalTrial","TESTED_IN",            0.00, False),
 ("Substance","Disease","INDICATED_FOR",             -0.14, False),
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
 ("Product","RegulatoryAgency","APPROVED_BY",         0.20, False),
 ("Approval","RegulatoryAgency","ISSUED_BY",          0.00, False),
 # --- Phase 2 ---
 ("Substance","AdverseEvent","HAS_ADVERSE_EVENT",     0.00, True),
 ("Publication","Disease","ABOUT",                    0.00, True),
 ("Publication","Substance","MENTIONS",               0.26, True),
 ("Product","Patent","PROTECTED_BY",                  0.10, True),
 ("Product","Exclusivity","HAS_EXCLUSIVITY",         -0.16, True),
 ("Product","RegulatoryEvent","SUBJECT_OF",           0.00, True),
 ("Substance","RegulatoryEvent","SUBJECT_OF",         0.14, True),
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

# Self-loops. Disease loops above; Product loops to its left, because below it
# the loop lands on the HAS_APPROVAL edge label.
for key, lbl, col, side in (("Disease", "SUBTYPE_OF", BIO, "up"),
                            ("Product", "BIOSIMILAR_OF", NEW, "left")):
    x, y, r, *_ = NODES[key]
    if side == "up":
        cx, cy, tx, ty = x - 1.0, y + r + 2.2, x - 1.0, y + r + 6.6
    else:
        cx, cy, tx, ty = x - r - 2.4, y, x - r - 3.0, y + 5.0
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

ax.text(50, 97.5, "Biomedical Knowledge Graph — Phase 2 (extended)",
        fontsize=19, ha="center", fontweight="bold", color="#1b2631")
ax.text(50, 94.0,
        "20 entity types  ·  27 relationship types      "
        "Phase 1 = 15 nodes / 19 edges  ·  additions shown in pink",
        fontsize=10.5, ha="center", color="#5d6d7e")

leg = [
 (NEW,  "Phase 2 — built from data already in the lake, none of it reachable today:"),
 (None, "    Patent           ← Orange Book patents_enriched (~16,344) + Purple Book patent_list (424)"),
 (None, "                          expiry dates, use codes, substance/product flags"),
 (None, "    Exclusivity      ← Orange Book exclusivity_enriched (2,265) + Purple Book exclusivity columns"),
 (None, "    RegulatoryEvent  ← EMA referrals/shortages/orphan designations, FDA recalls (~17,718), SFDA alerts"),
 (None, "    AdverseEvent     ← openFDA FAERS ~2.9M reports, aggregated to (substance, reaction) counts"),
 (None, "    Publication      ← europepmc / pubmed / openalex / biorxiv / medrxiv"),
 (None, "    BIOSIMILAR_OF    ← Purple Book license_type 351(k) + resolved_reference_bla"),
]
y = -2.0
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
