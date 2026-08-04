"""Disease, and the edges that reach it.

Three vocabularies arrive from three directions and none of them agree:

    MeSH        D045743      the descriptor tree, 34k terms, has a hierarchy
    EFO/MONDO   MONDO_0004975  what OpenTargets and ChEMBL speak
    ICD-11      8A20         clinical coding, no crosswalk to either

MeSH is the spine because it is the only one with a usable hierarchy and the
only one that chembl_indications maps to. chembl_indications carries mesh_id
AND efo_id on the same row, which is the one honest crosswalk in the lake - it
is used to fold EFO/MONDO ids onto MeSH nodes. Ids with no crosswalk keep their
own key rather than being guessed at, so "how many diseases" stays answerable.

ICD-11 has no id-level crosswalk to anything here. Its titles are matched to
MeSH descriptor names by exact fold, which is conservative and recorded as
match_method="name"; unmatched codes become their own nodes rather than
disappearing.
"""
from __future__ import annotations

import lake
from normalise import COND_TOO_GENERIC, fold, usable_name

L = {
    "mesh":        "Ontologies_Standards/meshb.nlm.nih.gov/mesh_data/mesh_descriptors.csv",
    "icd11":       "Ontologies_Standards/icd.who.int/icd_data/icd11_codes.csv",
    "icd10_codes":    "Ontologies_Standards/icd.who.int/icd_data/icd10_codes.csv",
    "icd10_blocks":   "Ontologies_Standards/icd.who.int/icd_data/icd10_blocks.csv",
    "icd10_chapters": "Ontologies_Standards/icd.who.int/icd_data/icd10_chapters.csv",
    "indications": "Drug_Substance_Reference/ebi.ac.uk-chembl/chembl_data/chembl_indications.csv",
    "hgnc":        "Targets_Genomics_Biomarkers/genenames.org/data/complete_set/hgnc_complete_set.csv",
    "ot_drugs":    "Targets_Genomics_Biomarkers/platform.opentargets.org/Drugs/known_drugs.csv",
}

# Discovered from S3, not listed here. The six areas that existed when this was
# written are a snapshot of one scraper run; a seventh would have been ignored
# in silence, and the totals would have looked entirely healthy.
OT_PREFIX = "Targets_Genomics_Biomarkers/platform.opentargets.org/Disease_Associations/"

# ICD-11 chapters that are not conditions. X is Extension Codes - anatomy,
# substances, histopathology - and V is the functioning assessment supplement.
# Without this filter 16,904 of 41,475 Disease nodes were things like "Cystic
# duct", "Levator scapulae muscle" and "Pembrolizumab". MeSH gets the
# equivalent filter via its C/F03 tree check; ICD-11 had none.
ICD_NON_DISEASE_CHAPTERS = {"X", "V"}


def ot_disease_key(raw: str) -> str:
    """MONDO_0004975 -> MONDO:0004975, EFO:0000404 -> EFO:0000404.

    OpenTargets writes underscores, ChEMBL writes colons, and both mean the
    same identifier. Normalising here is what lets the chembl crosswalk find
    an OpenTargets disease at all.
    """
    s = (raw or "").strip().replace("_", ":", 1)
    return s.upper() if s else ""


def add_subtype(b, child: str, parent: str, source: str,
                match_method: str = "structured") -> bool:
    """Write SUBTYPE_OF unless it would close a cycle.

    Three loaders build this tree - MeSH, ICD-10 and ICD-11 - and they do not
    agree. MeSH files Glaucoma under Ocular Hypertension; ICD puts it the
    other way. A row that matches MeSH by name BECOMES the MeSH node, so the
    two classifications end up writing opposing edges between the same pair,
    and 19 diseases became their own ancestor.

    Blocking one loader from writing between two MeSH nodes fixed 7 of them
    and left 12, because the disagreements are not all MeSH-to-MeSH: some run
    MeSH -> ICD-11 -> MeSH. Per-loader rules cannot see that; only the tree as
    a whole can.

    So the check is here, once, and it is the real one: walk up from the
    proposed parent, and if the child is already an ancestor, refuse. First
    writer wins, which means MeSH's own hierarchy is kept and a later
    classification may only ADD to it.

    A cycle is not cosmetic. `(x)-[:SUBTYPE_OF*]->(d)` is the shape every
    broad-condition question uses - it is what took Heart Diseases from 1,980
    trials to 31,141 - and a loop makes it non-terminating.
    """
    if not child or not parent or child == parent:
        return False
    seen = 0
    up = parent
    while up and seen < 64:            # 64 is far deeper than any real tree
        if up == child:
            b.stats["subtype_cycles_refused"] = (
                b.stats.get("subtype_cycles_refused", 0) + 1)
            return False
        up = b.subtype_parent.get(up)
        seen += 1
    b.subtype_parent.setdefault(child, parent)
    b.w.edge("SUBTYPE_OF", child, parent, match_method=match_method,
             source=source)
    return True


