"""Node and edge tables, written to CSV as the build runs.

The graph is built to files, not to a database. That is what makes the logic
testable without infrastructure: every question - did atorvastatin resolve to
one node, do the patents attach to real products - is answered by reading a
table.

CSV rather than parquet on purpose. `neo4j-admin database import` consumes CSV
directly, so the same files that get validated here are the files that get
loaded later; no format conversion sits in between to go wrong. It also means
no pyarrow on a box that is already tight on memory.

Columns are declared per label rather than discovered from the data. Discovery
would mean buffering every row to learn the union of keys - 2.9M substances
would not fit - and a declared schema also catches a loader inventing a field
that nothing reads.
"""
from __future__ import annotations

import csv
import re
import json
import pathlib
from dataclasses import dataclass, field

# Provenance carried by every row. `source` is a short id, not the S3 key it
# stands for; the id -> key table is written into manifest.json.
#
# The key itself is ~100 characters and would repeat on all ~26M rows - about
# 2.2 GB of the 3.9 GB output, and the same again in Neo4j's property store,
# to say "Regulatory_Approvals/health-products.canada.ca/..." over and over.
# A three-character id costs nothing and the manifest keeps it readable.
PROV = ["source", "run_id"]

NODE_COLUMNS: dict[str, list[str]] = {
    "Substance":       ["key", "name", "norm_name", "synonyms",
                        "substance_class", "status", "max_phase",
                        "resolved_by"],
    "Product":         ["key", "name", "brand_name", "agency", "status", "form",
                        "strength"],
    "Identifier":      ["key", "scheme", "value"],
    "Target":          ["key", "symbol", "name", "organism", "target_type"],
    # `synonyms` is what makes a disease findable. MeSH stores the formal
    # inverted heading - "Carcinoma, Non-Small-Cell Lung" - and nobody types
    # that. The entry terms that do get typed ("NSCLC", "lung cancer") were
    # read during the build to match against, then discarded, so a full-text
    # search for NSCLC returned Target nodes and "lung cancer" returned
    # companies with it in their name.
    "Disease":         ["key", "name", "synonyms", "vocabulary",
                        "tree_numbers"],
    "Mechanism":       ["key", "name", "action_type"],
    "DrugClass":       ["key", "atc_code", "name", "level", "vocabulary"],
    "Modality":        ["key", "name"],
    "Route":           ["key", "name"],
    "Region":          ["key", "name"],
    "Country":         ["key", "iso2", "name"],
    "RegulatoryAgency": ["key", "code", "name", "country", "region"],
    "Company":         ["key", "name", "raw_names"],
    "Approval":        ["key", "date", "type", "status", "agency"],
    "ClinicalTrial":   ["key", "registry", "title", "status", "phase",
                        "study_type", "enrollment", "start_date"],
    "Patent":          ["key", "patent_no", "expire_date", "use_code",
                        "use_definition", "drug_substance_flag",
                        "drug_product_flag"],
    "Exclusivity":     ["key", "code", "date", "definition"],
    "RegulatoryEvent": ["key", "type", "name", "status", "reason",
                        "start_date", "end_date", "url"],
    "AdverseEvent":    ["key", "term"],
    # `source_db`, not `source`: every row already carries a `source` from PROV,
    # and declaring it again put the column in the file twice. csv.DictWriter
    # accepts duplicate fieldnames without complaint, so the build succeeded and
    # neo4j-admin rejected the header 786ms into the import - after the previous
    # store had already been overwritten.
    "Publication":     ["key", "title", "year", "journal", "doi", "pmid",
                        "source_db", "preprint"],
    # One node per variant. Keyed by ClinVar VariationID where there is
    # one, COSMIC MutationID otherwise - the two catalogues do not share
    # an identifier, so a variant in both is two nodes rather than a
    # guessed merge on genomic position.
    "Variant":         ["key", "name", "variant_type", "gene_symbol",
                        "clinical_significance", "catalogue",
                        "consequence"],
    # MedDRA System Organ Class - the top of the reaction hierarchy.
    # AdverseEvent is otherwise 6,913 flat terms with no way to ask for
    # "any cardiac event".
    "OrganClass":      ["key", "name"],
}

