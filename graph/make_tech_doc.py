#!/usr/bin/env python3
"""Render GRAPH_TECHNICAL.pdf - how 49 independent sources become one graph.

    python graph/make_tech_doc.py

The document is about connection: how a name in one file becomes an edge to a
node created from another, what each join is worth, and where it can be wrong.
Companion to DATA_SOURCES.pdf, which inventories the files themselves.

Tables are generated from graph/sources.py and graph/emit.py rather than typed,
and EDGES below is asserted complete against EDGE_COLUMNS, so a relationship
added to the code cannot quietly go undocumented.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import sources                                       # noqa: E402
from emit import NODE_COLUMNS, EDGE_COLUMNS          # noqa: E402
from docgen import E, embed_png, page, render, table  # noqa: E402

HERE = pathlib.Path(__file__).parent
OUT_HTML = HERE / "GRAPH_TECHNICAL.html"
OUT_PDF = HERE / "GRAPH_TECHNICAL.pdf"

# --------------------------------------------------------------------------
# Every relationship: what it joins, out of which files, by what evidence.
# `method` is the value written to the edge's match_method column.
EDGES: dict[str, tuple[str, str, str, str]] = {
    "HAS_IDENTIFIER": (
        "any", "Identifier", "structured",
        "Every external id an entity carries. The one edge type reachable "
        "from all 22 labels."),
    "CONTAINS": (
        "Product", "Substance", "resolver",
        "The active ingredient of a marketed product, matched from the "
        "ingredient string each agency prints. Carries the resolver tier that "
        "found it, so a weak match is visible on the edge."),
    "DEVELOPS": (
        "Company", "Product", "structured",
        "Marketing authorisation holder, as stated by the agency."),
    "APPROVED_BY": (
        "Product", "RegulatoryAgency", "derived",
        "Which agency's register the product came out of. Derived rather than "
        "read - the file's location is the fact."),
    "APPROVED_IN": (
        "Product", "Region", "derived",
        "The agency's region, one hop pre-computed."),
    "HAS_APPROVAL": (
        "Product", "Approval", "structured",
        "The dated authorisation event, kept separate from the product so a "
        "product can carry several."),
    "ISSUED_BY": (
        "Approval | RegulatoryEvent", "RegulatoryAgency | Company",
        "structured | derived",
        "Who issued an approval, or who issued a recall."),
    "PROTECTED_BY": (
        "Product", "Patent", "structured",
        "Orange Book patent listings."),
    "HAS_EXCLUSIVITY": (
        "Product", "Exclusivity", "structured",
        "Orange Book and Purple Book market exclusivity."),
    "BIOSIMILAR_OF": (
        "Product", "Product", "structured",
        "Purple Book BLA reference relationship."),
    "HAS_ROUTE": (
        "Product", "Route", "structured",
        "Administration route, normalised by fold()."),
    "IN_CLASS": (
        "Substance | Product | DrugClass", "DrugClass",
        "resolver | structured",
        "ATC classification. Runs from a Substance, from a Product where the "
        "agency classified the product rather than the ingredient (28,579 of "
        "these, mostly Health Canada and EMA), and from a DrugClass to its "
        "own parent in the ATC tree."),
    "HAS_MODALITY": (
        "Substance", "Modality", "structured",
        "Small molecule, antibody, protein. Eleven values."),
    "IS_SALT_OF": (
        "Substance", "Substance", "structured",
        "Salt to parent. The reason a salt keeps its own node instead of "
        "being folded away."),
    "TARGETS": (
        "Substance", "Target", "symbol",
        "Drug to protein, from ChEMBL mechanisms and Open Targets, joined on "
        "gene symbol."),
    "HAS_MECHANISM": (
        "Substance", "Mechanism", "structured",
        "Mechanism of action as ChEMBL states it."),
    "INDICATED_FOR": (
        "Substance", "Disease", "structured",
        "Approved or investigated indication."),
    "ASSOCIATED_WITH": (
        "Target", "Disease", "structured",
        "Open Targets association, carrying their score."),
    "SUBTYPE_OF": (
        "Disease", "Disease", "structured",
        "MeSH tree hierarchy."),
    "SPONSORED_BY": (
        "ClinicalTrial", "Company", "structured",
        "Trial sponsor, normalised by norm_company()."),
    "STUDIES": (
        "ClinicalTrial", "Disease", "name | name_variant | vocab_alias | icd_name",
        "Condition studied, matched from the registry's free-text condition "
        "field. Four tiers, most reliable first: the condition as written is a "
        "MeSH heading or entry term; a rewriting reached MeSH (a stage "
        "qualifier stripped, a plural, 'cancer' for 'neoplasms'); NCIt or "
        "CDISC lists it as a synonym of a concept that reaches MeSH; or only "
        "an ICD title matched."),
    "TESTED_IN": (
        "Substance", "ClinicalTrial", "resolver",
        "Intervention, matched from the registry's free-text intervention "
        "field."),
    "CONDUCTED_IN": (
        "ClinicalTrial", "Country", "name",
        "Location, matched from free-text country names."),
    "SAME_STUDY_AS": (
        "ClinicalTrial", "ClinicalTrial", "structured",
        "The same study registered twice. Both nodes are kept."),
    "SUBJECT_OF": (
        "Substance", "RegulatoryEvent", "resolver",
        "Recall, shortage or safety communication naming the drug."),
    "HAS_ADVERSE_EVENT": (
        "Substance", "AdverseEvent", "aggregated",
        "FAERS reports, counted rather than listed: report, serious and death "
        "counts on the edge."),
    "IN_ORGAN_CLASS": (
        "AdverseEvent", "OrganClass", "structured",
        "MedDRA system organ class. Makes 'any cardiac event' one hop."),
    "VARIANT_IN": (
        "Variant", "Target", "symbol",
        "ClinVar and COSMIC variants to the gene they sit in."),
    "IMPLICATED_IN": (
        "Variant", "Disease", "structured",
        "Variant to condition, carrying clinical significance."),
    "ABOUT": (
        "Publication", "Disease", "name",
        "Paper to disease, matched on TITLE ONLY."),
    "MENTIONS": (
        "Publication", "Substance", "resolver",
        "Paper to drug, matched on TITLE ONLY."),
    "IN_REGION": (
        "Country", "Region", "structured",
        "189 countries into 9 regions."),
}

# The document must cover every relationship the code can write.
_missing = set(EDGE_COLUMNS) - set(EDGES)
assert not _missing, f"undocumented relationships: {sorted(_missing)}"
_extra = set(EDGES) - set(EDGE_COLUMNS)
assert not _extra, f"documented but not emittable: {sorted(_extra)}"

ORDER = [
    ("1", "load_atc", "Vocabularies",
     "WHO ATC tree, 1,318 rows. Loaded whole even for a one-drug slice, "
     "because any substance may point into it."),
    ("2", "load_gsrs", "Substance spine",
     "The naming authority. Builds the resolver every later loader matches "
     "names through, so nothing that needs a name can run before it."),
    ("3", "r.finalise()", "Resolver finalised",
     "The stereo tier is built only once every name is known, so a key that "
     "two different substances both reduce to can be blocked rather than "
     "merged."),
    ("4", "load_chembl_molecules", "ChEMBL molecules",
     "2.9M molecules, plus the molregno -> node-key map every later ChEMBL "
     "file needs."),
    ("5", "load_chembl_synonyms", "ChEMBL synonyms",
     "Brand names and research codes, registered as the alias tier - "
     "consulted only after all three identifier tiers miss."),
    ("6", "load_structures", "Structures",
     "InChIKey. Chemistry rather than a name, so the strongest merge signal "
     "available."),
    ("7", "reference.ALL", "Reference tables",
     "ATC level 5, salt/parent hierarchy, RXCUI, SPL setids."),
    ("8", "load_targets", "Targets",
     "Keyed by UniProt accession where one exists; the tid -> key map is held "
     "for mechanisms."),
    ("9", "load_mechanisms", "Mechanisms",
     "One file yields both TARGETS and HAS_MECHANISM as stated fact."),
    ("10", "disease.ALL", "Disease",
     "MeSH is the spine; chembl_indications carries the MeSH/EFO crosswalk "
     "that later loaders fold MONDO ids onto. Must precede anything matching "
     "disease prose."),
    ("11", "products.ALL", "Products",
     "Ten agencies. Also creates all 189 Country and 9 Region nodes."),
    ("12", "trials.ALL", "Trials",
     "Nine registries, WHO last so native records win on properties."),
    ("13", "safety.ALL", "Safety",
     "Regulatory events, FAERS aggregation, VigiAccess organ classes."),
    ("14", "variants.ALL", "Variants",
     "Needs symbol_target from HGNC and efo_mesh from chembl_indications."),
    ("15", "literature.ALL", "Literature",
     "Needs a finalised resolver and mesh_by_name to match titles against."),
]


def main():
    p = ["<h1>How the data connects</h1>",
         "<p class=sub>Graph technical reference &mdash; identity, resolution, "
         "every relationship, and the embedding layer on top.</p>",
         f"<p class=meta>{len(NODE_COLUMNS)} node labels &middot; "
         f"{len(EDGE_COLUMNS)} relationship types &middot; "
         f"{len(sources.INCLUDED)} declarations over 96 files &middot; "
         f"13,676,986 nodes / 16,830,561 relationships</p>"]

    # ------------------------------------------------------------------ 1
    p.append("<h2>1. The problem</h2>")
    p.append("<p>Forty-nine sources, none of which were built to be joined. "
             "The FDA calls a drug <code>ATORVASTATIN CALCIUM</code>, Health "
             "Canada calls it <code>Atorvastatin calcium (as trihydrate)</code>, "
             "a trial registry writes <code>Lipitor 40mg</code>, and ChEMBL "
             "refers to <code>CHEMBL1487</code>. None of these strings match, "
             "and there is no identifier common to all four.</p>")
    p.append("<p>So the whole graph rests on one question: <b>given a name, "
             "which substance is it?</b> Everything else &mdash; products, "
             "trials, adverse events, papers &mdash; hangs off the answer. "
             "Sections 3 and 4 are that machinery; the rest is what gets built "
             "once it works.</p>")

    p.append("<h3>Files, not a database</h3>")
    p.append("<p>Every loader writes CSV. A validator reads those CSVs. Only "
             "a build that passes is imported. That ordering is the core of "
             "the design: a bulk import replaces the store outright with no "
             "transaction, so an unchecked build silently <i>becomes</i> the "
             "live graph, and the failure mode is a confident wrong answer "
             "rather than an error.</p>")
    p.append("<pre>S3 (moine-data)\n"
             "   |  77 declarations -> 96 CSV files\n"
             "   v\n"
             "build.py            stream, resolve names, emit nodes/edges\n"
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
    p.append("<p>Nothing in the build talks to Neo4j. The CSVs a human can "
             "inspect are exactly the ones imported, so every claim about the "
             "graph is a claim about a table, checkable without "
             "infrastructure. The validator caught six structural bugs that "
             "way, including the key collision in section 2.</p>")

    # ------------------------------------------------------------------ 2
    p.append("<h2>2. Identity: how a node gets its key</h2>")
    p.append("<p>Every key is <code>NAMESPACE:VALUE</code> and globally "
             "unique &mdash; not unique per label, globally. "
             "<code>neo4j-admin import</code> resolves every endpoint in a "
             "single id space, so <b>two labels sharing a key become one "
             "node</b>, silently, with no error.</p>")
    p.append("<div class=warn><b>This happened.</b> <code>MESH:D000544</code> "
             "was the key of a Disease and also of an Identifier node "
             "recording that same MeSH id. On import they would have merged "
             "into a single node that was half disease, half identifier. The "
             "fix is that every Identifier is created through one function "
             "which prefixes its key with <code>ID:</code>, giving "
             "<code>ID:MESH:D000544</code>. Nothing else may create one.</div>")
    p.append(table(
        ["namespace", "label", "value"],
        [[f"<code>{E(a)}</code>", E(b), E(c)] for a, b, c in [
            ("UNII:", "Substance", "FDA UNII &mdash; the preferred key"),
            ("CHEMBL:", "Substance", "molregno, when no UNII is known"),
            ("NAME:", "Substance", "provisional &mdash; the name never resolved"),
            ("ATC:", "DrugClass", "WHO ATC code, levels 1-5"),
            ("MESH:", "Disease", "MeSH descriptor id"),
            ("UNIPROT: / CHEMBL_TARGET:", "Target", "accession, else ChEMBL tid"),
            ("MECH:", "Mechanism", "folded mechanism text"),
            ("NCT: EUCTR: CTIS: ...", "ClinicalTrial", "registry id, 22 registries"),
            ("FDA: EMA: MHRA: CA: PMDA: SFDA:", "Product", "agency's own product id"),
            ("APPROVAL: EVENT: PATENT: EXCL:", "Approval etc.", "composite of agency and id"),
            ("COMPANY:", "Company", "normalised company name"),
            ("COUNTRY: / REGION:", "Country / Region", "ISO-3166 alpha-2, folded region"),
            ("AE: / SOC:", "AdverseEvent / OrganClass", "MedDRA preferred term, organ class"),
            ("CLINVAR: / COSMIC:", "Variant", "variation id"),
            ("PMID: / DOI:", "Publication", "PubMed id, else DOI"),
            ("ID:", "Identifier", "<b>prefixed</b> &mdash; see above"),
        ]]))

    # ------------------------------------------------------------------ 3
    p.append("<h2>3. The resolver</h2>")
    p.append("<p>One object, built during load 2 and shared by every loader "
             "afterwards. It answers <code>resolve(name) -&gt; Match(key, "
             "method)</code> and <b>never returns nothing</b>.</p>")

    p.append("<h3>3.1 Normalisation</h3>")
    p.append(table(
        ["function", "does", "example"],
        [[f"<code>{E(a)}</code>", E(b), f"<code>{E(c)}</code>"] for a, b, c in [
            ("fold(s)", "lowercase, strip punctuation, collapse whitespace, "
                        "hyphens to spaces",
             "ATORVASTATIN CALCIUM -> atorvastatin calcium"),
            ("strip_salts(s)", "remove salt and hydrate suffixes",
             "atorvastatin calcium trihydrate -> atorvastatin"),
            ("strip_stereo(s)", "remove stereochemical prefixes",
             "R-salbutamol -> salbutamol"),
            ("norm_company(s)", "drop Inc, Ltd, GmbH, Pharmaceuticals",
             "Pfizer Inc. -> pfizer"),
        ]]))
    p.append("<div class=note><code>fold()</code> turning hyphens into spaces "
             "is load-bearing and has bitten once: a literal filter set "
             "containing <code>\"0-unassigned\"</code> never matched, because "
             "by the time the value reached it the string was "
             "<code>\"0 unassigned\"</code>. Fold both sides of any "
             "comparison.</div>")

    p.append("<h3>3.2 The tiers</h3>")
    p.append("<p>Four tables, tried in order. Separate tables rather than one, "
             "so a hit records <i>which</i> tier found it &mdash; that tier "
             "name is written onto every edge as "
             "<code>match_method</code>.</p>")
    p.append(table(
        ["#", "tier", "matches on", "method", "confidence"],
        [[E(a), f"<code>{E(b)}</code>", E(c), f"<code>{E(d)}</code>", E(f)]
         for a, b, c, d, f in [
            ("1", "exact", "fold(name) -> UNII", "unii", "highest"),
            ("2", "salt", "strip_salts(name) -> UNII", "salt", "high"),
            ("3", "stereo", "strip_stereo(name) -> UNII", "stereo",
             "high, and guarded &mdash; see 3.3"),
            ("4", "alias", "ChEMBL synonyms and brand names -> node key",
             "synonym", "good"),
            ("&mdash;", "miss", "nothing matched", "provisional",
             "<b>weak</b> &mdash; see 3.4"),
        ]]))
    p.append("<p>Within each table the <b>first writer wins</b>. Loading order "
             "is therefore an authority ranking: gsrs preferred names, then "
             "gsrs synonyms, then ChEMBL. A gsrs name always beats a ChEMBL "
             "research code for the same string.</p>")
    p.append("<p>When two substances genuinely share a name, the first is kept "
             "and the collision is <i>recorded</i> rather than overwritten. "
             "Overwriting would make the result depend on file order.</p>")

    p.append("<h3>3.3 Why the stereo tier is deferred</h3>")
    p.append("<p>It cannot be built as names arrive, because two things must "
             "be true at once:</p>")
    p.append("<pre>* The table must hold the PLAIN form. The prefix is usually on the\n"
             "  QUERY, not the registered name: gsrs registers \"Salbutamol\",\n"
             "  a source writes \"R-Salbutamol\". So salbutamol -> UNII must exist.\n"
             "\n"
             "* That same entry must not let \"Levo-cetirizine\" reach cetirizine's\n"
             "  UNII - they are different drugs. Whether it would depends on\n"
             "  whether levocetirizine is separately registered, which is not\n"
             "  known until every name has been loaded.</pre>")
    p.append("<p>So <code>finalise()</code> proposes an entry for every name, "
             "then <b>withdraws</b> any whose stripped key could bridge two "
             "distinct substances. Conflict detection uses a looser pattern "
             "than the matcher, so run-together spellings "
             "(<code>levocetirizine</code>) are caught even though the strict "
             "matcher leaves them alone. Calling <code>resolve()</code> before "
             "<code>finalise()</code> triggers it automatically.</p>")

    p.append("<h3>3.4 Provisional keys, and the merge that went wrong</h3>")
    p.append("<p>A name that matches nothing still gets a node: "
             "<code>NAME:&lt;folded&gt;</code>, method "
             "<code>provisional</code>. Discarding the row instead would throw "
             "away a real product or trial because its ingredient string was "
             "unusual.</p>")
    p.append("<div class=warn><b>The bug this caused.</b> Provisional nodes "
             "were originally merged into ChEMBL molecules by name. ChEMBL "
             "contains descriptive names, so <code>NAME:platinum complex</code> "
             "matched 248 distinct molecules and absorbed them into one node; "
             "across the build, 22,125 provisional nodes swallowed 45,891 "
             "molecules. Merging is now conditional on the ChEMBL molecule "
             "having <i>resolved</i> to a real identifier.</div>")
    p.append("<p>Provisional nodes remain, correctly: they hold rows whose "
             "drug is genuinely unidentifiable from the text given. Treat any "
             "edge marked <code>provisional</code> as a hint.</p>")

    # ------------------------------------------------------------------ 4
    p.append("<h2>4. Load order is a dependency chain</h2>")
    p.append("<p>Order is not cosmetic. Each stage fills dictionaries the next "
             "one reads, and running them out of order does not fail &mdash; "
             "it silently produces fewer edges.</p>")
    p.append(table(["#", "call", "stage", "why here"],
                   [[E(a), f"<code>{E(b)}</code>", f"<b>{E(c)}</b>", E(d)]
                    for a, b, c, d in ORDER]))

    # ------------------------------------------------------------------ 5
    p.append("<h2>5. Every relationship</h2>")
    p.append(f"<p>All {len(EDGE_COLUMNS)} of them. This table is asserted "
             "complete against the code at generation time, so a relationship "
             "added without a description here fails the build of this "
             "document.</p>")
    p.append(table(
        ["relationship", "from", "to", "match_method", "what it means"],
        [[f"<code>{E(k)}</code>", E(v[0]), E(v[1]), f"<code>{E(v[2])}</code>",
          v[3]] for k, v in sorted(EDGES.items())]))

    p.append("<h3>5.1 What match_method is worth</h3>")
    p.append("<p>Every edge carries it. It is the single most useful property "
             "for judging whether to trust an answer.</p>")
    p.append(table(
        ["value", "meaning", "trust"],
        [[f"<code>{E(a)}</code>", E(b), c] for a, b, c in [
            ("structured", "the source stated the relationship outright",
             "<b>high</b>"),
            ("unii / salt / stereo", "resolved through an identifier tier",
             "<b>high</b>"),
            ("symbol", "joined on a gene symbol", "good"),
            ("derived", "computed from the file's own location or agency",
             "high &mdash; but it is our inference, not the source's"),
            ("aggregated", "counted across many reports, not one fact",
             "high in aggregate, meaningless per-report"),
            ("synonym", "matched via a brand name or research code", "good"),
            ("ncit_oncology", "matched via an NCIt antineoplastic synonym",
             "good"),
            ("name", "free prose matched against a dictionary",
             "<b>hint only</b>"),
            ("name_variant", "a rewriting of the prose reached the dictionary",
             "<b>weaker than name</b>"),
            ("vocab_alias", "NCIt or CDISC names it as a synonym of a concept "
             "that reaches MeSH", "<b>weaker than name</b>"),
            ("icd_name", "no MeSH form matched, an ICD title did",
             "<b>weakest</b>"),
            ("provisional", "the name never resolved", "<b>weak</b>"),
        ]]))
    p.append("<div class=note><code>CONDUCTED_IN</code>, <code>STUDIES</code> "
             "and <code>ABOUT</code> rest entirely on matching text &mdash; "
             "<b>not one trial-to-disease edge in this graph is structured</b>. "
             "About 84% are exact <code>name</code> matches and the rest come "
             "from the three weaker tiers. They are sound for aggregate "
             "questions (<i>how many trials in the Gulf</i>) and should not be "
             "cited as fact about one specific trial.<br><br>"
             "Measured against ClinicalTrials.gov itself, over 15 conditions "
             "and restricted to the trials taken from that registry, recall is "
             "<b>75%</b> &mdash; so a count from this graph is a floor, not a "
             "total.</div>")

    # ------------------------------------------------------------------ 6
    p.append("<h2>6. The joins in detail</h2>")

    p.append("<h3>6.1 Products, and the identifier that did not join</h3>")
    p.append("<p>Ten agency registers, each with its own product id, each "
             "naming ingredients as free text. Every product's ingredient "
             "string goes through the resolver; the tier that succeeds becomes "
             "<code>CONTAINS.match_method</code>. That single join is what "
             "makes <i>where is this drug approved</i> answerable across ten "
             "registers that share no key.</p>")
    p.append("<div class=warn><b>DailyMed's RXCUI looked like a free join and "
             "was not.</b> Both DailyMed and RxNorm publish RXCUI, so joining "
             "on it seemed obvious. Overlap was zero: DailyMed publishes "
             "<i>product-level</i> RXCUIs (SCD - a specific clinical drug, "
             "\"atorvastatin 40 MG oral tablet\") while the substance side "
             "carries <i>ingredient-level</i> ones (IN). Same column name, "
             "same id space, different granularity &mdash; and the symptom was "
             "not an error but an empty result. It now joins on "
             "<code>rxstring</code> through the resolver.</div>")

    p.append("<h3>6.2 Trials across 22 registries</h3>")
    p.append("<p>Registry ids are canonicalised to "
             "<code>NAMESPACE:VALUE</code>. The values look doubled &mdash; "
             "<code>NCT:NCT01045135</code>, <code>CTIS:CTIS2024-511</code> "
             "&mdash; because 19 of the 22 registries embed their own prefix "
             "in the id itself. Stripping it would make the key ambiguous "
             "against registries that do not.</p>")
    p.append("<p>WHO ICTRP mirrors other registries. It is loaded <b>last</b>, "
             "and merges into the native node rather than creating its own, so "
             "the native record wins on every property. Where two registries "
             "hold genuinely separate registrations of one study, both nodes "
             "are kept and joined by <code>SAME_STUDY_AS</code> &mdash; "
             "collapsing them would lose whichever fields only one side "
             "recorded.</p>")

    p.append("<h3>6.3 Disease, and the crosswalk</h3>")
    p.append("<p>MeSH is the spine. Other sources speak EFO, MONDO or plain "
             "prose. <code>chembl_indications</code> carries a MeSH/EFO pair "
             "per row, and that is the crosswalk every later loader folds its "
             "own ids onto &mdash; which is why disease must load before "
             "anything that matches disease text.</p>")
    p.append("<p>Free-text conditions from trial registries are matched "
             "against the MeSH name and synonym dictionary. Synonyms matter "
             "more than names here: the descriptor is <i>Carcinoma, "
             "Non-Small-Cell Lung</i> and no human writes that.</p>")

    p.append("<h3>6.4 Safety: counted, not listed</h3>")
    p.append("<p>FAERS is tens of millions of individual reports. Storing one "
             "edge per report would add more relationships than the rest of "
             "the graph holds and answer no question better. Reports are "
             "aggregated per substance-reaction pair, with report, serious and "
             "death counts on the edge, method <code>aggregated</code>.</p>")
    p.append("<p><code>IN_ORGAN_CLASS</code> then hangs each reaction under a "
             "MedDRA system organ class, so <i>any cardiac event</i> is one "
             "hop rather than an enumeration of every cardiac term.</p>")

    p.append("<h3>6.5 Variants</h3>")
    p.append("<p>ClinVar is ~21.8M rows and <b>has no header</b> &mdash; the "
             "first line is data, so columns are read by position. Only the "
             "first 34 of 43 are positionally stable across releases. Filtered "
             "hard on load: the gene must be a known drug target and the "
             "clinical significance must be an actual call, not "
             "<i>uncertain</i> or <i>conflicting</i>. 937,377 survive.</p>")
    p.append("<p>This is what makes mutation &rarr; protein &rarr; drug a "
             "traversal: <code>VARIANT_IN</code> joins on gene symbol, "
             "<code>TARGETS</code> already joins drugs to those proteins.</p>")

    p.append("<h3>6.6 Literature: title only</h3>")
    p.append("<p>Publications are matched to diseases and drugs on their "
             "<b>title only, never the abstract</b>. An abstract mentions "
             "every drug in a comparison, every disease in a differential, and "
             "every compound in a screen. Matching on abstracts produces "
             "enormous numbers of edges that each mean nothing more than 'this "
             "word appeared'. The title is a claim about what the paper is "
             "about.</p>")

    p.append("<h3>6.7 Provenance on every row</h3>")
    p.append("<p>Every node and edge carries which source file produced it. "
             "Sources are written as short ids resolved through a side table "
             "&mdash; full S3 keys repeated across 30M rows cost 2.2 GB of "
             "pure repetition.</p>")

    # ------------------------------------------------------------------ 7
    p.append("<h2>7. Validation</h2>")
    p.append("<p>Exit non-zero here and no import happens. Checks:</p>")
    p.append("<pre>referential integrity  every edge endpoint exists as a node\n"
             "key uniqueness        no key used by two labels (section 2)\n"
             "resolution quality    provisional share within bounds per source\n"
             "fan-out outliers      a node absorbing implausibly many edges\n"
             "source coverage       every declared file actually read\n"
             "biology fixtures      atorvastatin -> HMG-CoA reductase\n"
             "                      pembrolizumab -> PD-1\n"
             "                      erenumab -> CGRP receptor</pre>")
    p.append("<p>Fan-out is what caught the platinum-complex merge; source "
             "coverage caught five declared files that were never loaded. "
             "Fixtures are skipped on a <code>--slice</code> build, since "
             "pembrolizumab cannot pass on an atorvastatin slice, and enforced "
             "on every full build.</p>")
    p.append("<div class=note>The validator holds <b>keys and counters, not "
             "rows</b>. It once held every row as a dict: 11.5M of them beside "
             "Neo4j's 8 GB drove the host into swap and dropped the SSH "
             "session mid-import.</div>")

    # ------------------------------------------------------------------ 8
    p.append("<h2>8. Import</h2>")
    p.append("<p><code>stage_for_neo4j.py</code> runs first: strips header "
             "rows, collapses embedded newlines, and blanks values that do not "
             "match their declared type. It exists because three separate "
             "imports failed on it &mdash; the last, an <code>enrollment</code> "
             "column where 131,158 ChiCTR rows hold prose rather than an "
             "integer.</p>")
    p.append("<p>Types are declared in headers because untyped means string, "
             "and a string comparison is silent: without <code>:int</code> on "
             "<code>report_count</code>, <code>WHERE e.report_count &gt; 100</code> "
             "sorts <code>9</code> above <code>100</code> and raises "
             "nothing.</p>")
    p.append("<div class=warn>Import runs with <code>--bad-tolerance=0</code>. "
             "The validator has already proved every endpoint resolves, so a "
             "bad relationship at this point means the files changed between "
             "validating and importing &mdash; and a partial import that drops "
             "edges silently is worse than a failed one. The store is dumped "
             "first; that dump is the only way back.</div>")

    # ------------------------------------------------------------------ 9
    p.append("<h2>9. Schema</h2>")
    p.append(embed_png(HERE / "schema_phase2.png"))
    rows = [[f"<code>{E(l)}</code>", str(len(c)),
             ", ".join(f"<code>{E(x)}</code>" for x in c)]
            for l, c in sorted(NODE_COLUMNS.items())]
    p.append(table(["label", "#", "properties (before provenance)"], rows))

    # ------------------------------------------------------------------ 10
    p.append("<h2>10. Embedding</h2>")
    p.append("<p>The last layer, and the one that decides whether a question "
             "finds its starting node at all. Everything above connects nodes "
             "to each other; this connects <i>a question</i> to a node.</p>")

    p.append("<h3>10.1 Why full-text was not enough</h3>")
    p.append("<p>Three full-text indexes cover names, titles and reaction "
             "terms, and they handle exact and near-exact strings well. They "
             "cannot bridge a query to a name it shares no characters with:</p>")
    p.append(table(
        ["query", "full-text", "the node it should find", "embedding score"],
        [[f"<code>{E(a)}</code>", b, E(c), f"<b>{E(d)}</b>"]
         for a, b, c, d in [
            ("NSCLC", "<i>nothing</i>", "Non-small cell lung cancer", "0.832"),
            ("statins", "<i>nothing</i>", "HMG-CoA reductase inhibitor", "0.737"),
            ("COPD", "<i>nothing</i>",
             "Chronic obstructive pulmonary disease", "0.803"),
            ("heart problems", "<i>nothing</i>", "heart disorder", "0.873"),
        ]]))

    p.append("<h3>10.2 Which model, and how that was decided</h3>")
    p.append("<p>Measured, not assumed. <b>SapBERT</b> "
             "(<code>cambridgeltl/SapBERT-from-PubMedBERT-fulltext</code>, "
             "768-d, [CLS] pooling) was compared against <b>bge-m3</b> "
             "(1024-d, already in use for documents) on the same probes.</p>")
    p.append(table(
        ["", "SapBERT", "bge-m3"],
        [["probes correct", "<b>6 / 6</b>", "5 / 6"],
         ["score range on correct answers", "<b>0.74 - 0.87</b>", "0.43 - 0.61"]]))
    p.append("<p>The score range matters more than the hit count. SapBERT is "
             "trained on biomedical entity linking &mdash; on exactly this "
             "task, aligning a surface form to a concept &mdash; so its "
             "correct answers sit far from its wrong ones and a threshold is "
             "meaningful. bge-m3's correct answers score close to its "
             "incorrect ones, which leaves no way to reject a bad match.</p>")
    p.append("<div class=note>The two models coexist on purpose. Documents "
             "stay on <b>bge-m3</b> in Qdrant (1024-d): passage retrieval is a "
             "different task from entity linking, and re-embedding 3.24M "
             "chunks to unify them would cost hours of GPU to make retrieval "
             "worse.</div>")

    p.append("<h3>10.3 What is embedded, and what is not</h3>")
    p.append(table(
        ["label", "nodes", "why"],
        [[f"<code>{E(a)}</code>", E(b), E(c)] for a, b, c in [
            ("Disease", "24,488",
             "questions arrive as acronyms and lay phrasing"),
            ("DrugClass", "6,996",
             "'statins' is a class nobody spells as an ATC name"),
            ("AdverseEvent", "6,981",
             "MedDRA terms are clinical; complaints are not"),
            ("Mechanism", "1,967", "described in prose that varies freely"),
        ]]))
    p.append("<p><b>40,432 nodes total.</b> Substance, Product, Target, "
             "Company and ClinicalTrial are deliberately excluded: those are "
             "matched by identifier or exact name, where full-text already "
             "wins and an embedding would introduce plausible wrong "
             "answers. Embedding 3.07M substances would also cost far more "
             "than it returns &mdash; 93% of them have no name at all.</p>")

    p.append("<h3>10.4 Using it</h3>")
    p.append("<pre>python graph/embed_entities.py --dir ~/graph-runs/&lt;ts&gt; \\\n"
             "                               --query \"NSCLC\"</pre>")
    p.append("<div class=warn><b>Reject matches below 0.60.</b> Correct "
             "answers measured 0.74-0.87 and the best wrong answer sat well "
             "below 0.60, so the gap is real. Use it &mdash; an embedding "
             "always returns a nearest neighbour, and without a floor the "
             "nearest neighbour to a nonsense query is presented with the same "
             "confidence as a correct one.</div>")
    p.append("<p>Order of preference when finding a starting node: an exact "
             "identifier if you have one, then full-text with a label filter, "
             "then embedding for acronyms and paraphrase. Full detail in "
             "<code>graph/AGENT_QUERY_GUIDE.md</code>.</p>")

    render(page("How the data connects", "".join(p)), OUT_HTML, OUT_PDF)


if __name__ == "__main__":
    main()