def load_mesh(b):
    """Disease nodes and their hierarchy.

    tree_numbers is the hierarchy: C04.588.322's parent is whichever descriptor
    owns C04.588. That is a lookup, not an inference - but it needs every
    descriptor in memory first, so parents are emitted in a second pass.

    Only the disease branches are loaded. MeSH covers anatomy, organisms,
    chemicals and publication types too; C (diseases) and F03 (mental
    disorders) are the ones a drug is indicated for. Without this filter
    "Calcimycin" - the first row of the file - becomes a Disease.
    """
    t0 = b._step("mesh")
    key = L["mesh"]
    tree_owner: dict[str, str] = {}
    # Every descriptor name MeSH publishes, including the ones outside the C
    # and F03 trees that do not become Disease nodes. Needed as a veto: see
    # _may_alias.
    b.mesh_any_name = getattr(b, "mesh_any_name", set())
    pending: list[tuple[str, str]] = []
    n = 0
    for row in lake.stream_csv(key, limit=b.limit):
        ui = (row.get("descriptor_ui") or "").strip()
        name = (row.get("name") or "").strip()
        trees = [t.strip() for t in (row.get("tree_numbers") or "").split(";") if t.strip()]
        if not ui or not name:
            continue
        b.mesh_any_name.add(fold(name))
        if not any(t.startswith("C") or t.startswith("F03") for t in trees):
            continue
        n += 1
        dkey = f"MESH:{ui}"
        # Entry terms are kept on the node, not just used to build the matcher.
        # They are how anyone actually searches: MeSH's heading is "Carcinoma,
        # Non-Small-Cell Lung" and the query is "NSCLC". Capped at 30 because a
        # handful of descriptors carry hundreds and the tail is chemical
        # registry strings nobody types.
        all_syns = [s.strip() for s in (row.get("synonyms") or "").split(";")
                    if len(s.strip()) >= 2]
        syns = all_syns[:30]
        b.w.node("Disease", dkey, source=key, name=name, vocabulary="MeSH",
                 synonyms=";".join(syns), tree_numbers=";".join(trees))
        b.w.identifier(dkey, "MESH", ui, source=key)
        # A node whose NAME is a bare category word. Guarding the query string
        # was not enough: "Symptom Cluster" is a perfectly specific phrase and
        # a synonym of the descriptor called "Syndrome", so the variant looked
        # fine and the destination was still meaningless. The guard belongs on
        # where the edge LANDS, not on what was typed to find it.
        if fold(name) in COND_TOO_GENERIC:
            b.generic_disease_keys.add(dkey)
        b.mesh_by_name.setdefault(fold(name), dkey)
        # The stored property is capped at 30 for readability; the MATCHER is
        # not. One number was deciding both, and 'renal cell carcinoma' is
        # entry term 40 of 'Carcinoma, Renal Cell' - so 313 ct.gov trials
        # naming it found nothing while the term sat in the same row. The
        # longest list is 127; uncapping the matcher links 4,303 more trials.
        for syn in all_syns:
            f = fold(syn)
            if len(f) >= 4:
                b.mesh_by_name.setdefault(f, dkey)
        for t in trees:
            tree_owner.setdefault(t, dkey)
            if "." in t:
                pending.append((dkey, t.rsplit(".", 1)[0]))

    for child, parent_tree in pending:
        parent = tree_owner.get(parent_tree)
        if parent and parent != child:
            add_subtype(b, child, parent, key)
    b._done("mesh", t0, n)


# ICD writes housekeeping titles that are not conditions anyone runs a trial
# on. Indexing them would attach trials to "Other specified disorders of the
# ear", which is worse than leaving the trial unlinked.
_ICD_NOT_A_CONDITION = (
    "other specified", "unspecified", "not elsewhere classified",
    "without mention", "other disorders of", "other diseases of",
    "sequelae of", "personal history", "family history", "encounter for",
)


