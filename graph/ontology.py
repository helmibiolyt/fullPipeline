"""Two vocabularies that were in the lake and not in the graph.

`Ontologies_Standards` holds 83 CSVs and the graph read two of them. Most of
that is correctly ignored - LOINC describes lab observations nothing in this
schema points at, and CDISC describes how a submission is formatted rather than
anything about a drug. These two are different: both attach to nodes that
already exist, and both were measured before a line of loader was written,
because a crosswalk that resolves 3% is not a crosswalk.

    NCIt -> Target      6,410 rows, 3,399 land on a Target      53%
    MeSH actions       35,790 rows, 21,164 substances resolve   59%

The misses are not failures. NCIt names proteins this graph has no Target for,
and MeSH's supplemental records cover chemicals GSRS has never heard of; both
are recorded in stats so the number is visible rather than assumed.
"""
from __future__ import annotations

import lake
from normalise import fold

L = {
    "ncit_swissprot": "Ontologies_Standards/evs.nci.nih.gov/nci_thesaurus_data/"
                      "mapping_ncit_swissprot.csv",
    "ncit_hgnc":      "Ontologies_Standards/evs.nci.nih.gov/nci_thesaurus_data/"
                      "mapping_ncit_hgnc.csv",
    "mesh_actions":   "Ontologies_Standards/meshb.nlm.nih.gov/mesh_data/"
                      "mesh_pharmacological_actions.csv",
}


def load_ncit_targets(b):
    """NCIt codes onto the Target nodes they name.

    swissprot_id IS the Target key - the same UniProt accession chembl's
    mapping produces - so this is a join, not a name match. That is the whole
    reason to take NCIt's crosswalk files and leave its 212,234 concepts: the
    concepts would need matching by name and would bring their own hierarchy
    to argue with MeSH, while the crosswalk is structural and attaches to what
    is already there.

    Worth having because NCIt is what oncology data speaks. A trial or a
    biomarker panel that names C150023 can now reach the same Target node
    everything else in this graph hangs off.
    """
    t0 = b._step("ncit_targets")
    key = L["ncit_swissprot"]
    n = missing = 0
    for row in lake.stream_csv(key, limit=b.limit):
        code = (row.get("ncit_code") or "").strip()
        acc = (row.get("swissprot_id") or "").strip()
        if not code or not acc:
            continue
        # Only where the Target already exists. Writing the identifier for an
        # accession this graph has no Target for would leave the edge dangling
        # at an endpoint neo4j-admin then invents as an empty node.
        if acc not in b.target_acc:
            missing += 1
            continue
        n += 1
        b.w.identifier(f"UNIPROT:{acc}", "NCIT", code, source=key,
                       match_method="structured")
        # Which node each NCIt code names, so the other three crosswalk files
        # can attach to the same thing instead of re-deriving it.
        b.ncit_key[code] = f"UNIPROT:{acc}"
    b.stats["ncit_targets_not_in_graph"] = missing
    b._done("ncit_targets", t0, n)


def load_mesh_actions(b):
    """Pharmacological action classes, and the substances in them.

    A second classification beside ATC, and a genuinely different one: ATC says
    where a drug sits in a dispensing hierarchy, MeSH says what it DOES -
    "Enzyme Inhibitors", "Neurotransmitter Agents", "Antineoplastic Agents".
    568 of them. Questions like "which drugs are anticoagulants" have no ATC
    answer that is not really a question about a code prefix.

    Keyed MESHPA: so it cannot collide with an ATC code, and `atc_code` is left
    empty so a query filtering on it still sees only the WHO hierarchy. The
    vocabulary column already existed on DrugClass for exactly this.
    """
    t0 = b._step("mesh_actions")
    key = L["mesh_actions"]
    n = unresolved = 0
    seen_class: set[str] = set()
    for row in lake.stream_csv(key, limit=b.limit):
        aui = (row.get("action_descriptor_ui") or "").strip()
        aname = (row.get("action_name") or "").strip()
        sname = (row.get("substance_name") or "").strip()
        if not aui or not aname or not sname:
            continue
        ckey = f"MESHPA:{aui}"
        if ckey not in seen_class:
            seen_class.add(ckey)
            b.w.node("DrugClass", ckey, source=key, name=aname,
                     vocabulary="MeSH Pharmacological Action")
        m = b.r.resolve(sname)
        if not (m.key and m.resolved):
            # A provisional NAME: key would make a DrugClass edge to a node
            # that is only this row's spelling of a chemical. MeSH supplemental
            # records cover chemicals GSRS has never heard of; skipping is
            # honest, and the count is in stats.
            unresolved += 1
            continue
        n += 1
        skey = m.key
        # Rolled up to the parent as everything else is: an action recorded
        # against a salt is an action of the drug. Without this, "which drugs
        # are enzyme inhibitors" misses whichever form MeSH happened to name.
        for k in b.with_parent(skey):
            b.w.edge("IN_CLASS", k, ckey, match_method="name", source=key)
    b.stats["mesh_actions_classes"] = len(seen_class)
    b.stats["mesh_actions_unresolved_substances"] = unresolved
    b._done("mesh_actions", t0, n)


ALL = [load_ncit_targets, load_mesh_actions]


NCIT_CHEBI = ("Ontologies_Standards/evs.nci.nih.gov/nci_thesaurus_data/"
              "mapping_ncit_chebi.csv")
NCIT_HGNC = ("Ontologies_Standards/evs.nci.nih.gov/nci_thesaurus_data/"
             "mapping_ncit_hgnc.csv")
NCIT_CUI = ("Ontologies_Standards/evs.nci.nih.gov/nci_thesaurus_data/"
            "nci_code_cui_map.csv")


def load_ncit_crosswalks(b):
    """Three more NCIt crosswalks, for the same reason as load_ncit_targets:
    they are structural joins, not name matches.

    Each attaches an identifier to a node that already exists and creates
    nothing. That is the entire contribution and it is deliberately small -
    NCIt's 212,234 concepts are NOT loaded, because they would arrive with a
    hierarchy that argues with MeSH's and a name-matching problem on top.

    An identifier is worth having even when nothing queries it yet: it is how
    two records become one node on the next scrape, and adding it later cannot
    retroactively merge what has already been written apart.
    """
    t0 = b._step("ncit_crosswalks")
    n = 0
    ncit_owner = b.ncit_key
    for path, scheme, col in ((NCIT_CHEBI, "CHEBI", "chebi_id"),
                              (NCIT_HGNC, "HGNC", "hgnc_id"),
                              (NCIT_CUI, "UMLS_CUI", "cui")):
        got = 0
        for row in lake.stream_csv(path, limit=b.limit):
            code = (row.get("ncit_code") or row.get("code") or "").strip()
            val = (row.get(col) or "").strip()
            if not code or not val:
                continue
            key = ncit_owner.get(code)
            if not key:
                continue          # NCIt code names nothing in this graph
            b.w.identifier(key, scheme, val, source=path,
                           match_method="crosswalk")
            got += 1
        b.stats[f"ncit_{scheme.lower()}_attached"] = got
        n += got
    b._done("ncit_crosswalks", t0, n)


ALL = ALL + [load_ncit_crosswalks]
