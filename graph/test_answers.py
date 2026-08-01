#!/usr/bin/env python3
"""Ask the graph real questions and check it answers them.

    python graph/test_answers.py              # everything
    python graph/test_answers.py --only edges
    python graph/test_answers.py -v           # show the failing Cypher

The structural validator proves the graph is well-formed: endpoints resolve,
keys are unique, nothing dangles. It passed on a graph where "what are the
side effects of metformin" returned nothing, because that is not a structural
fault - the reports hang off `metformin hydrochloride` and the parent node is
empty. Well-formed and useless are not mutually exclusive.

This checks the other thing: that the questions people actually ask return
rows. Four layers:

  REACH     every node label can be found the way a caller would find it -
            by norm_name, by symbol, by term, by the full-text index
  EDGE      every relationship type carries a real traversal, anchored on a
            node that exists, in the direction the schema documents
  DRUG      a panel of well-known drugs answered across every question type:
            targets, mechanism, class, indications, products, agencies,
            trials, adverse events, identifiers
  TRAP      the specific ways this graph misleads - salt forms, casing,
            unnormalised values, region vs country

Read-only. A failure prints the query so it can be pasted into Neo4j.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

try:
    from dotenv import load_dotenv
    load_dotenv(pathlib.Path(__file__).resolve().parent.parent
                / "testPipeline" / ".env")
except ImportError:
    pass

URI = os.getenv("NEO4J_URI", "bolt://4.233.210.24:7687")
USER = os.getenv("NEO4J_USER", "neo4j")
PWD = os.getenv("NEO4J_PASSWORD", "")
DB = os.getenv("NEO4J_DATABASE", "biolyt")

_TTY = sys.stdout.isatty()
def _c(code, s): return f"\033[{code}m{s}\033[0m" if _TTY else s
def dim(s):   return _c("2", s)
def bold(s):  return _c("1", s)
def red(s):   return _c("38;5;203", s)
def green(s): return _c("38;5;78", s)
def amber(s): return _c("38;5;214", s)
def blue(s):  return _c("38;5;75", s)

# Well-known drugs, chosen to span the modalities and the sources: small
# molecules, biologics, a salt-only case, an oncology drug, a vaccine-adjacent
# biologic, and one that exists mainly in trials.
DRUGS = ["atorvastatin", "metformin", "pembrolizumab", "ibuprofen",
         "warfarin", "sertraline", "erenumab", "insulin", "amoxicillin",
         "celecoxib", "omeprazole", "tamoxifen"]

GENES = ["EGFR", "BRAF", "TP53", "KRAS", "ALK", "ERBB2"]
DISEASES = ["breast cancer", "epilepsy", "type 2 diabetes", "asthma",
            "hypertension", "Parkinson disease"]


# --------------------------------------------------------------------------
# REACH - can each label be found the way a caller would find it?
REACH = [
    ("Substance", "MATCH (n:Substance {norm_name:'atorvastatin'}) RETURN n.key LIMIT 1"),
    ("Substance/fulltext",
     "CALL db.index.fulltext.queryNodes('entity_names','atorvastatin') "
     "YIELD node WHERE node:Substance RETURN node.key LIMIT 1"),
    ("Target/symbol", "MATCH (n:Target {symbol:'EGFR'}) RETURN n.key LIMIT 1"),
    ("Target/fulltext",
     "CALL db.index.fulltext.queryNodes('entity_names','EGFR') "
     "YIELD node WHERE node:Target RETURN node.key LIMIT 1"),
    ("Disease/fulltext",
     "CALL db.index.fulltext.queryNodes('entity_names','breast cancer') "
     "YIELD node WHERE node:Disease RETURN node.key LIMIT 1"),
    ("Product/fulltext",
     "CALL db.index.fulltext.queryNodes('entity_names','Lipitor') "
     "YIELD node WHERE node:Product RETURN node.key LIMIT 1"),
    ("Company/fulltext",
     "CALL db.index.fulltext.queryNodes('entity_names','Pfizer') "
     "YIELD node WHERE node:Company RETURN node.key LIMIT 1"),
    ("AdverseEvent/term",
     "CALL db.index.fulltext.queryNodes('reaction_terms','nausea') "
     "YIELD node RETURN node.key LIMIT 1"),
    ("ClinicalTrial/title",
     "CALL db.index.fulltext.queryNodes('document_titles','breast cancer') "
     "YIELD node WHERE node:ClinicalTrial RETURN node.key LIMIT 1"),
    ("Publication/title",
     "CALL db.index.fulltext.queryNodes('document_titles','cancer') "
     "YIELD node WHERE node:Publication RETURN node.key LIMIT 1"),
    ("Mechanism/fulltext",
     "CALL db.index.fulltext.queryNodes('entity_names','reductase inhibitor') "
     "YIELD node WHERE node:Mechanism RETURN node.key LIMIT 1"),
    ("DrugClass/atc", "MATCH (n:DrugClass {atc_code:'C10AA'}) RETURN n.key LIMIT 1"),
    ("Country/key", "MATCH (n:Country {key:'COUNTRY:SA'}) RETURN n.name LIMIT 1"),
    ("Region/name", "MATCH (n:Region {name:'MENA/GCC'}) RETURN n.key LIMIT 1"),
    ("RegulatoryAgency/code",
     "MATCH (n:RegulatoryAgency {code:'SFDA'}) RETURN n.key LIMIT 1"),
    ("Variant/gene",
     "MATCH (v:Variant)-[:VARIANT_IN]->(:Target {symbol:'BRAF'}) "
     "RETURN v.key LIMIT 1"),
    ("Identifier/value",
     "MATCH (:Identifier {value:'NCT00000102'}) RETURN 1 LIMIT 1"),
    ("OrganClass", "MATCH (n:OrganClass) WHERE toLower(n.name) CONTAINS 'cardiac' "
                   "RETURN n.key LIMIT 1"),
    ("Patent", "MATCH (n:Patent) RETURN n.key LIMIT 1"),
    ("Exclusivity", "MATCH (n:Exclusivity) RETURN n.key LIMIT 1"),
    ("Approval", "MATCH (n:Approval) RETURN n.key LIMIT 1"),
    ("RegulatoryEvent/recall",
     "MATCH (n:RegulatoryEvent {type:'recall'}) RETURN n.key LIMIT 1"),
    ("Route", "MATCH (n:Route) WHERE toUpper(n.name) CONTAINS 'ORAL' "
              "RETURN n.key LIMIT 1"),
    ("Modality", "MATCH (n:Modality) RETURN n.key LIMIT 1"),
]

# --------------------------------------------------------------------------
# EDGE - one realistic traversal per relationship type, anchored on something
# that exists. Written the way the query guide says to write them.
EDGES = {
 "CONTAINS":
   "MATCH (p:Product)-[:CONTAINS]->(s:Substance {norm_name:'atorvastatin'}) "
   "RETURN p.name LIMIT 3",
 "DEVELOPS":
   "MATCH (c:Company)-[:DEVELOPS]->(p:Product) RETURN c.name, p.name LIMIT 3",
 "APPROVED_BY":
   "MATCH (p:Product)-[:APPROVED_BY]->(a:RegulatoryAgency {code:'FDA'}) "
   "RETURN p.name LIMIT 3",
 "APPROVED_IN":
   "MATCH (p:Product)-[:APPROVED_IN]->(r:Region {name:'MENA/GCC'}) "
   "RETURN p.name LIMIT 3",
 "HAS_APPROVAL":
   "MATCH (p:Product)-[:HAS_APPROVAL]->(a:Approval) RETURN p.name, a.key LIMIT 3",
 "ISSUED_BY":
   "MATCH (x)-[:ISSUED_BY]->(y) RETURN labels(x)[0], labels(y)[0] LIMIT 3",
 "PROTECTED_BY":
   "MATCH (p:Product)-[:PROTECTED_BY]->(pat:Patent) "
   "RETURN p.name, pat.key LIMIT 3",
 "HAS_EXCLUSIVITY":
   "MATCH (p:Product)-[:HAS_EXCLUSIVITY]->(e:Exclusivity) "
   "RETURN p.name, e.key LIMIT 3",
 "BIOSIMILAR_OF":
   "MATCH (a:Product)-[:BIOSIMILAR_OF]->(b:Product) "
   "RETURN a.name, b.name LIMIT 3",
 "HAS_ROUTE":
   "MATCH (p:Product)-[:HAS_ROUTE]->(r:Route) RETURN p.name, r.name LIMIT 3",
 "IN_CLASS":
   "MATCH (s:Substance {norm_name:'atorvastatin'})-[:IN_CLASS]->(c:DrugClass) "
   "RETURN c.atc_code, c.name LIMIT 3",
 "HAS_MODALITY":
   "MATCH (s:Substance)-[:HAS_MODALITY]->(m:Modality) "
   "RETURN s.name, m.name LIMIT 3",
 "IS_SALT_OF":
   "MATCH (s:Substance)-[:IS_SALT_OF]->(p:Substance) "
   "RETURN s.name, p.name LIMIT 3",
 "TARGETS":
   "MATCH (s:Substance)-[:TARGETS]->(t:Target {symbol:'EGFR'}) "
   "RETURN s.name LIMIT 3",
 # Prefix, not equality. ChEMBL annotates the form it was given - the
 # mechanism sits on 'atorvastatin calcium anhydrous', not on 'atorvastatin'.
 # 1,006 parent names are affected for this relationship alone.
 "HAS_MECHANISM":
   "MATCH (s:Substance)-[:HAS_MECHANISM]->(m:Mechanism) "
   "WHERE s.norm_name STARTS WITH 'atorvastatin' RETURN m.name LIMIT 3",
 "INDICATED_FOR":
   "MATCH (s:Substance)-[:INDICATED_FOR]->(d:Disease) "
   "RETURN s.name, d.name LIMIT 3",
 "ASSOCIATED_WITH":
   "MATCH (t:Target {symbol:'TP53'})-[:ASSOCIATED_WITH]->(d:Disease) "
   "RETURN d.name LIMIT 3",
 "SUBTYPE_OF":
   "MATCH (a:Disease)-[:SUBTYPE_OF]->(b:Disease) RETURN a.name, b.name LIMIT 3",
 "SPONSORED_BY":
   "MATCH (t:ClinicalTrial)-[:SPONSORED_BY]->(c:Company) "
   "RETURN t.registry, c.name LIMIT 3",
 "STUDIES":
   "MATCH (t:ClinicalTrial)-[:STUDIES]->(d:Disease) "
   "RETURN t.registry, d.name LIMIT 3",
 "TESTED_IN":
   "MATCH (s:Substance {norm_name:'pembrolizumab'})-[:TESTED_IN]->(t:ClinicalTrial) "
   "RETURN t.registry, t.title LIMIT 3",
 "CONDUCTED_IN":
   "MATCH (t:ClinicalTrial)-[:CONDUCTED_IN]->(c:Country {key:'COUNTRY:SA'}) "
   "RETURN t.registry LIMIT 3",
 "SAME_STUDY_AS":
   "MATCH (a:ClinicalTrial)-[:SAME_STUDY_AS]->(b:ClinicalTrial) "
   "RETURN a.key, b.key LIMIT 3",
 "SUBJECT_OF":
   "MATCH (s:Substance)-[:SUBJECT_OF]->(e:RegulatoryEvent) "
   "WHERE e.type='recall' RETURN s.name, e.name LIMIT 3",
 "HAS_ADVERSE_EVENT":
   "MATCH (s:Substance)-[e:HAS_ADVERSE_EVENT]->(a:AdverseEvent) "
   "WHERE s.norm_name STARTS WITH 'metformin' "
   "RETURN a.term, e.report_count ORDER BY e.report_count DESC LIMIT 3",
 "IN_ORGAN_CLASS":
   "MATCH (a:AdverseEvent)-[:IN_ORGAN_CLASS]->(o:OrganClass) "
   "RETURN a.term, o.name LIMIT 3",
 "VARIANT_IN":
   "MATCH (v:Variant)-[:VARIANT_IN]->(t:Target {symbol:'BRAF'}) "
   "RETURN v.name LIMIT 3",
 "IMPLICATED_IN":
   "MATCH (v:Variant)-[:IMPLICATED_IN]->(d:Disease) "
   "RETURN v.name, d.name LIMIT 3",
 "ABOUT":
   "MATCH (p:Publication)-[:ABOUT]->(d:Disease) RETURN p.title, d.name LIMIT 3",
 "MENTIONS":
   "MATCH (p:Publication)-[:MENTIONS]->(s:Substance) "
   "RETURN p.title, s.name LIMIT 3",
 "IN_REGION":
   "MATCH (c:Country)-[:IN_REGION]->(r:Region) RETURN c.name, r.name LIMIT 3",
 "HAS_IDENTIFIER":
   "MATCH (s:Substance {norm_name:'atorvastatin'})-[:HAS_IDENTIFIER]->(i:Identifier) "
   "RETURN i.scheme, i.value LIMIT 3",
}

# --------------------------------------------------------------------------
# DRUG - every question type, for every drug in the panel. `{d}` is the
# normalised drug name. Written to include salt forms, because that is where
# the safety data lives.
ANY_FORM = "(s.norm_name = '{d}' OR s.norm_name STARTS WITH '{d} ')"

DRUG_Q = {
 "identifiers":
   "MATCH (s:Substance) WHERE " + ANY_FORM +
   " MATCH (s)-[:HAS_IDENTIFIER]->(i:Identifier) RETURN i.scheme LIMIT 5",
 "products":
   "MATCH (s:Substance) WHERE " + ANY_FORM +
   " MATCH (p:Product)-[:CONTAINS]->(s) RETURN p.name LIMIT 5",
 "agencies":
   "MATCH (s:Substance) WHERE " + ANY_FORM +
   " MATCH (p:Product)-[:CONTAINS]->(s)-[:HAS_IDENTIFIER]->() "
   " WITH p MATCH (p)-[:APPROVED_BY]->(a:RegulatoryAgency) "
   " RETURN DISTINCT a.code LIMIT 5",
 "adverse events":
   "MATCH (s:Substance) WHERE " + ANY_FORM +
   " MATCH (s)-[e:HAS_ADVERSE_EVENT]->(a:AdverseEvent) "
   " RETURN a.term ORDER BY e.report_count DESC LIMIT 5",
 "trials":
   "MATCH (s:Substance) WHERE " + ANY_FORM +
   " MATCH (s)-[:TESTED_IN]->(t:ClinicalTrial) RETURN t.registry LIMIT 5",
 "indications":
   "MATCH (s:Substance) WHERE " + ANY_FORM +
   " MATCH (s)-[:INDICATED_FOR]->(d:Disease) RETURN d.name LIMIT 5",
 "ATC class":
   "MATCH (s:Substance) WHERE " + ANY_FORM +
   " MATCH (s)-[:IN_CLASS]->(c:DrugClass) RETURN c.atc_code LIMIT 5",
 "targets":
   "MATCH (s:Substance) WHERE " + ANY_FORM +
   " MATCH (s)-[:TARGETS]->(t:Target) RETURN t.name LIMIT 5",
 "mechanism":
   "MATCH (s:Substance) WHERE " + ANY_FORM +
   " MATCH (s)-[:HAS_MECHANISM]->(m:Mechanism) RETURN m.name LIMIT 5",
}

# Question types that genuinely do not apply to every drug. A biologic has no
# ATC salt form; a drug with no oncology use has no variant link. Absence here
# is recorded, not failed.
SOFT = {"mechanism", "targets", "indications"}

# --------------------------------------------------------------------------
# TRAP - the specific ways this graph misleads. Each asserts the CORRECT
# behaviour, so a regression shows up as a failure.
TRAPS = [
 # Ontologies_Standards holds 83 CSVs and the graph read two of them. These
 # two attach to nodes that already exist and were measured before the loader
 # was written: NCIt 53% of its crosswalk lands on a Target, MeSH 59% of its
 # substances resolve.
 ("NCIt codes reach Target nodes",
  "MATCH (:Identifier {scheme:'NCIT'})<-[:HAS_IDENTIFIER]-(t:Target) "
  "RETURN count(DISTINCT t) AS n", lambda r: r[0]["n"] > 2_000),
 ("MeSH pharmacological actions are a class you can query",
  "MATCH (c:DrugClass {vocabulary:'MeSH Pharmacological Action'}) "
  "RETURN count(c) AS n", lambda r: 300 < r[0]["n"] < 800),
 ("a MeSH action class has drugs in it",
  "MATCH (c:DrugClass {vocabulary:'MeSH Pharmacological Action'}) "
  "WHERE toLower(c.name) CONTAINS 'anticoagulant' "
  "MATCH (s:Substance)-[:IN_CLASS]->(c) RETURN count(DISTINCT s) AS n",
  lambda r: r[0]["n"] > 5),
 # The two classifications must stay separable: a MeSH action carries no ATC
 # code, so filtering on atc_code still sees only the WHO hierarchy.
 ("MeSH action classes never carry an ATC code",
  "MATCH (c:DrugClass {vocabulary:'MeSH Pharmacological Action'}) "
  "WHERE c.atc_code IS NOT NULL AND c.atc_code <> '' RETURN count(c) AS n",
  lambda r: r[0]["n"] == 0),

 # ICD-10 is what hospital coding and claims actually use, and it was in the
 # lake unloaded while ICD-11 was loaded. Loaded WITH its tree, so "everything
 # under C00-C97" is a traversal rather than a string prefix match.
 ("ICD-10 codes are reachable",
  "MATCH (:Identifier {scheme:'ICD10'}) RETURN count(*) AS n",
  lambda r: r[0]["n"] > 8_000),
 ("ICD-10 carries its hierarchy, not just flat codes",
  "MATCH (d:Disease {vocabulary:'ICD-10'})-[:SUBTYPE_OF]->() "
  "RETURN count(*) AS n", lambda r: r[0]["n"] > 5_000),
 ("an ICD-10 leaf walks up to its chapter",
  "MATCH p=(:Identifier {scheme:'ICD10', value:'Z99.9'})<-[:HAS_IDENTIFIER]-()"
  "-[:SUBTYPE_OF*1..4]->(top:Disease) "
  "WHERE NOT (top)-[:SUBTYPE_OF]->() RETURN count(DISTINCT top) AS n",
  lambda r: r[0]["n"] >= 1),
 # A00 'Cholera' matches MeSH by name, so it is keyed MESH:D002771 and its
 # children still say parent_code=A00. Written as read, that edge pointed at a
 # node that does not exist.
 ("an ICD-10 child whose parent matched MeSH is still attached",
  "MATCH (:Identifier {scheme:'ICD10', value:'A00.0'})<-[:HAS_IDENTIFIER]-(c) "
  "MATCH (c)-[:SUBTYPE_OF]->(p) RETURN count(p) AS n",
  lambda r: r[0]["n"] >= 1),

 # "which drugs are approved for epilepsy" returned tocilizumab, bevacizumab
 # and doxycycline, because INDICATED_FOR carried no phase and 86% of ChEMBL's
 # indication rows are investigational. These four assert the phase is there,
 # that it separates the two populations, and that it lands on the source that
 # actually knows.
 ("INDICATED_FOR carries max_phase",
  "MATCH ()-[e:INDICATED_FOR]->() WHERE e.max_phase IS NOT NULL "
  "RETURN count(e) AS n", lambda r: r[0]["n"] > 50_000),
 ("max_phase separates approved from investigational",
  "MATCH ()-[e:INDICATED_FOR]->() WHERE e.max_phase IS NOT NULL "
  "RETURN e.max_phase AS p, count(*) AS n ORDER BY n DESC",
  lambda r: len(r) >= 5 and any(float(x["p"]) == 4 for x in r)
            and sum(x["n"] for x in r if float(x["p"]) == 4)
                < sum(x["n"] for x in r) * 0.3),
 ("phase 4 excludes the drugs that only have trials in the disease",
  "MATCH (s:Substance)-[e:INDICATED_FOR]->(d:Disease) "
  "WHERE toLower(d.name) CONTAINS 'epilep' AND e.max_phase = 4.0 "
  "RETURN collect(DISTINCT s.norm_name) AS names",
  lambda r: not any(n and n.startswith(("tocilizumab", "bevacizumab",
                                        "daratumumab", "doxycycline",
                                        "empagliflozin"))
                    for n in r[0]["names"])),
 ("opentargets edges leave max_phase null rather than lie",
  "MATCH ()-[e:INDICATED_FOR]->() WHERE e.source = 's16' "
  "RETURN count(e) AS total, count(e.max_phase) AS phased",
  lambda r: r[0]["total"] > 10_000 and r[0]["phased"] == 0),
 ("salt forms carry the safety data",
  "MATCH (s:Substance) WHERE s.norm_name STARTS WITH 'metformin' "
  "MATCH (s)-[e:HAS_ADVERSE_EVENT]->() RETURN count(e) AS n", lambda r: r[0]["n"] > 100),
 ("phase is normalised, equality works",
  "MATCH (t:ClinicalTrial {phase:'PHASE3'}) RETURN count(t) AS n",
  lambda r: r[0]["n"] > 50_000),
 ("phase has no unnormalised spellings left",
  "MATCH (t:ClinicalTrial) WHERE t.phase IN ['Phase 3','PHASE 3','3','phase3'] "
  "RETURN count(t) AS n", lambda r: r[0]["n"] == 0),
 ("status is normalised",
  "MATCH (t:ClinicalTrial) WHERE t.status IN ['Completed','completed'] "
  "RETURN count(t) AS n", lambda r: r[0]["n"] == 0),
 ("registry is lowercase only",
  "MATCH (t:ClinicalTrial) WHERE t.registry <> toLower(t.registry) "
  "RETURN count(t) AS n", lambda r: r[0]["n"] == 0),
 ("Target.symbol is populated",
  "MATCH (t:Target) WHERE t.symbol IS NOT NULL RETURN count(t) AS n",
  lambda r: r[0]["n"] > 5000),
 ("symbol is searchable in full-text",
  "CALL db.index.fulltext.queryNodes('entity_names','EGFR') YIELD node "
  "WHERE node:Target RETURN count(node) AS n", lambda r: r[0]["n"] > 0),
 # SFDA returns whole objects where strings belong, and it has done so for
 # four fields found one at a time - route, form, marketing status, and the
 # marketing company, that last one nested with an embedded country. This
 # checks every property on every label rather than the four already known,
 # so a fifth fails the import instead of turning up in a query result.
 ("no property anywhere is a serialised object",
  "MATCH (n) WITH n LIMIT 400000 "
  "WHERE any(p IN keys(n) WHERE toString(n[p]) STARTS WITH '{') "
  "RETURN count(n) AS n", lambda r: r[0]["n"] == 0),
 ("company names are names, not lookup rows",
  "MATCH (c:Company) WHERE c.name STARTS WITH '{' RETURN count(c) AS n",
  lambda r: r[0]["n"] == 0),

 ("Product.form has no JSON blobs",
  "MATCH (p:Product) WHERE p.form STARTS WITH '{' RETURN count(p) AS n",
  lambda r: r[0]["n"] == 0),
 ("Product.form is not the product class",
  "MATCH (p:Product) WHERE p.form IN ['HUMAN','VETERINARY'] RETURN count(p) AS n",
  lambda r: r[0]["n"] == 0),
 # Trial keys. trial_key exists to deduplicate a study registered in its own
 # registry and again in WHO ICTRP, and four registries fell straight through
 # it: ANZCTR writes a bare 14-digit number where WHO writes ACTRN..., CTIS
 # prefixes its own ids with a literal "CTIS" where WHO does not. The same
 # study became two nodes - 3,690 of a 4,000 ANZCTR sample, 6,618 CTIS.
 ("no trial falls through to the TRIAL: fallback namespace",
  "MATCH (t:ClinicalTrial) WHERE t.key STARTS WITH 'TRIAL:' "
  "RETURN count(t) AS n", lambda r: r[0]["n"] == 0),
 ("ANZCTR ids are keyed the way WHO writes them",
  "MATCH (t:ClinicalTrial) WHERE t.registry='anzctr' AND "
  "NOT t.key STARTS WITH 'ACTRN:' RETURN count(t) AS n",
  lambda r: r[0]["n"] == 0),
 ("CTIS is one namespace, not two spellings",
  "MATCH (t:ClinicalTrial) WHERE t.key STARTS WITH 'CTIS:CTIS' "
  "RETURN count(t) AS n", lambda r: r[0]["n"] == 0),

 ("a country is reachable and is not a region",
  "MATCH (c:Country {key:'COUNTRY:SA'})-[:IN_REGION]->(r:Region) "
  "RETURN r.name AS region", lambda r: r and r[0]["region"] == "MENA/GCC"),
 ("Gulf trials are far fewer than the whole MENA region",
  "MATCH (t:ClinicalTrial)-[:CONDUCTED_IN]->(c:Country) "
  "WHERE c.key IN ['COUNTRY:SA','COUNTRY:AE','COUNTRY:QA','COUNTRY:BH',"
  "'COUNTRY:KW','COUNTRY:OM'] RETURN count(DISTINCT t) AS n",
  lambda r: 0 < r[0]["n"] < 50_000),
 # The build validator already proves key uniqueness across all 13.7M nodes,
 # cheaply, against the CSVs. Repeating it here as a global aggregation
 # exhausts Neo4j's memory pool on a 2 vCPU box - so this checks only the
 # collision that actually happened: an Identifier keyed the same as the
 # entity it identifies, which merged a Disease and an Identifier into one
 # node until every identifier was given an ID: prefix.
 ("identifier keys cannot collide with entity keys",
  "MATCH (i:Identifier) WHERE NOT i.key STARTS WITH 'ID:' "
  "RETURN count(i) AS n", lambda r: r[0]["n"] == 0),
 ("a MeSH id is a Disease, and its Identifier is separate",
  "MATCH (d:Disease {key:'MESH:D000544'}) "
  "MATCH (i:Identifier {key:'ID:MESH:D000544'}) "
  "RETURN d.key AS d, i.key AS i", lambda r: bool(r)),

 # The systemic one, found by asking real questions: ChEMBL and FAERS
 # annotate whichever salt or hydrate form the source named, not the parent.
 # Any query anchored on an exact norm_name silently misses them.
 # The parent node must carry the pharmacology itself. ChEMBL and FAERS
 # annotate whichever salt form the source named, so before the rollup
 # `atorvastatin` had no mechanism and `metformin` no adverse events - the
 # names everyone actually asks about answered nothing. An earlier version of
 # this check asserted that broken state and started failing once it was
 # fixed, which is its own lesson: a test written against a bug outlives it.
 ("a drug carries its own mechanism, not only its salt",
  "MATCH (s:Substance {norm_name:'atorvastatin'})-[m:HAS_MECHANISM]->() "
  "RETURN count(m) AS n", lambda r: r[0]["n"] > 0),
 ("a drug carries its own adverse events",
  "MATCH (s:Substance {norm_name:'metformin'})-[e:HAS_ADVERSE_EVENT]->() "
  "RETURN count(e) AS n", lambda r: r[0]["n"] > 1000),
 ("whitespace is stripped from trial titles",
  "MATCH (t:ClinicalTrial) WHERE t.title <> trim(t.title) RETURN count(t) AS n",
  lambda r: r[0]["n"] == 0),
 ("DrugClass.level is a number, not text",
  "MATCH (c:DrugClass) WHERE c.level IS NOT NULL "
  "RETURN valueType(c.level) AS t LIMIT 1",
  lambda r: r and "INTEGER" in str(r[0]["t"]).upper()),
 ("Publication.year is a number",
  "MATCH (p:Publication) WHERE p.year IS NOT NULL "
  "RETURN valueType(p.year) AS t LIMIT 1",
  lambda r: r and "INTEGER" in str(r[0]["t"]).upper()),
 ("report counts are numeric so ordering is true",
  "MATCH ()-[e:HAS_ADVERSE_EVENT]->() WHERE e.report_count IS NOT NULL "
  "RETURN valueType(e.report_count) AS t LIMIT 1",
  lambda r: r and "INTEGER" in str(r[0]["t"]).upper()),
]


class Runner:
    def __init__(self, drv, verbose=False):
        self.drv = drv
        self.verbose = verbose
        self.fails, self.soft, self.passed = [], [], 0

    def q(self, cypher):
        with self.drv.session(database=DB, default_access_mode="READ") as s:
            return [dict(r) for r in s.run(cypher)]

    def check(self, group, name, cypher, ok=None, soft=False):
        try:
            rows = self.q(cypher)
            good = ok(rows) if ok else bool(rows)
        except Exception as e:                               # noqa: BLE001
            self.fails.append((group, name, cypher,
                               f"{type(e).__name__}: {str(e)[:120]}"))
            print(red(f"  ERROR  {name}"))
            print(dim(f"         {str(e)[:110]}"))
            return False
        if good:
            self.passed += 1
            return True
        detail = "0 rows" if not rows else f"unexpected: {rows[:1]}"
        (self.soft if soft else self.fails).append(
            (group, name, cypher, detail))
        print((amber if soft else red)(
            f"  {'SOFT ' if soft else 'FAIL '}  {name}  {dim(detail)}"))
        if self.verbose:
            print(dim("         " + " ".join(cypher.split())[:150]))
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["reach", "edges", "drugs", "traps"])
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    if not PWD:
        sys.exit("NEO4J_PASSWORD not set")

    from neo4j import GraphDatabase
    drv = GraphDatabase.driver(URI, auth=(USER, PWD))
    r = Runner(drv, a.verbose)
    t0 = time.time()

    # Full-text indexes are created by schema.cypher and populate in the
    # background. Querying one before it is ONLINE raises
    # ProcedureCallFailed, so running immediately after an import failed two
    # reachability checks on a graph that was correct seconds later.
    for _ in range(60):
        try:
            st = r.q("SHOW INDEXES YIELD name, type, state "
                     "WHERE type = 'FULLTEXT' RETURN state")
            if st and all(x["state"] == "ONLINE" for x in st):
                break
        except Exception:                                    # noqa: BLE001
            pass
        print(dim("  waiting for the full-text indexes to come online"))
        time.sleep(5)

    if a.only in (None, "reach"):
        print(bold(f"\nREACH  ({len(REACH)} lookups)"))
        for name, cy in REACH:
            r.check("reach", name, cy)

    if a.only in (None, "edges"):
        print(bold(f"\nEDGES  ({len(EDGES)} relationship types)"))
        for name, cy in sorted(EDGES.items()):
            r.check("edge", name, cy)

    if a.only in (None, "drugs"):
        print(bold(f"\nDRUGS  ({len(DRUGS)} drugs x {len(DRUG_Q)} questions)"))
        for d in DRUGS:
            missing = []
            for q, tpl in DRUG_Q.items():
                cy = tpl.format(d=d)
                if not r.check("drug", f"{d}: {q}", cy, soft=q in SOFT):
                    missing.append(q)
            mark = green("ok") if not missing else amber(
                f"missing {', '.join(missing)}")
            print(f"  {blue(d):<24} {mark}")

    if a.only in (None, "traps"):
        print(bold(f"\nTRAPS  ({len(TRAPS)} known ways this graph misleads)"))
        for name, cy, ok in TRAPS:
            if r.check("trap", name, cy, ok):
                print(green(f"  ok     {name}"))

    print()
    print(bold("=" * 70))
    hard = [f for f in r.fails]
    print(bold(f"  {r.passed} passed, {len(hard)} FAILED, "
               f"{len(r.soft)} soft ({time.time()-t0:.0f}s)"))
    if hard:
        print(red("\n  FAILURES"))
        for grp, name, cy, why in hard:
            print(f"    [{grp}] {name} - {why}")
            print(dim("        " + " ".join(cy.split())[:150]))
    if r.soft:
        print(amber(f"\n  SOFT ({len(r.soft)}) - absent, may be legitimate"))
        for grp, name, cy, why in r.soft[:20]:
            print(f"    {name}")
    drv.close()
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