def _index_icd(b, title: str, dkey: str) -> None:
    """Make an ICD title reachable by trial condition matching.

    A SECOND dictionary, not mesh_by_name, so a MeSH hit always wins and the
    weaker source stays visible on the edge. Worth only 1.1% of ct.gov on its
    own - measured before it was written - which is also why it is guarded
    hard: a generic ICD title matches far more prose than it should.
    """
    low = " ".join(title.lower().split())
    if len(low) < 6 or any(w in low for w in _ICD_NOT_A_CONDITION):
        return
    b.icd_by_name.setdefault(fold(title), dkey)


def load_icd11(b):
    """A second coding system, attached to MeSH where the name matches, and
    WITH its hierarchy - which it did not have.

    The previous version skipped every row whose `code` was blank, on the
    reasoning that those are chapter and block headings with no identifier to
    key on. They are, and skipping them meant the 28 chapters and 1,360 blocks
    were never created - so all 16,965 ICD-11 diseases had no parent, against
    99.8% for ICD-10 and 97.8% for MeSH. Over half the Disease label was flat.

    That is not a cosmetic gap. `(x)-[:SUBTYPE_OF*]->(d)` is how a question
    about a broad condition is answered - it is what took Heart Diseases from
    1,980 trials to 31,141 - and it could never reach an ICD-11 node. The
    icd_name tier has put 39,056 trial links on those nodes, every one of them
    invisible to a rollup.

    The file carries `parent` as a foundation URI and `foundation_uri` as the
    row's own, so the tree is a join rather than an inference. Chapters and
    blocks are keyed on what they do have - chapter_no and block_id.

    Two passes, for the same reason load_icd10 needs them: a row whose title
    matches MeSH IS the MeSH node and is keyed MESH:*, so a child pointing at
    its URI has to be resolved through what the parent actually became, not
    through what its own file called it.
    """
    t0 = b._step("icd11")
    key = L["icd11"]
    n = matched = skipped = 0
    by_uri: dict[str, str] = {}          # foundation_uri -> the key it became
    pending: list[tuple[str, str]] = []  # (child key, parent uri)

    for row in lake.stream_csv(key, limit=b.limit):
        title = (row.get("title") or "").strip()
        if not title:
            continue
        chapter = (row.get("chapter_no") or "").strip().upper()
        code = (row.get("code") or "").strip()
        if (chapter in ICD_NON_DISEASE_CHAPTERS
                or (code and code[:1].upper() in ICD_NON_DISEASE_CHAPTERS)):
            skipped += 1
            continue

        kind = (row.get("class_kind") or "").strip().lower()
        uri = (row.get("foundation_uri") or "").strip()
        block = (row.get("block_id") or "").strip()

        # Chapters and blocks have no code, which is why they were dropped.
        # They do have an identity, and it is the level a question groups by.
        if code:
            own = f"ICD11:{code}"
        elif kind == "block" and block:
            own = f"ICD11:{block}"
        elif kind == "chapter" and chapter:
            own = f"ICD11:CH{chapter}"
        else:
            continue

        n += 1
        hit = b.mesh_by_name.get(fold(title))
        dkey = hit or own
        if hit:
            matched += 1
        else:
            b.w.node("Disease", dkey, source=key, name=title,
                     vocabulary="ICD-11")
            _index_icd(b, title, dkey)
        if code:
            b.w.identifier(dkey, "ICD11", code, source=key,
                           match_method="name" if hit else "structured")
        if uri:
            by_uri.setdefault(uri, dkey)
        parent_uri = (row.get("parent") or "").strip()
        if parent_uri:
            pending.append((dkey, parent_uri))

    edges = 0
    for child, parent_uri in pending:
        pkey = by_uri.get(parent_uri)
        # A parent outside the disease chapters was skipped, so its children
        # simply have no parent here. Writing the edge anyway would dangle at
        # an endpoint neo4j-admin then invents as an empty node.
        if not pkey or pkey == child:
            continue
        if not add_subtype(b, child, pkey, key):
            continue
        edges += 1

    b.stats["icd11_name_matched"] = matched
    b.stats["icd11_non_disease_skipped"] = skipped
    b.stats["icd11_subtype_edges"] = edges
    b._done("icd11", t0, n)


