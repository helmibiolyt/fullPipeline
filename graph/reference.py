"""The reference tables that hang identity and classification off Substance.

Five files declared in sources.py had no loader, and the gap was invisible in
the totals because nothing counts a relationship that was never emitted. What
it cost:

  * IN_CLASS had no Substance source at all - only Product. "What ATC class is
    atorvastatin in" was unanswerable on a graph whose whole point is drugs.
  * RXCUI and SPL_SETID were absent, so nothing joined to DailyMed, which is
    the bridge from a substance to the label documents in the vector store.
  * Salt handling rested entirely on strip_salts(), a heuristic, while ChEMBL
    states parent/salt as a fact in molecule_hierarchy.

Loaded after the substance spine, because every one of these attaches to
Substance nodes that must already exist.
"""
from __future__ import annotations

import re

import lake
from normalise import fold

# Where the ingredient name stops and the strength begins: the first bare
# number, or a number attached to a unit. "ethanol 0.62 ML/ML Topical Gel".
_STRENGTH = re.compile(r"\s+\d")

L = {
    "atc_subs":  "Drug_Substance_Reference/atcddd.fhi.no/atc_ddd_data/atc_substances.csv",
    "hierarchy": "Drug_Substance_Reference/ebi.ac.uk-chembl/chembl_data/chembl_molecule_hierarchy.csv",
    "usan":      "Drug_Substance_Reference/ebi.ac.uk-chembl/chembl_data/chembl_usan_stems.csv",
    "rxnorm":    "Drug_Substance_Reference/rxnav.nlm.nih.gov/rxnorm_data/rxnorm_drugs.csv",
    "dailymed":  "Drug_Substance_Reference/dailymed.nlm.nih.gov/dailymed_data/dailymed_master_mapping.csv",
}


def load_atc_substances(b):
    """Substance -> DrugClass, the edge the graph was missing.

    The WHO file is keyed by ATC code with the substance as a plain name, so
    this is a dictionary match, not a join - recorded as such on the edge. Only
    confident matches are taken: an unresolved name here would mint a
    provisional substance whose only fact is a class, which is worse than no
    edge because it looks like a drug.
    """
    t0 = b._step("atc_substances")
    key = L["atc_subs"]
    n = hit = 0
    for row in lake.stream_csv(key, limit=b.limit):
        code = (row.get("atc_code") or "").strip()
        name = (row.get("name") or "").strip()
        if not code or not name:
            continue
        n += 1
        # ATC has five levels and atc_classes.csv carries only the first four.
        # Level 5 - the chemical substance, C10AA05 for atorvastatin - lives
        # here, so these codes are created rather than looked up. Gating them
        # against atc_codes rejected the entire file.
        parent = (row.get("parent_code") or "").strip()
        b.w.node("DrugClass", f"ATC:{code}", source=key, atc_code=code,
                 name=name, level="5", vocabulary="ATC")
        b.atc_codes.add(code)
        if parent in b.atc_codes:
            b.w.edge("IN_CLASS", f"ATC:{code}", f"ATC:{parent}", source=key)

        m = b.r.resolve(name)
        if not m.key or not m.resolved:
            continue
        hit += 1
        b.w.edge("IN_CLASS", m.key, f"ATC:{code}", match_method=m.method,
                 source=key)
        b.w.identifier(m.key, "ATC", code, source=key, match_method=m.method)
    b.stats["atc_substances_matched"] = f"{hit}/{n}"
    b._done("atc_substances", t0, n)


def load_hierarchy(b):
    """ChEMBL's own salt -> parent statement.

    strip_salts() guesses this from trailing tokens; this file states it. Where
    a salt and its parent are both loaded and are different nodes, the salt's
    identifiers are attached to the parent too, so a lookup by the salt's
    InChIKey reaches the parent molecule.

    3.5M rows, but only the ~5% where molregno != parent_molregno do anything.
    """
    t0 = b._step("chembl_hierarchy")
    key = L["hierarchy"]
    n = 0
    for row in lake.stream_csv(key, limit=b.limit):
        mol = (row.get("molregno") or "").strip()
        par = (row.get("parent_molregno") or "").strip()
        if not mol or not par or mol == par:
            continue
        skey, pkey = b.molregno_key.get(mol), b.molregno_key.get(par)
        if not skey or not pkey or skey == pkey:
            continue
        n += 1
        # IS_SALT_OF rather than folding the salt into the parent: a product
        # contains the salt, and the strength on the label is the salt's. Two
        # nodes with a stated edge keeps both facts.
        b.w.edge("IS_SALT_OF", skey, pkey, match_method="structured", source=key)
        # Held so later loaders can attach a salt's pharmacology to its parent
        # as well. ChEMBL and FAERS annotate whichever form the source named,
        # so without this the parent - the name everyone actually asks about -
        # carries nothing. See Build.with_parent.
        b.salt_parent[skey] = pkey
    b.stats["salt_parent_pairs"] = len(b.salt_parent)
    b._done("chembl_hierarchy", t0, n)