# `match_method` distinguishes a fact the source stated (structured) from one
# this code inferred by matching prose against a dictionary (exact/synonym).
# Without it the two are indistinguishable downstream and precision cannot be
# measured.
EDGE_COLUMNS: dict[str, list[str]] = {
    "CONTAINS":          ["src", "dst", "match_method"],
    "DEVELOPS":          ["src", "dst", "match_method"],
    "HAS_IDENTIFIER":    ["src", "dst", "match_method"],
    "TARGETS":           ["src", "dst", "match_method"],
    "HAS_MECHANISM":     ["src", "dst", "match_method"],
    "IN_CLASS":          ["src", "dst", "match_method"],
    "HAS_MODALITY":      ["src", "dst", "match_method"],
    "HAS_ROUTE":         ["src", "dst", "match_method"],
    "INDICATED_FOR":     ["src", "dst", "match_method", "max_phase"],
    "ASSOCIATED_WITH":   ["src", "dst", "match_method", "score"],
    "SUBTYPE_OF":        ["src", "dst", "match_method"],
    "IS_SALT_OF":        ["src", "dst", "match_method"],
    "IN_REGION":         ["src", "dst", "match_method"],
    # Publication. ABOUT and MENTIONS were on the schema diagram but
    # never declared here, so emitting either raised KeyError - the
    # diagram promised two relationships the code refused to write.
    "ABOUT":             ["src", "dst", "match_method"],
    "MENTIONS":          ["src", "dst", "match_method"],
    # Variant.
    "VARIANT_IN":        ["src", "dst", "match_method"],
    "IMPLICATED_IN":     ["src", "dst", "match_method", "significance"],
    # AdverseEvent hierarchy.
    "IN_ORGAN_CLASS":    ["src", "dst", "match_method"],
    "HAS_APPROVAL":      ["src", "dst", "match_method"],
    "APPROVED_BY":       ["src", "dst", "match_method"],
    "APPROVED_IN":       ["src", "dst", "match_method"],
    "ISSUED_BY":         ["src", "dst", "match_method"],
    "SPONSORED_BY":      ["src", "dst", "match_method"],
    "STUDIES":           ["src", "dst", "match_method"],
    "TESTED_IN":         ["src", "dst", "match_method"],
    "CONDUCTED_IN":      ["src", "dst", "match_method"],
    "SAME_STUDY_AS":     ["src", "dst", "match_method"],
    "PROTECTED_BY":      ["src", "dst", "match_method"],
    "HAS_EXCLUSIVITY":   ["src", "dst", "match_method"],
    "SUBJECT_OF":        ["src", "dst", "match_method"],
    "BIOSIMILAR_OF":     ["src", "dst", "match_method"],
    "HAS_ADVERSE_EVENT": ["src", "dst", "match_method", "report_count",
                          "serious_count", "death_count"],
}


# A declared column that collides with PROV lands in the file twice. Checked
# here rather than discovered at import: csv.DictWriter takes duplicate
# fieldnames silently, so the build looks fine and neo4j-admin fails on a
# header it cannot parse, by which point the old store is already gone.
for _label, _cols in NODE_COLUMNS.items():
    _clash = set(_cols) & set(PROV)
    assert not _clash, f"{_label} declares {_clash}, which PROV already adds"
for _etype, _cols in EDGE_COLUMNS.items():
    _clash = set(_cols) & set(PROV)
    assert not _clash, f"{_etype} declares {_clash}, which PROV already adds"


# A serialised lookup row where a value belongs.
#
# SFDA's API returns whole objects for fields that should be strings, and it
# has done so for four different fields found one at a time: administration
# route, dosage form, marketing status, and the marketing company - the last
# nested, carrying an embedded country object with its own nameEn.
#
# Caught at the Writer rather than in each loader, because finding the fifth
# by noticing it in a query result is not a process. A value that starts with
# "{" and carries a nameEn is never a real name; the English name is the
# value. Anything else is left exactly as it is.
_LOOKUP_NAME = re.compile(r'"name[eE]n"\s*:\s*"([^"]*)"')


def _unwrap_lookup(v: str) -> str:
    if not v.startswith("{") or "ame" not in v:
        return v
    m = _LOOKUP_NAME.search(v)
    return m.group(1).strip() if m and m.group(1).strip() else v