def load_icd10(b):
    """ICD-10, with its hierarchy, which ICD-11 is not loaded with.

    ICD-10 is what claims, registries and hospital coding actually use;
    ICD-11 adoption is still thin. Leaving it out meant a MeSH disease could
    not be reached from the code a hospital record carries.

    The difference from load_icd11 is that this one keeps the TREE. The files
    name each level explicitly - `parent_code` on a code, `chapter_id` on a
    block - so SUBTYPE_OF costs nothing to derive and makes "everything under
    C00-C97" a traversal instead of a string prefix match. That is the whole
    reason to hold a classification in a graph rather than a table.

    Chapters and blocks are Disease nodes too: they are the levels you group
    by, and a node with no parent is a chapter, not a special case.
    """
    t0 = b._step("icd10")
    n = matched = 0

    # Chapters, then blocks, then codes: a child's parent must exist before the
    # edge is written, and each file names the level above it.
    for row in lake.stream_csv(L["icd10_chapters"], limit=b.limit):
        cid = (row.get("chapter_id") or "").strip()
        title = (row.get("title") or "").strip()
        if not cid or not title:
            continue
        if cid.upper() in ICD_NON_DISEASE_CHAPTERS:
            continue
        n += 1
        b.w.node("Disease", f"ICD10:{cid}", source=L["icd10_chapters"],
                 name=title, vocabulary="ICD-10")

    for row in lake.stream_csv(L["icd10_blocks"], limit=b.limit):
        bid = (row.get("block_id") or "").strip()
        title = (row.get("title") or "").strip()
        cid = (row.get("chapter_id") or "").strip()
        if not bid or not title or cid.upper() in ICD_NON_DISEASE_CHAPTERS:
            continue
        n += 1
        b.w.node("Disease", f"ICD10:{bid}", source=L["icd10_blocks"],
                 name=title, vocabulary="ICD-10")
        if cid:
            add_subtype(b, f"ICD10:{bid}", f"ICD10:{cid}",
                     match_method="structured", source=L["icd10_blocks"])

    # Nodes first, edges second - the same two-pass shape load_mesh uses, and
    # for the same reason. A code that matches MeSH by name IS the MeSH node,
    # so A00 "Cholera" is keyed MESH:D002771 and not ICD10:A00. Its children
    # still say parent_code=A00, and writing that edge as it is read produced
    # three parents pointing at a node that does not exist. The map records
    # what each code actually resolved to; parents are looked up through it.
    resolved: dict[str, str] = {}
    parents: list[tuple[str, str]] = []

    for row in lake.stream_csv(L["icd10_codes"], limit=b.limit):
        code = (row.get("code") or "").strip()
        title = (row.get("title") or "").strip()
        cid = (row.get("chapter_id") or "").strip()
        if not code or not title:
            continue
        if cid.upper() in ICD_NON_DISEASE_CHAPTERS or code[:1].upper() in ICD_NON_DISEASE_CHAPTERS:
            continue
        n += 1
        # Same rule as ICD-11: an exact name match makes this the MeSH disease
        # and the code becomes an identifier on it, so the two vocabularies
        # describe one node rather than two.
        hit = b.mesh_by_name.get(fold(title))
        dkey = hit or f"ICD10:{code}"
        if hit:
            matched += 1
        else:
            _index_icd(b, title, dkey)
            b.w.node("Disease", dkey, source=L["icd10_codes"], name=title,
                     vocabulary="ICD-10")
        b.w.identifier(dkey, "ICD10", code, source=L["icd10_codes"],
                       match_method="name" if hit else "structured")
        resolved[code] = dkey
        # Parent first, block second: a code's immediate parent is another
        # code where there is one, and only the top of a subtree hangs off the
        # block. Writing both would make the tree wrong, not merely redundant.
        parent = (row.get("parent_code") or "").strip()
        block = (row.get("block_id") or "").strip()
        if parent:
            parents.append((code, parent))
        elif block:
            parents.append((code, f"\0block\0{block}"))

    for code, above in parents:
        child = resolved.get(code)
        if not child:
            continue
        dst = (f"ICD10:{above.split(chr(0))[2]}" if above.startswith("\0block\0")
               else resolved.get(above, ""))
        if dst and dst != child:
            add_subtype(b, child, dst,
                     source=L["icd10_codes"])

    b.stats["icd10_name_matched"] = matched
    b._done("icd10", t0, n)