# load_usan_stems was removed rather than repaired.
#
# It inferred a substance's Modality from its name suffix, which was wrong
# twice over. First, the annotations are not one concept: "-mab" is
# "monoclonal antibodies" (modality), "-kinra" is "interleukin receptor
# antagonists" (mechanism), "-prazole" is a pharmacologic class. Routing them
# all to any single label misfiles two thirds of them - as Modality it put 397
# class descriptions next to 9 real modalities.
#
# Second, and worse, a stem has several rows with sub-variants: "-mab" appears
# as "monoclonal antibodies", "...: fully human", "...: chimeric" and
# "...: humanized". A name suffix cannot distinguish those, so matching the
# first row assigned "chimeric" to humanized antibodies by list order. That is
# invented precision, which is worse than no annotation.
#
# Everything it approximated is already in the graph, stated rather than
# inferred: Modality from ChEMBL molecule_type, DrugClass from WHO ATC,
# Mechanism from chembl_mechanisms. Revisit only with the USAN stem table
# hand-classified into those three, which is a curation task, not a load.


def load_rxnorm(b):
    """RXCUI - the identifier US prescribing systems speak, and the join to
    DailyMed."""
    t0 = b._step("rxnorm")
    key = L["rxnorm"]
    n = 0
    for row in lake.stream_csv(key, limit=b.limit):
        rxcui = (row.get("ingredient_rxcui") or row.get("rxcui") or "").strip()
        name = (row.get("ingredient_name") or row.get("name") or "").strip()
        if not rxcui or not name:
            continue
        m = b.r.resolve(name)
        if not m.key or not m.resolved:
            continue
        n += 1
        b.w.identifier(m.key, "RXCUI", rxcui, source=key, match_method=m.method)
        b.rxcui_key.setdefault(rxcui, m.key)
    b._done("rxnorm", t0, n)


def load_dailymed(b):
    """SPL setid - the join from a substance to its label documents.

    This is the one edge that connects the graph to the vector store: an SPL
    setid identifies the same document that was chunked and embedded, so
    "find the label text for this drug" becomes a graph lookup followed by a
    filtered vector search rather than a name guess.

    NOT joined on RXCUI, though both files have that column - they are two
    different RXNorm namespaces and never overlap. DailyMed's rxcui is
    product-level (SCD/PSN: "chloroprocaine hydrochloride 10 MG/ML Injectable
    Solution" = 992801) while rxnorm_drugs.csv is entirely tty=IN, the
    ingredient concept, which carries a different code. Joining them produced
    exactly zero matches, and read as missing data rather than as the wrong
    key.

    So the join is on rxstring instead, which begins with the ingredient and
    then states strength and form. Cutting at the first number leaves the name,
    which the resolver handles - "chloroprocaine hydrochloride" reaches
    chloroprocaine through the salt tier. That is a name match and is recorded
    as one on the edge.
    """
    t0 = b._step("dailymed")
    key = L["dailymed"]
    n = 0
    for row in lake.stream_csv(key, limit=b.limit):
        setid = (row.get("setid") or "").strip()
        rxs = (row.get("rxstring") or "").strip()
        if not setid or not rxs:
            continue
        name = _STRENGTH.split(rxs, 1)[0].strip(" ,-")
        if len(name) < 4:
            continue
        m = b.r.resolve(name)
        if not m.key or not m.resolved:
            continue
        n += 1
        b.w.identifier(m.key, "SPL_SETID", setid, source=key,
                       match_method=m.method)
    b._done("dailymed", t0, n)


ALL = [load_atc_substances, load_hierarchy, load_rxnorm, load_dailymed]