@dataclass
class Writer:
    """Collects nodes and edges, deduplicates, writes one CSV per label/type.

    Deduplication is in-memory on the key, first writer wins. That ordering is
    deliberate and matches the resolver: the most authoritative source is
    loaded first, so its properties are the ones kept. A later source seeing
    the same key contributes its edges but does not overwrite the node.

    Memory is a set of key strings per label - ~2.9M for Substance, tens of MB.
    The rows themselves stream to disk as they arrive and are never held.
    """
    outdir: pathlib.Path
    run_id: str
    _files: dict = field(default_factory=dict)
    _writers: dict = field(default_factory=dict)
    _seen: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)
    dropped: dict = field(default_factory=dict)
    _sources: dict = field(default_factory=dict)

    def sid(self, source: str) -> str:
        """Short id for an S3 key, allocated on first use."""
        if not source:
            return ""
        s = self._sources.get(source)
        if s is None:
            s = f"s{len(self._sources):02d}"
            self._sources[source] = s
        return s

    def __post_init__(self):
        (self.outdir / "nodes").mkdir(parents=True, exist_ok=True)
        (self.outdir / "edges").mkdir(parents=True, exist_ok=True)

    def _writer(self, kind: str, name: str, columns: list[str]):
        k = (kind, name)
        if k not in self._writers:
            path = self.outdir / kind / f"{name}.csv"
            f = path.open("w", encoding="utf-8", newline="")
            w = csv.DictWriter(f, fieldnames=columns + PROV,
                               extrasaction="ignore")
            w.writeheader()
            self._files[k] = f
            self._writers[k] = w
            self._seen[k] = set()
            self.counts[k] = 0
            self.dropped[k] = 0
        return self._writers[k], self._seen[k], k

    @staticmethod
    def _clean(props: dict) -> dict:
        """Trim whitespace and flatten embedded newlines on every value.

        Applied centrally rather than per loader, because a source that pads a
        field is not a loader's problem to remember - and the cost of missing
        it is invisible: " Tablet" and "Tablet" are two values to a GROUP BY,
        so a count silently splits in two. This found 4,091 trial titles and
        257 event names carrying stray whitespace.

        Only whitespace is touched. Placeholder-looking values are left alone
        on purpose: Country.iso2 'NA' is Namibia, not a missing value, and a
        blanket rule for 'NA' would delete it.
        """
        out = {}
        for k, v in props.items():
            if isinstance(v, str):
                v = " ".join(v.split()) if ("\n" in v or "\t" in v
                                            or "\r" in v) else v.strip()
                v = _unwrap_lookup(v)
            out[k] = v
        return out

    def node(self, label: str, key: str, source: str = "", **props) -> bool:
        """Emit one node. Returns False if the key was already written."""
        props = self._clean(props)
        cols = NODE_COLUMNS.get(label)
        if cols is None:
            raise KeyError(f"unknown node label {label!r} - add it to NODE_COLUMNS")
        if not key:
            self.dropped[("nodes", label)] = self.dropped.get(("nodes", label), 0) + 1
            return False
        w, seen, k = self._writer("nodes", label, cols)
        if key in seen:
            return False
        seen.add(key)
        w.writerow({**props, "key": key, "source": self.sid(source),
                    "run_id": self.run_id})
        self.counts[k] += 1
        return True

    def edge(self, etype: str, src: str, dst: str, match_method: str = "structured",
             source: str = "", **props) -> bool:
        """Emit one edge. Endpoints are node keys, not database ids - which is
        what lets edges be written before the nodes they point at exist."""
        cols = EDGE_COLUMNS.get(etype)
        if cols is None:
            raise KeyError(f"unknown edge type {etype!r} - add it to EDGE_COLUMNS")
        if not src or not dst:
            self.dropped[("edges", etype)] = self.dropped.get(("edges", etype), 0) + 1
            return False
        w, seen, k = self._writer("edges", etype, cols)
        ident = (src, dst)
        if ident in seen:
            return False
        seen.add(ident)
        w.writerow({**props, "src": src, "dst": dst,
                    "match_method": match_method,
                    "source": self.sid(source), "run_id": self.run_id})
        self.counts[k] += 1
        return True

    def identifier(self, entity_key: str, scheme: str, value: str,
                   source: str = "", match_method: str = "structured") -> str:
        """Emit an Identifier node and the edge attaching it to an entity.

        Identifier keys carry an "ID:" prefix, and that prefix is the whole
        reason this method exists. `neo4j-admin import` resolves every edge
        endpoint in one id space by default, so a Substance keyed
        "UNII:A0JWA85V8F" and its Identifier keyed the same string are one
        node, not two - the identifier silently absorbs the substance and every
        edge meant for one lands on the other. The same collision applied to
        MESH: (Disease), UNIPROT: (Target) and NCT: (ClinicalTrial).

        Every identifier goes through here so no loader can reintroduce it.
        """
        if not entity_key or not value:
            return ""
        ikey = f"ID:{scheme}:{value}"
        self.node("Identifier", ikey, source=source, scheme=scheme, value=value)
        self.edge("HAS_IDENTIFIER", entity_key, ikey,
                  match_method=match_method, source=source)
        return ikey

    def close(self, extra: dict | None = None) -> dict:
        for f in self._files.values():
            f.close()
        manifest = {
            "run_id": self.run_id,
            "nodes": {n: c for (kind, n), c in self.counts.items() if kind == "nodes"},
            "edges": {n: c for (kind, n), c in self.counts.items() if kind == "edges"},
            "dropped_empty_key": {f"{kind}/{n}": c
                                  for (kind, n), c in self.dropped.items() if c},
            # The lookup that makes the short `source` ids readable. Without
            # this the provenance column is meaningless, so it is written
            # alongside the data rather than derived later.
            "sources": {v: k for k, v in self._sources.items()},
            **(extra or {}),
        }
        (self.outdir / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8")
        return manifest