def load_indications(b):
    """INDICATED_FOR, and the EFO->MeSH crosswalk everything else depends on.

    The crosswalk is the reason this file is loaded before OpenTargets: it is
    the only place where a MeSH id and an EFO id appear on the same row, so it
    is the only evidence that MONDO:0004975 and MeSH D000544 are one disease.
    """
    t0 = b._step("chembl_indications")
    key = L["indications"]
    n = 0
    for row in lake.stream_csv(key, limit=b.limit):
        skey = b.molregno_key.get((row.get("molregno") or "").strip())
        mesh = (row.get("mesh_id") or "").strip()
        efo = ot_disease_key(row.get("efo_id", ""))
        heading = (row.get("mesh_heading") or "").strip()
        if mesh and efo:
            b.efo_mesh.setdefault(efo, f"MESH:{mesh}")
        if not skey or not (mesh or efo):
            continue
        n += 1
        dkey = f"MESH:{mesh}" if mesh else efo
        # chembl references MeSH descriptors this build may not have loaded
        # (a slice, or a non-disease branch). Emit the node so the edge has an
        # endpoint; the Writer keeps the richer mesh version if it exists.
        b.w.node("Disease", dkey, source=key, name=heading or row.get("efo_term", ""),
                 vocabulary="MeSH" if mesh else "EFO")
        # max_phase_for_ind is the whole difference between "approved for" and
        # "has been tried in". It is per PAIR, not per drug - 4,458 of the
        # 10,073 molecules here carry more than one value across their own
        # indications - so it is the only column that can tell the two apart.
        # Dropping it made tocilizumab an epilepsy drug: it has a real phase 2
        # trial in refractory status epilepticus, and without the phase the
        # edge is indistinguishable from levetiracetam's.
        #
        # Only 8,683 of 60,055 rows are phase 4. Read the other 86% as
        # approvals and the graph answers a question nobody asked.
        phase = (row.get("max_phase_for_ind") or "").strip()
        for k in b.with_parent(skey):
            b.w.edge("INDICATED_FOR", k, dkey, match_method="structured",
                     source=key, max_phase=phase)
    b._done("chembl_indications", t0, n)


def load_hgnc(b):
    """Gene symbol and Ensembl id -> the UniProt-keyed Target node.

    Loaded for the crosswalk as much as the enrichment: OpenTargets identifies
    targets by ENSG and by symbol, neither of which is the Target key, so
    without this table every OpenTargets edge would dangle.
    """
    t0 = b._step("hgnc")
    key = L["hgnc"]
    n = 0
    for row in lake.stream_csv(key, limit=b.limit):
        sym = (row.get("symbol") or "").strip()
        # uniprot_ids is pipe-delimited; the first is the canonical accession
        accs = [a.strip() for a in (row.get("uniprot_ids") or "").replace('"', "").split("|") if a.strip()]
        ensg = (row.get("ensembl_gene_id") or "").strip()
        if not sym or not accs:
            continue
        n += 1
        tkey = f"UNIPROT:{accs[0]}"
        # Enrichment only: do not create Targets that ChEMBL never saw, or the
        # graph gains 42k proteins with no drug attached to any of them.
        #
        # The crosswalk is gated on the same condition, and that is the part
        # that is easy to get wrong: mapping every gene here while creating
        # nodes for only some of them leaves OpenTargets emitting associations
        # to targets that were never written. It cost 59,076 dangling edges
        # before the validator caught it.
        if tkey not in b.w._seen.get(("nodes", "Target"), ()):
            b.skipped_targets.add(tkey)
            continue
        b.w.node("Target", tkey, source=key, symbol=sym,
                 name=row.get("name", ""), organism="Homo sapiens")
        b.symbol_target[sym.upper()] = tkey
        if ensg:
            b.ensg_target[ensg] = tkey
    b.stats["hgnc_genes_without_chembl_target"] = len(b.skipped_targets)
    b._done("hgnc", t0, n)


