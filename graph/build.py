#!/usr/bin/env python3
"""Build the graph to node and edge tables.

    python graph/build.py --slice atorvastatin,erenumab,pembrolizumab
    python graph/build.py --all

Slice mode exists because the full build reads ~13M rows and takes a while,
and a mapping bug found after that is a mapping bug found expensively. A slice
restricted to a handful of molecules runs in seconds and produces a graph small
enough to read end to end, so the logic gets exercised dozens of times before
anything expensive happens.

Nothing here talks to Neo4j. Output is CSV under graph/build/, which is both
what the validator reads and what `neo4j-admin database import` consumes later.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import disease
import lake
import literature
import products
import ontology
import reference
import safety
import trials
import variants
from emit import Writer
from normalise import (Resolver, fold, is_placeholder, norm_company,
                       split_synonyms)

LAKE = {
    "atc_classes":   "Drug_Substance_Reference/atcddd.fhi.no/atc_ddd_data/atc_classes.csv",
    "gsrs":          "Drug_Substance_Reference/gsrs.ncats.nih.gov/gsrs_data/gsrs_substances.csv",
    "molecules":     "Drug_Substance_Reference/ebi.ac.uk-chembl/chembl_data/chembl_molecules.csv",
    "structures":    "Drug_Substance_Reference/ebi.ac.uk-chembl/chembl_data/chembl_structures.csv",
    "synonyms":      "Drug_Substance_Reference/ebi.ac.uk-chembl/chembl_data/chembl_synonyms.csv",
    "targets":       "Drug_Substance_Reference/ebi.ac.uk-chembl/chembl_data/chembl_targets.csv",
    "uniprot_map":   "Drug_Substance_Reference/ebi.ac.uk-chembl/chembl_data/chembl_uniprot_mapping.csv",
    "mechanisms":    "Drug_Substance_Reference/ebi.ac.uk-chembl/chembl_data/chembl_mechanisms.csv",
    "action_types":  "Drug_Substance_Reference/ebi.ac.uk-chembl/chembl_data/chembl_action_types.csv",
}



def _no_placeholder(v: str) -> str:
    """Empty out a value that only means 'we do not know'."""
    v = (v or "").strip()
    return "" if is_placeholder(v) else v


def _chembl_phase(v: str) -> str:
    """ChEMBL's max_phase, with its sentinel removed.

    -1 is not a phase. ChEMBL uses it for "unknown", and stored as a number it
    sorts below 0 and satisfies `max_phase < 1`, so 426 substances of unknown
    development stage counted as preclinical in every such filter.
    """
    v = (v or "").strip()
    return "" if v.startswith("-1") else v


class Build:
    def __init__(self, outdir: pathlib.Path, slice_names: set[str] | None,
                 limit: int | None):
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.w = Writer(outdir=outdir, run_id=self.run_id)
        self.r = Resolver()
        self.slice = slice_names
        self.limit = limit
        # Join keys the sources use internally. ChEMBL threads everything
        # through molregno and tid, neither of which is a graph key, so the
        # mapping to graph keys has to be held while loading.
        self.molregno_key: dict[str, str] = {}
        self.tid_key: dict[str, str] = {}
        self.target_acc: set[str] = set()
        self.chembl_target_key: dict[str, str] = {}
        self.ca_code_key: dict[str, str] = {}       # Canada DRUG_CODE -> Product key
        self.fda_appl_products: dict[str, set] = {} # Appl_No -> {(Product_No, key)}
        self.chembl_mol_key: dict[str, str] = {}    # CHEMBL id -> Substance key
        # The WHO ATC vocabulary, as a set. Product sources write their own
        # "ATC" values and several are not WHO codes at all - Canada's TC_ATC
        # includes local classifications - so membership is checked rather
        # than assumed, or IN_CLASS points at classes that do not exist.
        self.atc_codes: set[str] = set()
        # Dictionaries the prose matchers look things up in. Built by the
        # loaders that own the vocabulary, read by every loader after them -
        # which is why load order in run() is not cosmetic.
        self.mesh_by_name: dict[str, str] = {}      # folded name -> Disease key
        # ICD titles, kept apart from mesh_by_name so a MeSH hit always wins
        # and a match against the weaker dictionary is recorded as such.
        self.icd_by_name: dict[str, str] = {}
        # NCIt/CDISC synonyms that reach a MeSH node. Separate again, so the
        # edge can record that the match came from a curated crosswalk rather
        # than from the registry writing MeSH's own heading.
        self.alias_by_name: dict[str, str] = {}
        # NCIt code -> the node key it names. Filled by load_ncit_targets and
        # read by the other three crosswalk files, so each attaches to what is
        # already there rather than deriving the join again.
        self.ncit_key: dict[str, str] = {}
        # CDISC dosage-form key -> the submission value regulators write.
        self.form_std: dict[str, str] = {}
        # Disease nodes named for a category rather than a condition. A
        # rewritten or ICD match may never land here; an exact one may.
        self.generic_disease_keys: set[str] = set()
        self.efo_mesh: dict[str, str] = {}          # EFO/MONDO id -> MeSH key
        self.symbol_target: dict[str, str] = {}     # gene symbol -> Target key
        self.salt_parent: dict[str, str] = {}       # salt key -> parent key
        self.ensg_target: dict[str, str] = {}       # ENSG id -> Target key
        self.skipped_targets: set[str] = set()      # HGNC genes ChEMBL lacks
        self.rxcui_key: dict[str, str] = {}         # RXCUI -> Substance key
        self.bla_key: dict[str, str] = {}           # BLA number -> Product key
        self.timings: dict[str, float] = {}
        self.stats: dict = {}

    # ---- slice ----------------------------------------------------------
    def wanted(self, *names: str) -> bool:
        """True if this row is in scope. Everything is in scope for a full run."""
        if self.slice is None:
            return True
        for n in names:
            f = fold(n or "")
            if not f:
                continue
            if f in self.slice:
                return True
            # a slice term appearing inside a longer name still counts, so
            # "atorvastatin" catches "atorvastatin calcium"
            if any(s in f for s in self.slice):
                return True
        return False

    def wanted_trial(self, interventions: str, title: str) -> bool:
        """Slice scope for trials.

        A trial is in scope if a slice substance appears in what it tested. The
        title is a fallback for registries that have no intervention column at
        all - CTRI and jRCT - where dropping every trial would be worse than
        matching on prose.
        """
        if self.slice is None:
            return True
        blob = fold(f"{interventions} {title}")
        return any(s in blob for s in self.slice)

    def with_parent(self, skey: str) -> list[str]:
        """A substance key, plus its parent if it is a salt.

        ChEMBL and FAERS annotate whichever form the source happened to name,
        which is usually the salt or the hydrate. Left alone, that puts the
        pharmacology on a node nobody asks for: `atorvastatin` had no
        mechanism because it sits on `atorvastatin calcium anhydrous`, and
        `metformin` had no adverse events because all 1,370 hang off
        `metformin hydrochloride`. Measured across the graph, 1,006 parent
        substances lacked a mechanism a salt form carried, 707 lacked targets,
        632 lacked indications and 533 lacked adverse events.

        A salt and its parent share an active moiety, so the target, the
        mechanism, the indication and the reported reaction belong to both.
        The salt keeps its own edge as well - a product contains the salt and
        the strength on the label is the salt's - so nothing is lost, and
        IS_SALT_OF still records which is which.

        One hop only. Salt-of-a-salt does not occur in ChEMBL's hierarchy and
        chaining would risk a cycle.
        """
        parent = self.salt_parent.get(skey)
        return [skey, parent] if parent and parent != skey else [skey]

    def _step(self, name):
        t0 = time.time()
        print(f"  {name:22}", end="", flush=True)
        return t0

    def _done(self, name, t0, n):
        self.timings[name] = time.time() - t0
        print(f" {n:>9,} rows   {self.timings[name]:6.1f}s")

    # ---- vocabularies ---------------------------------------------------
    def load_atc(self):
        """DrugClass plus its own hierarchy. Loaded whole even in slice mode -
        it is 1,318 rows and every substance may point into it."""
        t0 = self._step("atc_classes")
        key = LAKE["atc_classes"]
        n = 0
        pending_parents = []
        for row in lake.stream_csv(key):
            code = (row.get("atc_code") or "").strip()
            if not code:
                continue
            n += 1
            self.atc_codes.add(code)
            self.w.node("DrugClass", f"ATC:{code}", source=key,
                        atc_code=code, name=row.get("name", ""),
                        level=row.get("level", ""), vocabulary="ATC")
            parent = (row.get("parent_code") or "").strip()
            if parent:
                pending_parents.append((code, parent))
        for code, parent in pending_parents:
            if parent in self.atc_codes:
                self.w.edge("IN_CLASS", f"ATC:{code}", f"ATC:{parent}", source=key)
        self._done("atc_classes", t0, n)

    # ---- substance spine -------------------------------------------------
    def load_gsrs(self):
        """The naming authority: Substance nodes, UNII/CAS identifiers, and the
        name lookup every later loader resolves through."""
        t0 = self._step("gsrs")
        key = LAKE["gsrs"]
        n = 0
        for row in lake.stream_csv(key, limit=self.limit):
            unii = (row.get("unii") or "").strip()
            name = (row.get("preferred_name") or "").strip()
            if not unii or not name:
                continue
            syns = split_synonyms(row.get("synonyms", ""))
            if not self.wanted(name, *syns):
                continue
            n += 1
            skey = f"UNII:{unii}"
            # Same discard as MeSH had: gsrs synonyms carry the brand and
            # trade names people search by, and they were being read into the
            # resolver and then dropped from the node.
            self.w.node("Substance", skey, source=key, name=name,
                        norm_name=fold(name), synonyms=";".join(syns[:30]),
                        substance_class=row.get("substance_class", ""),
                        status=row.get("status", ""), resolved_by="gsrs")
            self.w.identifier(skey, "UNII", unii, source=key)
            cas = (row.get("cas_number") or "").strip()
            if cas:
                self.w.identifier(skey, "CAS", cas, source=key)
            # Preferred name only, in this pass. See below.
            self.r.add(name, unii)

        # Second pass for synonyms, after every preferred name is registered.
        #
        # Adding a row's synonyms immediately after its own preferred name
        # only orders them WITHIN a row. Across rows it is file order, and
        # Resolver.add is first-writer-wins - so a synonym on an early row
        # beats a preferred name on a later one.
        #
        # That is not hypothetical. METFORMIN C-11, a carbon-11 radiotracer,
        # sits at row 18,440 with a synonym folding to "metformin"; plain
        # Metformin is at row 32,329. The tracer took the name, so ChEMBL's
        # metformin molecule resolved to it - CHEMBL1431 landed on the tracer,
        # the real Metformin node got no ChEMBL id at all, and every ChEMBL
        # annotation for metformin attached to the wrong substance. The salt
        # hierarchy then read "metformin hydrochloride IS_SALT_OF METFORMIN
        # C-11", which is how it was found.
        #
        # Streaming the file twice rather than holding 173k synonym lists:
        # the file is 53 MB and this host has been driven into swap before by
        # keeping rows in memory.
        for row in lake.stream_csv(key, limit=self.limit):
            unii = (row.get("unii") or "").strip()
            name = (row.get("preferred_name") or "").strip()
            if not unii or not name:
                continue
            syns = split_synonyms(row.get("synonyms", ""))
            if not self.wanted(name, *syns):
                continue
            for s in syns:
                self.r.add(s, unii)
        self._done("gsrs", t0, n)

    def load_chembl_molecules(self):
        """ChEMBL is pan-therapeutic where gsrs is a naming registry, so it
        contributes molecules gsrs never lists. Rows that resolve to a known
        UNII attach to that node; the rest get provisional NAME: keys."""
        t0 = self._step("chembl_molecules")
        key = LAKE["molecules"]
        n = 0
        for row in lake.stream_csv(key, limit=self.limit):
            molregno = (row.get("molregno") or "").strip()
            chembl_id = (row.get("chembl_id") or "").strip()
            pref = (row.get("pref_name") or "").strip()
            if not molregno or not chembl_id:
                continue
            # A molecule with no preferred name cannot be resolved or matched
            # by anything downstream; in a slice it is noise.
            if self.slice is not None and not self.wanted(pref):
                continue
            n += 1
            m = self.r.resolve(pref) if pref else None
            # Merge only on a real identifier match. A provisional match merges
            # on the folded name alone, and ChEMBL's pref_name is often a
            # category rather than an identity: "platinum complex" is the
            # preferred name of 248 different molecules, "auranofin analogue"
            # of 54. Trusting those collapsed 45,891 distinct compounds into
            # 22,125 nodes.
            #
            # The cost is that two ChEMBL rows for one drug that gsrs has never
            # heard of now stay separate. That is the right way round: a false
            # merge is silent and permanent, a missed merge is visible as two
            # nodes and fixable by any identifier they share.
            skey = m.key if (m and m.resolved) else f"CHEMBL:{chembl_id}"
            self.w.node("Substance", skey, source=key, name=pref,
                        norm_name=fold(pref),
                        max_phase=_chembl_phase(row.get("max_phase", "")),
                        resolved_by=(m.method if m else "chembl_id"))
            self.w.identifier(skey, "CHEMBL_ID", chembl_id, source=key,
                              match_method=(m.method if m else "structured"))
            mt = (row.get("molecule_type") or "").strip()
            # ChEMBL writes 'Unknown' as a molecule type. Taken at face value
            # it became a Modality node, and every substance of unknown type
            # hung off it as though that were a shared property.
            if mt and not is_placeholder(mt):
                self.w.node("Modality", f"MODALITY:{fold(mt)}", source=key, name=mt)
                self.w.edge("HAS_MODALITY", skey, f"MODALITY:{fold(mt)}", source=key)
            self.molregno_key[molregno] = skey
            self.chembl_mol_key[chembl_id] = skey
        self._done("chembl_molecules", t0, n)

    def load_chembl_synonyms(self):
        """139k names ChEMBL knows that gsrs does not - mostly brand names and
        research codes.

        Registered as aliases, not as UNII mappings: a synonym identifies a
        molregno, and the substance that molregno became may have no UNII. The
        alias tier is consulted last, so a gsrs preferred name always beats a
        ChEMBL research code for the same string.

        Runs after chembl_molecules because it needs molregno_key, which is why
        it cannot be folded into the resolver before finalise().
        """
        t0 = self._step("chembl_synonyms")
        key = LAKE["synonyms"]
        n = 0
        for row in lake.stream_csv(key, limit=self.limit):
            skey = self.molregno_key.get((row.get("molregno") or "").strip())
            syn = (row.get("synonyms") or "").strip()
            if not skey or not syn:
                continue
            n += 1
            self.r.add_alias(syn, skey)
        self.w.sid(key)          # record the read; it emits no rows of its own
        self.stats["chembl_aliases"] = len(self.r.alias)
        self._done("chembl_synonyms", t0, n)

    def load_structures(self):
        """InChIKey - the strongest merge signal there is, because it is the
        chemistry rather than a name."""
        t0 = self._step("chembl_structures")
        key = LAKE["structures"]
        n = 0
        for row in lake.stream_csv(key, limit=self.limit):
            molregno = (row.get("molregno") or "").strip()
            ik = (row.get("standard_inchi_key") or "").strip()
            skey = self.molregno_key.get(molregno)
            if not skey or not ik:
                continue
            n += 1
            self.w.identifier(skey, "INCHIKEY", ik, source=key)
        self._done("chembl_structures", t0, n)

    # ---- targets ---------------------------------------------------------
    def load_targets(self):
        """Target keyed by UniProt accession where one exists.

        chembl_uniprot_mapping joins on the target's CHEMBL id, not on tid, so
        it has to be read first and held. Targets with no UniProt mapping are
        real - cell lines, whole organisms, protein complexes - and keep a
        CHEMBL: key rather than being dropped.
        """
        t0 = self._step("chembl_targets")
        umap_key = LAKE["uniprot_map"]
        uniprot_by_chembl: dict[str, str] = {}
        for row in lake.stream_csv(umap_key):
            ct = (row.get("chembl_target_id") or "").strip()
            acc = (row.get("uniprot_accession") or "").strip()
            if ct and acc:
                uniprot_by_chembl.setdefault(ct, acc)

        self.w.sid(umap_key)     # read into memory only; record it as read

        # Gene symbols must be known BEFORE the Target node is written.
        #
        # HGNC loads later, inside disease.ALL, and used to call node() again
        # to "enrich" the Target with its symbol. That call did nothing: the
        # Writer is append-once, so a second node() on an existing key returns
        # False and drops the properties on the floor. The result was
        # Target.symbol declared, indexed, documented - and null on all 16,624
        # nodes, which reads at query time as "the graph has no EGFR" rather
        # than as a defect.
        #
        # Reading HGNC here costs one pass over 42k rows and makes the symbol
        # available at creation time, which is the only moment it can land.
        hgnc_key = LAKE.get("hgnc") or (
            "Targets_Genomics_Biomarkers/genenames.org/data/complete_set/"
            "hgnc_complete_set.csv")
        symbol_by_acc: dict[str, str] = {}
        for row in lake.stream_csv(hgnc_key):
            sym = (row.get("symbol") or "").strip()
            if not sym:
                continue
            for a in (row.get("uniprot_ids") or "").replace('"', "").split("|"):
                a = a.strip()
                if a:
                    symbol_by_acc.setdefault(a, sym)
        self.stats["hgnc_symbols_available"] = len(symbol_by_acc)

        key = LAKE["targets"]
        n = 0
        for row in lake.stream_csv(key, limit=self.limit):
            tid = (row.get("tid") or "").strip()
            cid = (row.get("chembl_id") or "").strip()
            if not tid or not cid:
                continue
            acc = uniprot_by_chembl.get(cid)
            tkey = f"UNIPROT:{acc}" if acc else f"CHEMBL_TARGET:{cid}"
            n += 1
            self.w.node("Target", tkey, source=key,
                        symbol=symbol_by_acc.get(acc, "") if acc else "",
                        name=row.get("pref_name", ""),
                        organism=row.get("organism", ""),
                        target_type=_no_placeholder(row.get("target_type", "")))
            if acc:
                self.w.identifier(tkey, "UNIPROT", acc, source=key)
                # The accessions that actually became Targets. NCIt's crosswalk
                # names 6,410 proteins and only 3,399 are in this graph; without
                # this set the other 3,011 would be written as identifiers on
                # nodes that do not exist.
                self.target_acc.add(acc)
            self.tid_key[tid] = tkey
            self.chembl_target_key[cid] = tkey
        self._done("chembl_targets", t0, n)

    def load_mechanisms(self):
        """One file gives both TARGETS and HAS_MECHANISM, as facts rather than
        inference: molregno -> tid, plus mechanism_of_action and action_type."""
        t0 = self._step("chembl_mechanisms")
        # action_type vocabulary first so Mechanism nodes carry a description
        at_desc = {}
        for row in lake.stream_csv(LAKE["action_types"]):
            at = (row.get("action_type") or "").strip()
            if at:
                at_desc[at] = row.get("description", "")

        self.w.sid(LAKE["action_types"])   # vocabulary, emits no rows itself
        key = LAKE["mechanisms"]
        n = 0
        for row in lake.stream_csv(key, limit=self.limit):
            skey = self.molregno_key.get((row.get("molregno") or "").strip())
            if not skey:
                continue
            n += 1
            moa = (row.get("mechanism_of_action") or "").strip()
            action = (row.get("action_type") or "").strip()
            if moa:
                mkey = f"MECH:{fold(moa)}"
                self.w.node("Mechanism", mkey, source=key, name=moa,
                            action_type=action)
                for k in self.with_parent(skey):
                    self.w.edge("HAS_MECHANISM", k, mkey, source=key)
            tkey = self.tid_key.get((row.get("tid") or "").strip())
            if tkey:
                for k in self.with_parent(skey):
                    self.w.edge("TARGETS", k, tkey, source=key)
        self._done("chembl_mechanisms", t0, n)

    # ---- driver ----------------------------------------------------------
    def preflight(self):
        """Every S3 key the loaders name, checked before any of them run.

        A mistyped path used to surface as NoSuchKey three minutes into a
        build, after gsrs and chembl had already streamed - and it surfaced as
        a botocore traceback naming a bucket, not as "ontology.ncit_swissprot
        is wrong". Two loaders were added with a guessed directory and both
        died that way. This costs one HEAD per key and turns it into a list.
        """
        import disease as _d, ontology as _o, reference as _r
        missing = []
        for mod in (_d, _o, _r):
            for name, key in getattr(mod, "L", {}).items():
                if not lake.exists(key):
                    missing.append(f"{mod.__name__}.L[{name!r}] -> {key}")
        for name, key in LAKE.items():
            if not lake.exists(key):
                missing.append(f"build.LAKE[{name!r}] -> {key}")
        if missing:
            raise SystemExit("these declared files are not in the lake:\n  "
                             + "\n  ".join(missing))
        print("preflight: every declared key resolves")

    def run(self):
        self.preflight()
        print(f"run_id {self.run_id}"
              f"{'   SLICE: ' + ', '.join(sorted(self.slice)) if self.slice else '   FULL'}")
        print()
        self.load_atc()
        self.load_gsrs()
        self.r.finalise()          # stereo tier needs every name first
        self.load_chembl_molecules()
        self.load_chembl_synonyms()
        self.load_structures()
        # reference needs molregno_key (chembl_molecules), atc_codes (atc) and
        # a finalised resolver - so it cannot move earlier.
        for fn in reference.ALL:
            fn(self)
        self.load_targets()
        self.load_mechanisms()
        # After load_targets, because NCIt attaches to Target accessions that
        # must already exist; after the resolver is finalised, because MeSH
        # names substances and half of them are salts.
        for fn in ontology.ALL:
            fn(self)
        # Disease before products and trials: both match prose against
        # mesh_by_name, which does not exist until load_mesh has run.
        for fn in disease.ALL:
            fn(self)
        for fn in products.ALL:
            fn(self)
        for fn in trials.ALL:
            fn(self)
        for fn in safety.ALL:
            fn(self)
        # Variants need symbol_target (hgnc, inside disease.ALL) and efo_mesh
        # (chembl_indications); literature needs mesh_by_name and a finalised
        # resolver. Both are last because they only read dictionaries the
        # earlier loaders built.
        for fn in variants.ALL:
            fn(self)
        for fn in literature.ALL:
            fn(self)
        man = self.w.close(extra={
            "mode": "slice" if self.slice else "full",
            "slice": sorted(self.slice) if self.slice else None,
            "resolver": self.r.stats(),
            "stats": self.stats,
            "files_read": sorted(lake.READ),
            "timings_sec": {k: round(v, 2) for k, v in self.timings.items()},
        })
        return man


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slice", help="comma-separated substance names")
    ap.add_argument("--all", action="store_true", help="full build")
    ap.add_argument("--limit", type=int, help="max rows per file (debugging)")
    ap.add_argument("--out", default="graph/build")
    ap.add_argument("--max-mem-gb", type=float, default=0,
                    help="self-imposed ceiling; 0 disables")
    a = ap.parse_args()
    if not a.slice and not a.all:
        ap.error("give --slice <names> or --all")

    if a.max_mem_gb:
        # This box also runs Qdrant, which serves the live search API. Without
        # a ceiling the kernel picks the OOM victim, and Qdrant - the larger,
        # older process - is the likely choice. A MemoryError here kills only
        # the build, and says how much it wanted.
        import resource
        cap = int(a.max_mem_gb * 1024 ** 3)
        resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
        print(f"memory ceiling {a.max_mem_gb} GB")

    names = {fold(s) for s in a.slice.split(",")} if a.slice else None
    b = Build(pathlib.Path(a.out), names, a.limit)
    t0 = time.time()
    man = b.run()

    print(f"\nnodes: " + "  ".join(f"{k}={v:,}" for k, v in sorted(man["nodes"].items())))
    print(f"edges: " + "  ".join(f"{k}={v:,}" for k, v in sorted(man["edges"].items())))
    print(f"resolver: {man['resolver']}")
    print(f"\ntotal {time.time() - t0:.1f}s -> {a.out}/")


if __name__ == "__main__":
    main()
