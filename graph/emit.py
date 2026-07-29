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
    "Substance":       ["key", "name", "norm_name", "substance_class", "status",
                        "max_phase", "resolved_by"],
    "Product":         ["key", "name", "brand_name", "agency", "status", "form",
                        "strength"],
    "Identifier":      ["key", "scheme", "value"],
    "Target":          ["key", "symbol", "name", "organism", "target_type"],
    "Disease":         ["key", "name", "vocabulary", "tree_numbers"],
    "Mechanism":       ["key", "name", "action_type"],
    "DrugClass":       ["key", "atc_code", "name", "level"],
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
    "Publication":     ["key", "title", "year", "journal"],
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
    "INDICATED_FOR":     ["src", "dst", "match_method"],
    "ASSOCIATED_WITH":   ["src", "dst", "match_method", "score"],
    "SUBTYPE_OF":        ["src", "dst", "match_method"],
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

    def node(self, label: str, key: str, source: str = "", **props) -> bool:
        """Emit one node. Returns False if the key was already written."""
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