def resolve_disease(b, raw_key: str, name: str) -> tuple[str, str]:
    """An OpenTargets disease id -> the node it belongs on.

    OpenTargets speaks MONDO and EFO; the graph's spine is MeSH. Until now
    this used only the ChEMBL EFO<->MeSH crosswalk and, when that missed, kept
    the raw id and created a second node for a disease MeSH already had.

    The cost was invisible and large. ASSOCIATED_WITH put 21,690 edges on
    MONDO nodes against 10,827 on MeSH, so "which targets are associated with
    this disease" answered from a third of the evidence, and 593 disease names
    existed twice - 'anxiety' as MESH:D001007 with 173 relationships and
    HP:0000739 with 81, neither reachable from the other.

    Three tiers, the same discipline load_icd11 already used:
      1. the ChEMBL EFO/MeSH crosswalk - an explicit statement of equivalence
      2. exact name against MeSH descriptors and their entry terms
      3. the source's own id, as a genuinely new disease

    Returns (key, how) so the caller can record which tier matched.
    """
    hit = b.efo_mesh.get(raw_key)
    if hit:
        return hit, "crosswalk"
    if name:
        hit = b.mesh_by_name.get(fold(name))
        if hit:
            return hit, "name"
    return raw_key, "own"


def load_opentargets_drugs(b):
    """A second source for INDICATED_FOR and TARGETS, joined on CHEMBL id.

    OpenTargets is not the base for either edge - ChEMBL is - but it covers
    drug/indication pairs ChEMBL's own table misses, and it names the target by
    symbol where chembl_mechanisms names it by tid.
    """
    t0 = b._step("opentargets_drugs")
    key = L["ot_drugs"]
    n = 0
    for row in lake.stream_csv(key, limit=b.limit):
        skey = b.chembl_mol_key.get((row.get("drug_id") or "").strip())
        if not skey:
            continue
        n += 1
        ind = ot_disease_key(row.get("indication_id", ""))
        if ind:
            iname = row.get("indication_name", "")
            dkey, how = resolve_disease(b, ind, iname)
            b.stats[f"ot_drugs_disease_{how}"] =                 b.stats.get(f"ot_drugs_disease_{how}", 0) + 1
            b.w.node("Disease", dkey, source=key, name=iname,
                     vocabulary="MeSH" if dkey.startswith("MESH:") else "EFO")
            if how != "own" and ind != dkey:
                b.w.identifier(dkey, "EFO", ind, source=key, match_method=how)
            # No max_phase here on purpose, though the column exists. OT's
            # max_phase is the DRUG's furthest phase, not this indication's:
            # zero of 3,087 drugs vary across their own indication rows, so
            # every row of an approved drug reads APPROVAL whatever the
            # indication. Writing it would mark every condition atorvastatin
            # was ever studied in as approved. Null here means unknown, which
            # is the truth - ChEMBL is the only source that knows.
            for k in b.with_parent(skey):
                b.w.edge("INDICATED_FOR", k, dkey, match_method="structured",
                         source=key)
        tkey = b.symbol_target.get((row.get("target_symbol") or "").strip().upper())
        if tkey:
            for k in b.with_parent(skey):
                b.w.edge("TARGETS", k, tkey, match_method="symbol", source=key)
    b._done("opentargets_drugs", t0, n)


def load_opentargets_assoc(b):
    """Target <-> Disease association scores, six disease areas.

    Scored rather than binary, and one target/disease pair appears once per
    datatype (genetic_literature, known_drug, rna_expression...). Only the
    strongest is kept: the edge means "the best evidence for this link scores
    X", and a mean across datatypes would understate a strong genetic link.
    """
    t0 = b._step("opentargets_assoc")
    best: dict[tuple[str, str], tuple[float, str, str]] = {}
    n = 0
    areas = lake.list_keys(OT_PREFIX, "_targets.csv")
    b.stats["opentargets_areas"] = len(areas)
    for key in areas:
        for row in lake.stream_csv(key, limit=b.limit):
            tkey = b.ensg_target.get((row.get("target_id") or "").strip())
            if not tkey:
                continue
            dis = ot_disease_key(row.get("disease_id", ""))
            if not dis:
                continue
            dkey, how = resolve_disease(b, dis, row.get("disease_name", ""))
            b.stats[f"ot_assoc_disease_{how}"] =                 b.stats.get(f"ot_assoc_disease_{how}", 0) + 1
            try:
                score = float(row.get("overall_score") or 0)
            except ValueError:
                continue
            n += 1
            k = (tkey, dkey)
            if k not in best or score > best[k][0]:
                best[k] = (score, row.get("disease_name", ""), key)

    for (tkey, dkey), (score, dname, src) in best.items():
        b.w.node("Disease", dkey, source=src, name=dname,
                 vocabulary="MeSH" if dkey.startswith("MESH:") else "EFO")
        b.w.edge("ASSOCIATED_WITH", tkey, dkey, match_method="structured",
                 source=src, score=f"{score:.4f}")
    b._done("opentargets_assoc", t0, n)


ALL = [load_mesh, load_icd11, load_icd10, load_indications, load_hgnc,
       load_opentargets_drugs, load_opentargets_assoc]


# --------------------------------------------------------------------- bridge

NCIT_CONCEPTS = ("Ontologies_Standards/evs.nci.nih.gov/nci_thesaurus_data/"
                 "nci_thesaurus_concepts.csv")
NEOPLASM_CORE = ("Ontologies_Standards/evs.nci.nih.gov/nci_thesaurus_data/"
                 "neoplasm_core.csv")
CDISC_FILES = (
    "Ontologies_Standards/cdisc.org/CDISC/data/SDTM Terminology.csv",
    "Ontologies_Standards/cdisc.org/CDISC/data/CDASH Terminology.csv",
    "Ontologies_Standards/cdisc.org/CDISC/data/Protocol Terminology.csv",
)

#: An alias shorter than this bridges too much. "ALS" is worth having and is
#: reached as an exact MeSH entry term already; 4 characters is where a
#: three-letter code stops being ambiguous.
MIN_ALIAS = 5


def _vocab_concepts():
    """One list of names per concept, from every vocabulary carrying synonyms."""
    for row in lake.stream_csv(NCIT_CONCEPTS):
        out = [(row.get("preferred_name") or "").strip()]
        out += [s.strip() for s in (row.get("synonyms") or "").split("|")]
        yield [x for x in out if x]
    for row in lake.stream_csv(NEOPLASM_CORE):
        out = [(row.get("Preferred Term") or "").strip()]
        out += [s.strip() for s in (row.get("Synonyms") or "").split("|")]
        yield [x for x in out if x]
    for path in CDISC_FILES:
        for row in lake.stream_csv(path):
            out = [(row.get("NCI Preferred Term") or "").strip(),
                   (row.get("CDISC Submission Value") or "").strip()]
            out += [s.strip()
                    for s in (row.get("CDISC Synonym(s)") or "").split(";")]
            yield [x for x in out if x]


def _may_alias(b, folded: str) -> bool:
    """False when this string is MeSH's own name for something else.

    The bridge put 5,027 trials on "Anxiety Disorders" through an alias that
    was the single word "Anxiety". MeSH has a descriptor for anxiety the
    SYMPTOM, D001007, but it lives in tree F01 and this build only makes
    Disease nodes from C and F03 - so the symptom is not a node, the alias has
    nowhere right to go, and it lands on the psychiatric diagnosis. A trial
    reducing pre-operative anxiety became an anxiety-disorder trial.

    So a string MeSH already uses as a descriptor name is never allowed to
    become an alias for a DIFFERENT descriptor, whichever tree it sits in.
    Being outside the disease trees is a reason to leave a term unlinked, not
    a licence to attach it to the nearest thing that is.
    """
    return folded not in b.mesh_any_name


def load_vocab_aliases(b):
    """NCIt and CDISC synonyms, used as a BRIDGE to MeSH and never as nodes.

    What was left after rewriting was not missing concepts, it was a different
    vocabulary: trials write "Lung Cancer" and "NSCLC" where MeSH heads "Lung
    Neoplasms" and "Carcinoma, Non-Small-Cell Lung". Rewriting cannot cross
    that - the strings have no shared shape - but NCI already curated the
    mapping, and the lake already holds it.

    The rule is one line: if ANY name on a concept already resolves to a MeSH
    Disease, the concept's OTHER names become aliases for that same node.
    Nothing is created and nothing is inferred - a bridge exists only where
    the two vocabularies already agree on one string, and 232,275 concepts
    that touch no MeSH node contribute nothing at all.

    This is also how "Diabetes" is reached safely. Expanding it by prefix was
    measured and rejected: it is a prefix of ten headings including Diabetes
    Insipidus, so picking one is a guess. NCIt lists it as a synonym of
    Diabetes Mellitus, which is a decision someone qualified already made.

    Kept in its own dictionary so the edge can say `vocab_alias` and a curated
    synonym is never mistaken for the registry having written the MeSH heading.
    """
    t0 = b._step("vocab_aliases")
    n = touched = 0
    for names in _vocab_concepts():
        hit = None
        for x in names:
            hit = b.mesh_by_name.get(fold(x))
            if hit:
                break
        if not hit:
            continue
        touched += 1
        for x in names:
            f = fold(x)
            # Never shadow MeSH's own wording, and never re-point an alias
            # another concept already claimed - first writer wins, as
            # everywhere else in this build.
            if (len(f) >= MIN_ALIAS and f not in b.mesh_by_name
                    and _may_alias(b, f)):
                if b.alias_by_name.setdefault(f, hit) == hit:
                    n += 1
    b.stats["vocab_alias_concepts_matched"] = touched
    b.stats["vocab_aliases"] = len(b.alias_by_name)
    b._done("vocab_aliases", t0, n)


MESH_SCR = ("Ontologies_Standards/meshb.nlm.nih.gov/mesh_data/"
            "mesh_supplemental_concepts.csv")
ANTINEO = ("Ontologies_Standards/evs.nci.nih.gov/nci_thesaurus_data/"
           "antineoplastic_agents.csv")


def load_mesh_scr(b):
    """MeSH Supplementary Concept Records - 324,045 of them, none read before.

    An SCR is a name MeSH did not give a descriptor of its own, and every one
    carries the descriptor it maps TO. That is the bridge already drawn: no
    inference, no string similarity, just a crosswalk NLM maintains.

    Only the 6,547 that map to a DISEASE descriptor are used here. The other
    317,498 map to chemical headings - "bevonium" to "Benzilates" - and
    attaching a trial to a chemical class because its drug name appeared in
    the condition field would be a wrong link, not a thin one.

    Worth 17,032 aliases, and they are the rare-disease tail: the names that
    are too specific for a MeSH heading are exactly the ones a trial studying
    a rare disease writes.
    """
    t0 = b._step("mesh_scr")
    disease_uis = {k.split(":", 1)[1] for k in b.generic_disease_keys} | set()
    # Rebuild the set of descriptors that became Disease nodes: mesh_by_name
    # holds their keys, and a MESH: key is a Disease by construction here.
    disease_uis = {v.split(":", 1)[1] for v in b.mesh_by_name.values()
                   if v.startswith("MESH:")}
    n = 0
    for row in lake.stream_csv(MESH_SCR, limit=b.limit):
        uis = [u.strip().lstrip("*")
               for u in (row.get("heading_mapped_to_uis") or "").split(";")
               if u.strip()]
        hit = next((u for u in uis if u in disease_uis), None)
        if not hit:
            continue
        dkey = f"MESH:{hit}"
        names = [(row.get("name") or "").strip()]
        names += [x.strip() for x in (row.get("synonyms") or "").split(";")]
        for x in names:
            f = fold(x)
            if (len(f) >= MIN_ALIAS and f not in b.mesh_by_name
                    and _may_alias(b, f)):
                if b.alias_by_name.setdefault(f, dkey) == dkey:
                    n += 1
    b.stats["mesh_scr_aliases"] = n
    b._done("mesh_scr", t0, n)


def load_antineoplastic_aliases(b):
    """NCIt's antineoplastic agents, as substance aliases only.

    Same bridge rule as the disease vocabularies, applied to Substance: a
    concept contributes its synonyms only where one of its names already
    resolves to a substance this graph knows. Oncology drug names are where
    trial intervention text is most varied - a code name, a generic, and three
    spellings of the same salt - and the resolver never sees most of them.

    Registered through add_alias, so these map to the node key directly and
    can never outrank a UNII: the resolver's own tier order decides, and this
    is its last tier.
    """
    t0 = b._step("antineoplastic")
    n = 0
    for row in lake.stream_csv(ANTINEO, limit=b.limit):
        names = [(row.get("NCIt Preferred Name") or "").strip()]
        names += [x.strip() for x in (row.get("Synonyms") or "").split("||")]
        names = [x for x in names if x and usable_name(x)]
        hit = None
        for x in names:
            m = b.r.resolve(x)
            if m.key and m.resolved:
                hit = m.key
                break
        if not hit:
            continue
        for x in names:
            if b.r.resolve(x).resolved:
                continue
            b.r.add_alias(x, hit, method="ncit_oncology")
            n += 1
    b.stats["antineoplastic_aliases"] = n
    b._done("antineoplastic", t0, n)


ALL = ALL + [load_vocab_aliases, load_mesh_scr, load_antineoplastic_aliases]
