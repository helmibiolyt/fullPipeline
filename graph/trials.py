"""ClinicalTrial, across ten registries.

The hard problem here is not loading ten files, it is that the same study is
registered in several of them. WHO ICTRP alone is 1.01M rows and is itself an
aggregation of the other registries, so a naive load produces roughly two
million trial nodes for a world that has around half that many trials.

The fix is that a trial's key is derived from its registry id by one function,
`trial_key`, used by every loader. WHO's TrialID for a CT.gov study is
"NCT00000102", which canonicalises to exactly the key clinicaltrials_all.csv
produced - so the WHO row merges into the existing node instead of duplicating
it, and the native record wins on properties because the Writer keeps the first
writer. SAME_STUDY_AS is then only needed for genuinely different ids for one
study, which is what WHO's cross-reference columns carry.

The second problem is that conditions and interventions are free prose. This
module resolves them against the dictionaries built earlier, and **only accepts
confident matches**: an intervention that does not resolve to a known substance
is dropped, never turned into a provisional node. Product loaders can afford
provisional keys because an ingredient list is a short controlled field; 1.6M
rows of "Drug: placebo comparator, 10mg tablet, twice daily" cannot.
"""
from __future__ import annotations

import json
import re

import countries
import lake
from normalise import (condition_parts, condition_variants, fold,
                       is_placeholder, norm_company, squash)

# --------------------------------------------------------------------------
# Ten registries describe the same three facts in their own words. Left raw,
# `phase` held 112 distinct values for what are really six, `status` held 46
# for about ten, and `registry` spelled itself two ways in four cases. Every
# one of those splits a GROUP BY and makes an equality filter silently miss:
# `{phase:'Phase 3'}` matched 10,268 rows out of roughly 57,000 real phase-3
# trials.
#
# These map to a canonical form. The registry's own wording is not preserved -
# provenance already records which file each row came from, and the raw value
# has no analytical use once it is understood.

_PHASE_PAT = [
    # Order matters: the combined phases must be tested before the single
    # ones, or "PHASE1 | PHASE2" is read as phase 1.
    (r"\b(early[\s_-]*phase[\s_-]*1|early[\s_-]*phase[\s_-]*i)\b", "EARLY_PHASE1"),
    (r"(phase\s*1\s*[|/&+,-]\s*(phase\s*)?2|1\s*-\s*2\b|i\s*/\s*ii\b)", "PHASE1_PHASE2"),
    (r"(phase\s*2\s*[|/&+,-]\s*(phase\s*)?3|2\s*-\s*3\b|ii\s*/\s*iii\b)", "PHASE2_PHASE3"),
    (r"(phase\s*3\s*[|/&+,-]\s*(phase\s*)?4|3\s*-\s*4\b|iii\s*/\s*iv\b)", "PHASE3_PHASE4"),
    (r"(phase[\s_-]*4|phase[\s_-]*iv|therapeutic use)", "PHASE4"),
    (r"(phase[\s_-]*3|phase[\s_-]*iii|therapeutic confirmatory)", "PHASE3"),
    (r"(phase[\s_-]*2|phase[\s_-]*ii|therapeutic exploratory)", "PHASE2"),
    (r"(phase[\s_-]*1|phase[\s_-]*i\b|human pharmacology)", "PHASE1"),
    # "phase 0" spelled out is a real exploratory micro-dosing study. A BARE
    # "0" is not: registries write it for "no phase applies", which is why it
    # lives in _PHASE_NONE below. An earlier version had `^0$` -> PHASE0 here
    # and _PHASE_NONE tested first, so the rule could never fire and the graph
    # holds zero PHASE0 trials.
    (r"phase[\s_-]*0(?![0-9])", "PHASE0"),
    (r"^\s*4\s*$", "PHASE4"),
    (r"^\s*3\s*$", "PHASE3"),
    (r"^\s*2\s*$", "PHASE2"),
    (r"^\s*1\s*$", "PHASE1"),
]

#: What a trial with no phase carries. Every ClinicalTrial has this property.
#:
#: Stored rather than left off, because an absent property and a phase of "not
#: applicable" look identical in the browser and in `IS NULL`, and they are not
#: the same statement. An observational study HAS no phase; a registry that
#: simply never filled the field is a gap. Both read as NA today - separating
#: them would need the loaders to distinguish a blank from an "N/A", which no
#: registry makes easy.
#:
#: Filter with `t.phase <> 'NA'` for trials that carry a real phase.
PHASE_NA = "NA"

# Values meaning "no phase applies". Mapped to NA, not dropped.
_PHASE_NONE = {
    "na", "n/a", "n.a.", "not applicable", "not specified", "none",
    "unknown", "not available", "others", "other", "0", "",
}

_STATUS_MAP = {
    "completed": "COMPLETED", "complete": "COMPLETED", "ended": "COMPLETED",
    "finished": "COMPLETED", "trial now transitioned": "COMPLETED",
    "recruiting": "RECRUITING", "ongoing recruiting": "RECRUITING",
    "authorised recruiting": "RECRUITING", "open public recruiting": "RECRUITING",
    "not yet recruiting": "NOT_YET_RECRUITING", "pending": "NOT_YET_RECRUITING",
    "authorised recruitment pending": "NOT_YET_RECRUITING",
    "not recruiting": "ACTIVE_NOT_RECRUITING",
    "active not recruiting": "ACTIVE_NOT_RECRUITING",
    "ongoing": "ACTIVE_NOT_RECRUITING",
    "ongoing recruitment ended": "ACTIVE_NOT_RECRUITING",
    "enrolling by invitation": "ENROLLING_BY_INVITATION",
    "terminated": "TERMINATED", "prematurely ended": "TERMINATED",
    "stopped": "TERMINATED", "stopped early": "TERMINATED",
    "withdrawn": "WITHDRAWN", "not authorised": "WITHDRAWN",
    "suspended": "SUSPENDED", "temporary halt": "SUSPENDED",
    "temporarily halted": "SUSPENDED", "deferred": "SUSPENDED",
    "unknown": "", "withheld": "", "not available": "",
}


def norm_phase(raw: str) -> str:
    """112 registry spellings -> a canonical phase, or "NA" if none applies."""
    s = (raw or "").strip()
    if s.lower() in _PHASE_NONE:
        return PHASE_NA
    low = s.lower()
    # The Indian registry writes every phase on one line with Yes/No against
    # each; only the Yes matters.
    if "yes" in low and "phase" in low:
        picked = [tag for pat, tag in _PHASE_PAT[4:9]
                  if re.search(pat.replace(r"\b", ""), low)
                  and re.search(pat.split(")")[0].strip("(") + r"[^:]*:\s*yes",
                                low)]
        if picked:
            return picked[0]
    for pat, tag in _PHASE_PAT:
        if re.search(pat, low):
            return tag
    # Prose that mentions no phase at all - "Treatment study", a purpose, a
    # sentence. Unreadable is still "no phase we can state".
    return PHASE_NA


def norm_status(raw: str) -> str:
    """46 registry spellings -> one of ten canonical values, or ""."""
    s = (raw or "").strip()
    if not s:
        return ""
    key = re.sub(r"[^a-z0-9 ]+", " ", s.lower())
    key = " ".join(key.split())
    if key in _STATUS_MAP:
        return _STATUS_MAP[key]
    # Registry-specific prose that still carries the state.
    for frag, tag in (("no longer in eu", "WITHDRAWN"),
                      ("not available", ""), ("available", "")):
        if frag in key:
            return tag
    return s.upper().replace(" ", "_").replace("-", "_")


# study_type, which was the one enum nobody normalised. Four spellings of
# "interventional" carried 679,141 trials between them and
# `{study_type:'INTERVENTIONAL'}` reached 455,213 of them - the same silent
# two-thirds miss that phase, status and registry each had before their maps.
#
# The harder half of the problem is that three registries put three DIFFERENT
# concepts in a column they all call "study type", across 1,188 distinct
# values:
#
#   ct.gov, WHO   the study type proper       Interventional / Observational
#   ISRCTN        the study's PURPOSE         Treatment / Screening / Efficacy
#   ChiCTR        a design family             Prognosis study / Basic Science
#   CTRI          the intervention MODALITY   Drug / Ayurveda / Medical Device
#                 concatenated without a separator: "DrugSurgical/Anesthesia"
#
# So `study_type` is kept to what the name promises - is an intervention
# assigned or only observed - with four values and nothing else. Registry text
# is mapped only where the term is DEFINITIONALLY one or the other: a cohort
# study assigns nothing, and naming the modality you administer presupposes
# you administer something. Purpose words do not decide it - a screening study
# can be either - so they stay NA rather than being guessed into a bucket.
#
# Phase looked like it could arbitrate the ambiguous 6% and cannot: CTRI writes
# no phase for anything, so its 0% means "this registry omits the field", not
# "observational". Measured before relying on it.
#
# Nothing is discarded. `study_type_raw` keeps the registry's own string, which
# is where CTRI's traditional-medicine modalities (Ayurveda, Siddha, Unani,
# Homeopathy) and ISRCTN's purposes survive.

#: What a trial whose type cannot be decided carries.
STUDY_TYPE_NA = "NA"

_STUDY_TYPE_MAP = {
    "interventional": "INTERVENTIONAL",
    "intervention": "INTERVENTIONAL",
    "interventional study": "INTERVENTIONAL",
    "interventional clinical trial of medicinal product": "INTERVENTIONAL",
    "interventional trial": "INTERVENTIONAL",
    "observational": "OBSERVATIONAL",
    "observational study": "OBSERVATIONAL",
    "observational invasive": "OBSERVATIONAL",
    "observational non invasive": "OBSERVATIONAL",
    "expanded access": "EXPANDED_ACCESS",
}

# Designs that observe and assign nothing. ChiCTR and CTRI wording, including
# ChiCTR's two misspellings, which are in the register itself and not ours to
# correct silently.
_OBSERVATIONAL_DESIGN = (
    "cross sectional", "cohort study", "case control", "follow up study",
    "epidemilogical", "epidemiological", "cause relative factors",
    "prognosis study", "natural history", "post marketing surveillance",
    "registry study", "case series", "case report",
)

# Abbreviations matched as a whole token, never as a substring: "pms" inside a
# longer word would be a silent miscategorisation, and this list is checked
# against text no one has seen yet on the next scrape.
_OBSERVATIONAL_TOKEN = {"pms", "pmos", "rwe"}

# CTRI's modality vocabulary. Naming what you administer presupposes that
# something is administered, so each of these implies an interventional study.
# Matched as a PREFIX because CTRI concatenates them with no separator -
# "DrugAyurvedaPreventive" is one cell.
_INTERVENTIONAL_MODALITY = (
    "drug", "ayurveda", "homeopathy", "unani", "siddha", "yoga",
    "naturopathy", "dentistry", "physiotherapy", "surgical anesthesia",
    "medical device", "nutraceutical", "biological", "behavioral",
    "behavioural", "vaccine", "radiation therapy", "ba be", "bioequivalence",
    "preventive", "treatment study",
)


def _st_key(raw: str) -> str:
    """Lowercase, punctuation to spaces, runs collapsed. "Surgical/Anesthesia"
    and "BA/BE" become "surgical anesthesia" and "ba be"."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (raw or "").lower()).split())


def norm_study_type(raw: str) -> str:
    """A registry's study-type text -> INTERVENTIONAL, OBSERVATIONAL,
    EXPANDED_ACCESS or NA. Never invents a fifth value: an unrecognised
    string is NA and survives verbatim in study_type_raw."""
    key = _st_key(raw)
    if not key:
        return STUDY_TYPE_NA
    if key in _STUDY_TYPE_MAP:
        return _STUDY_TYPE_MAP[key]
    # Substring, not equality: WHO and CTIS write "Interventional clinical
    # trial of medicinal product" and a dozen longer variants of it.
    if "expanded access" in key:
        return "EXPANDED_ACCESS"
    if "observational" in key:
        return "OBSERVATIONAL"
    if any(d in key for d in _OBSERVATIONAL_DESIGN):
        return "OBSERVATIONAL"
    if _OBSERVATIONAL_TOKEN & set(key.split()):
        return "OBSERVATIONAL"
    if "intervention" in key:
        return "INTERVENTIONAL"
    if key.startswith(_INTERVENTIONAL_MODALITY):
        return "INTERVENTIONAL"
    # Purpose words (Treatment, Screening, Quality of life, Efficacy, Other,
    # Not Specified) and anything unseen. A purpose does not decide the type.
    return STUDY_TYPE_NA


# One registry, two names. Case folding alone leaves these split, because WHO
# writes the registry's full title where the native file writes its short one.
_REGISTRY_ALIAS = {
    "clinical trials information system": "ctis",
    "eu clinical trials register": "eu_ctr",
    "eudract": "eu_ctr",
    "german clinical trials register": "drks",
    "clinicaltrials.gov ": "clinicaltrials.gov",
    "nct": "clinicaltrials.gov",
}


def norm_registry(raw: str) -> str:
    """Registry names differ by case across sources - anzctr/ANZCTR,
    chictr/ChiCTR, ctri/CTRI - and by wording where WHO uses the full title.
    Lowercase short form is the canonical value."""
    s = " ".join((raw or "").split()).lower()
    return _REGISTRY_ALIAS.get(s, s)

L = {
    "ctgov":  "Clinical_Trials_Pipeline_Intelligence/clinicaltrials.gov/clinicaltrials_data/clinicaltrials_all.csv",
    "who":    "Clinical_Trials_Pipeline_Intelligence/trialsearch.who.int/who_trials_csv/who_trials.csv",
    "euctr":  "Clinical_Trials_Pipeline_Intelligence/clinicaltrialsregister.eu/eu_ctr_trials/eu_ctr_all_trials.csv",
    "chictr": "Clinical_Trials_Pipeline_Intelligence/chictr.org.cn/chictr_trails2/chictr_detailed.csv",
    "anzctr": "Clinical_Trials_Pipeline_Intelligence/anzctr.org.au/anzctr_trials/anzctr_trials.csv",
    "isrctn": "Clinical_Trials_Pipeline_Intelligence/isrctn.com/isrctn_trials/ISRCTN_search_results.csv",
    "ctis":   "Clinical_Trials_Pipeline_Intelligence/euclinicaltrials.eu/ctis_data/CTIS_trials_20260622.csv",
    "ctri":   "Clinical_Trials_Pipeline_Intelligence/ctri.nic.in/ctri_trials/ctri_trials.csv",
    "jrct":   "Clinical_Trials_Pipeline_Intelligence/jrct.mhlw.go.jp/jrct_trials/jrct_list.csv",
    "cris":   "Clinical_Trials_Pipeline_Intelligence/cris.nih.go.kr/cris_trials/cris_trials.csv",
}

# Registry prefixes, longest first so ISRCTN is tested before any shorter
# pattern could claim it.
_PREFIX = [
    ("ISRCTN", "ISRCTN"), ("ACTRN", "ACTRN"), ("CHICTR", "CHICTR"),
    ("NCT", "NCT"), ("CTRI", "CTRI"), ("JRCT", "JRCT"), ("UMIN", "UMIN"),
    ("IRCT", "IRCT"), ("PACTR", "PACTR"), ("DRKS", "DRKS"), ("NTR", "NTR"),
    ("KCT", "KCT"), ("TCTR", "TCTR"), ("RBR", "RBR"), ("SLCTR", "SLCTR"),
    ("RPCEC", "RPCEC"), ("LBCTR", "LBCTR"), ("ITMCTR", "ITMCTR"),
    ("CRIS", "CRIS"), ("REBEC", "RBR"),
]
_EUDRACT = re.compile(r"(\d{4}-\d{6}-\d{2})")
_CTIS = re.compile(r"^(?:CTIS)?(\d{4}-\d{6}-\d{2}-\d{2})$")

# Ids a registry writes WITHOUT the prefix that WHO ICTRP writes WITH.
#
# This is where deduplication was silently failing. ANZCTR's own file gives
# "12605000001695" while WHO gives "ACTRN12605000001695", so the same study
# landed as TRIAL:12605000001695 and ACTRN:ACTRN12605000001695 - two nodes,
# identical titles. 3,690 of a 4,000-row sample were duplicated that way, and
# 6,618 CTIS studies likewise, because CTIS's own file prefixes the id with a
# literal "CTIS" that the bare-format pattern rejected.
#
# Anchored patterns, not prefixes: a bare 14-digit number means nothing on its
# own, so the rule has to be the exact shape or it will claim ids belonging to
# a registry added later.
_BARE = [
    (re.compile(r"^\d{14}$"), "ACTRN", "ACTRN"),   # ANZCTR, WHO adds ACTRN
    (re.compile(r"^NL-OMON\d+$"), "NL-OMON", ""),  # Dutch, self-identifying
    # The trailing letter is a split registration: PER-002-99-A and -B
    # are two arms of one study, and both are real REPEC ids.
    (re.compile(r"^PER-\d{3}-\d{2}(-[A-Z])?$"), "REPEC", ""),  # Peru
]


def trial_key(raw: str) -> str:
    """Canonical node key for any registry id, from any source.

    This is the deduplication mechanism. Everything else follows from every
    loader calling it on the same study id and getting the same string.

    One rule, NAMESPACE:VALUE, applied to every registry - which means 19 of
    the 22 repeat their own prefix, because their ids already carry it:
    NCT:NCT01045135, ISRCTN:ISRCTN12345678. 933,232 of 1,048,841 keys look like
    that. It was considered and kept.

    Stripping the prefix where the id already self-identifies would read better
    and is safe - those ids are globally unique - but it makes the rule
    conditional, and a conditional rule is what breaks when the 23rd registry
    is added and nobody remembers which branch it falls in. EUCTR and CTIS need
    the namespace because "2004-000010-11" identifies nothing on its own, so
    the exception would have to exist either way.

    The unprefixed value is not lost: the Identifier node carries
    value="NCT01045135", and Identifier.value is indexed.
    """
    s = re.sub(r"\s+", "", (raw or "").strip().upper())
    if not s:
        return ""
    if s.startswith("JPRN-"):            # WHO's prefix for all Japanese registries
        s = s[5:]
    m = _CTIS.match(s)                   # 4 groups = CTIS, before EudraCT's 3
    if m:
        # The captured group, not the whole string: CTIS's own file writes a
        # literal "CTIS" in front of the id and WHO does not, so returning `s`
        # here produced CTIS:CTIS2022-... alongside CTIS:2022-... - the two
        # spellings of one study, as two nodes.
        return f"CTIS:{m.group(1)}"
    if s.startswith("EUCTR") or _EUDRACT.fullmatch(s) or s.startswith("20") and _EUDRACT.match(s):
        m = _EUDRACT.search(s)
        if m:                            # drop WHO's trailing country suffix
            return f"EUCTR:{m.group(1)}"
    for pref, ns in _PREFIX:
        if s.startswith(pref):
            return f"{ns}:{s}"
    # Registries whose own file omits the prefix WHO ICTRP writes. `add` is
    # that prefix, so the key matches what the WHO row produces.
    for pat, ns, add in _BARE:
        if pat.fullmatch(s):
            return f"{ns}:{add}{s}"
    return f"TRIAL:{s}"


# Split on ; | and <br>. NOT on comma: MeSH inverts its descriptor names, so
# "Scleroderma, Diffuse" and "Carcinoma, Non-Small-Cell Lung" are single terms,
# and comma-splitting them guarantees the dictionary lookup misses.
#
# <br> is here because WHO ICTRP passes registry HTML through untouched:
# ISRCTN writes "Glanzmann thrombasthenia <br>Genetic Diseases" and NL-OMON
# opens with one. Both resolved 0% until the tag became a separator - the
# disease name was always present, the splitter could not see it.
_SEP = re.compile(r"\s*(?:[;|]|<\s*br\s*/?\s*>)\s*", re.I)

# Registry bookkeeping wrapped around the condition. CTRI in WHO writes
# "Health Condition 1: C692- Malignant neoplasm of retina" - a label, an
# ICD-10 code, then the disease. Every one of CTRI's WHO rows carries this,
# which is why 0% of them resolved.
_COND_LABEL = re.compile(r"^\s*health\s+condition\s*\d*\s*:\s*", re.I)
_ICD_PREFIX = re.compile(r"^\s*([A-Z]\d{2,3}(?:\.\d+)?)\s*[-:]\s*")

# Marks a term that is an ICD-10 CODE rather than a condition name, so the
# matcher can hold it back and use it only when every name tier has missed.
# A prefix rather than a separate return value: _terms already feeds several
# call sites and a second channel would have to be threaded through all of
# them. No real condition begins with this.
ICD_TERM = "\x00icd10:"

# CTRI's own file writes the code inside its JSON, before a || marker:
# '(1) ICD-10 Condition: O80||Encounter for full-term uncomplicated delivery'.
# Ranges appear too - "O00-O9A", "P84-P84" - and the first code is the one
# that names the diagnosis.
_CTRI_CODE = re.compile(r"ICD-?10\s*Condition:\s*([A-Z]\d{2,3}(?:\.\d+)?)", re.I)
# NL-OMON appends a MedDRA id: "...Iliac Artery (FLIA);10047079". _SEP already
# splits it off, but a bare number left alone would count as a term.
_BARE_CODE = re.compile(r"^\d{4,}$")
# CT.gov writes "Drug: Atorvastatin", ISRCTN writes "Other: ..."
_TYPE = re.compile(r"^(?:drug|biological|device|procedure|behavioral|dietary"
                   r"\s+supplement|radiation|genetic|diagnostic\s+test|other|"
                   r"combination\s+product)\s*:\s*", re.I)
_DOSE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:mg|mcg|ug|g|ml|iu|%|mg/kg|mg/ml)\b.*$", re.I)


# _TYPE above is ClinicalTrials.gov's fixed vocabulary, and knowing only that
# is why ct.gov supplies 93% of all TESTED_IN edges. Every other registry
# labels its arms in its own words - "Observation group:", "Case series:",
# "Intervention1:", "Gold Standard:", "Index test:", "Control group:" - and
# the label went to the resolver along with the drug name, so nothing matched.
#
# Deliberately narrow: at most four words, no more than 40 characters, and
# something must follow. A drug name containing a colon is rare; a registry
# label followed by one is 445,351 rows in WHO alone.
_ARM_LABEL = re.compile(r"^[A-Za-z][A-Za-z0-9 /()-]{0,38}?\s*:\s*(?=\S)")

# Not a drug, however often it is written in the intervention field. Placebo
# alone is 47,882 rows in WHO, and a Substance node for it would connect tens
# of thousands of unrelated trials to each other.
# A combination arm names several drugs in one cell - "Carboplatin +
# Paclitaxel", "Cisplatin/Etoposide", "Rituximab and Bendamustine" - and the
# resolver was asked for the whole string, which is not a substance. 16,183
# drug-typed ct.gov trials are written that way, 6.9% of them.
#
# products.py has split ingredient lists like this from the start; the trial
# path never did. Applied to interventions only: a CONDITION containing "and"
# is usually one condition ("Overweight and Obesity" is handled separately,
# where both halves have to hit the dictionary).
_COMBO = re.compile(r"\s*[+]\s*|\s+and\s+|\s*/\s*", re.I)

_NOT_A_DRUG = {
    # Not drugs at all, however they arrive in a drug-typed arm.
    "blood sample", "blood samples", "saline solution", "water",
    "normal diet", "diet", "exercise", "surgery", "radiotherapy",
    "placebo", "placebos", "control", "controls", "no intervention",
    "standard care", "standard of care", "usual care", "routine care",
    "normal saline", "saline", "sham", "sham procedure", "blank", "vehicle",
    "best supportive care", "observation", "no treatment", "conventional",
}

# The same non-drugs written with a form or a role attached: "Placebo Oral
# Tablet", "Placebo oral capsule", "Matching Placebo", "Placebo Comparator".
# Exact matching caught none of them - roughly 1,200 arm mentions in ct.gov
# alone. "Chemotherapy" belongs here too: it names a class of treatment, and
# a trial arm saying only that has not told us which drug.
# A formulation prefix. "Nab-paclitaxel" is paclitaxel bound to albumin -
# 610 drug-typed ct.gov arms across three spellings - and the prefix is the
# only thing between it and the substance. Same shape as a salt: the moiety
# is unchanged, the presentation is not.
#
# Tried as a FALLBACK, after the whole term. If the graph holds the
# formulation as its own substance, that node wins.
_FORMULATION = re.compile(
    r"^(?:nab[ -]?|pegylated[ -]|peg[ -]|liposomal[ -]|lipo[ -]|"
    r"micronized[ -]|micronised[ -]|nano[ -]?|recombinant[ -]|"
    r"human[ -]recombinant[ -]|inhaled[ -]|oral[ -]|topical[ -])", re.I)

_NOT_A_DRUG_SUBSTR = ("placebo", "chemotherapy", "sham ", "vehicle control",
                      "standard of care", "best supportive care")


def _terms(raw: str, kind: str = "condition") -> list[str]:
    """Split a registry cell into terms the dictionaries can be asked about.

    `kind` decides which labels are stripped. Conditions carry ICD prefixes
    and "health condition N:"; interventions carry an arm label. Applying the
    arm-label rule to conditions would eat text like "Diabetes: type 2".
    """
    out = []
    for part in _SEP.split(raw or ""):
        # A code the CTRI JSON reader already extracted. It is not prose and
        # every rule below would only damage it.
        if (part or "").startswith(ICD_TERM):
            out.append(part)
            continue
        p = _DOSE.sub("", _TYPE.sub("", part or ""))
        if kind == "intervention":
            # Repeatedly, because labels nest: "Intervention1: Nil: Nil" needs
            # two passes before the placeholder underneath is visible.
            # Bounded, so a name that is genuinely all colons cannot loop.
            for _ in range(3):
                nxt = _ARM_LABEL.sub("", p, count=1)
                if nxt == p:
                    break
                p = nxt
            low_p = " ".join(p.lower().split())
            if (is_placeholder(p) or low_p in _NOT_A_DRUG
                    or any(w in low_p for w in _NOT_A_DRUG_SUBSTR)):
                continue
            # Split the combination and keep every component that survives the
            # same filters. One arm can legitimately contribute three drugs.
            for c in _COMBO.split(p):
                c = c.strip(" -	")
                if not c or is_placeholder(c):
                    continue
                low_c = " ".join(c.lower().split())
                if (low_c in _NOT_A_DRUG
                        or any(w in low_c for w in _NOT_A_DRUG_SUBSTR)):
                    continue
                if 3 <= len(c) <= 120:
                    out.append(c)
                    bare = _FORMULATION.sub("", c).strip()
                    if bare != c and 4 <= len(bare) <= 120:
                        out.append(bare)
            continue
        # Strip the registry's own labelling before the dictionary sees it.
        # Order matters: the label wraps the code, so the label goes first.
        p = _COND_LABEL.sub("", p)
        # Keep the code before discarding it. The name that follows is an ICD
        # RUBRIC - "Other specified acquired deformities", "Encounter for
        # full-term uncomplicated delivery" - phrased for a coding manual and
        # not for MeSH, so the name tiers miss it and the code was the only
        # thing that would have resolved. Emitted as a marked term so the
        # matcher can hold it back as a fallback rather than treating it as a
        # second condition.
        m = _ICD_PREFIX.match(p)
        if m:
            out.append(ICD_TERM + m.group(1).upper())
        p = _ICD_PREFIX.sub("", p).strip(" -\t")
        if _BARE_CODE.match(p):
            continue
        if 3 <= len(p) <= 120:
            out.append(p)
    return out[:12]          # a trial listing 60 interventions is a basket study


def _same_study(b, key, other, source):
    """A cross-registry reference, and the stub node that keeps it valid.

    A registry naming another registry's id is evidence the study exists, but
    that registry's own file may not be in the lake (UMIN, DRKS, IRCT) or the
    row may sit outside a slice. Emitting a stub - keyed the same way the real
    loader would key it - means the edge resolves, and if the real record is
    ever loaded it lands on the same node and overwrites nothing.
    """
    ok = trial_key(other)
    if not ok or ok == key or ok.startswith("TRIAL:"):
        return
    # phase and study_type are set here too, to the same NA the real loader
    # would write, so "unknown" has ONE spelling across the label. A stub that
    # left them off would be the null-versus-NA split all over again, just
    # confined to the 10,580 nodes nobody looks at directly.
    b.w.node("ClinicalTrial", ok, source=source, registry="referenced",
             title="", status="", phase=PHASE_NA,
             study_type=STUDY_TYPE_NA, study_type_raw="")
    b.w.edge("SAME_STUDY_AS", key, ok, match_method="structured", source=source)


def _trial(b, key, registry, source, sponsor="", conditions="", interventions="",
           iso=(), **props):
    """One trial node plus its edges. Shared by all ten registries."""
    # Normalise here rather than in each loader: this is the one funnel every
    # registry passes through, so a tenth registry gets the same treatment
    # without anyone remembering to add it.
    # Always set, even when the loader passed nothing: a trial with no phase
    # property and a trial whose phase is "not applicable" are different facts
    # and used to be indistinguishable.
    props["phase"] = norm_phase(props.get("phase", ""))
    if "status" in props:
        props["status"] = norm_status(props["status"])
    # Set unconditionally, like phase: euctr and ctis pass no study_type, and
    # an absent property is not the same claim as "we could not decide".
    raw_st = (props.get("study_type") or "").strip()
    props["study_type"] = norm_study_type(raw_st)
    props["study_type_raw"] = raw_st
    # A registry writing "NA" in the title field has not titled the trial.
    # Left in, those 16 rows are findable by a title search for NA.
    #
    # "NA" is spelled out here rather than added to is_placeholder, which
    # excludes it on purpose: it is Namibia's ISO code and the value phase and
    # study_type carry. A title is one of the places it means nothing, and
    # that judgement belongs at the call site.
    title = (props.get("title") or "").strip()
    if is_placeholder(title) or title.upper() in {"NA", "N.A"}:
        props["title"] = ""
    b.w.node("ClinicalTrial", key, source=source,
             registry=norm_registry(registry), **props)

    # is_placeholder, not just a length test. The length test alone let "nil",
    # "None", "N/A", "not applicable" and "Not available" through as COMPANY
    # nodes, and 4,066 trials were SPONSORED_BY one of them. That is worse than
    # an absent sponsor: "who sponsors this trial" answered "nil" in a
    # full sentence. Every one of these strings was already in _PLACEHOLDER -
    # the set was just never consulted here, only for titles.
    if sponsor and len(sponsor) > 2 and not is_placeholder(sponsor):
        ckey = f"COMPANY:{norm_company(sponsor)}"
        b.w.node("Company", ckey, source=source, name=sponsor, raw_names=sponsor)
        b.w.edge("SPONSORED_BY", key, ckey, match_method="structured", source=source)

    # Codes are collected here and spent at the end, only if nothing else
    # landed. Linking on both would give a trial two diseases where the
    # registry stated one - the code and the rubric are the SAME diagnosis
    # written twice, not two conditions.
    icd_codes: list[str] = []
    linked = False

    for c in _terms(conditions):
        if c.startswith(ICD_TERM):
            icd_codes.append(c[len(ICD_TERM):])
            continue
        # Tried in order, first hit wins, and the tier is recorded. Order is
        # the design: taking ANY hit rather than the first would link a
        # renal-cell trial to plain "Carcinoma" via the split variant.
        #
        #   name          the condition as written is a MeSH heading or entry
        #                 term - 73.8% of ct.gov, and the only tier that was
        #                 ever consulted
        #   name_variant  a rewriting reached MeSH: a stage qualifier removed,
        #                 a plural, "cancer" for "neoplasms" - 3.9% more
        #   icd_name      no MeSH form matched but an ICD title did - 1.1%,
        #                 and the weakest, so it is last and labelled
        #   vocab_alias   NCIt or CDISC lists the phrase as a synonym of a
        #                 concept that already reaches MeSH - this is how
        #                 "NSCLC" and "Lung Cancer" arrive, and neither is a
        #                 rewriting of any MeSH string
        dkey = mth = None
        for i, v in enumerate(condition_variants(c)):
            fv = fold(v)
            dkey = b.mesh_by_name.get(fv)
            if dkey:
                mth = "name" if i == 0 else "name_variant"
                break
            # Tried at each variant level rather than after all of them, so
            # the most specific form still wins across both dictionaries.
            dkey = b.alias_by_name.get(fv)
            if dkey:
                mth = "vocab_alias"
                break
            # Same letters, different separators: "Sars-CoV2" for "SARS-CoV 2",
            # "Covid19" for "COVID-19". A squashed key shared by two diseases
            # is stored empty and refused here.
            sq = b.mesh_squashed.get(squash(v))
            if sq:
                dkey, mth = sq, "name_squashed"
                break
        if not dkey:
            dkey = b.icd_by_name.get(fold(c))
            mth = "icd_name" if dkey else None
        # Anything but an exact match must not land on a category node. A
        # trial on "Chronic Disease" attached to the descriptor "Disease"
        # reads as a finding and states nothing; 1,278 did before this, and
        # 10 survived a first attempt that guarded the query string instead.
        if dkey and mth != "name" and dkey in b.generic_disease_keys:
            dkey = None
        if dkey:
            b.w.edge("STUDIES", key, dkey, match_method=mth, source=source)
            linked = True

        # A cell naming several conditions is several conditions. The loop
        # above stops at the first hit, so "Overweight and Obesity" linked to
        # Overweight and never to Obesity - 670 ct.gov trials linked to half
        # of what they said. Each part is resolved on its own and every one
        # that lands gets an edge; the Writer drops a duplicate, so a part
        # that agrees with the whole-term match costs nothing.
        for part in condition_parts(c):
            pk = b.mesh_by_name.get(fold(part))
            if not pk:
                for v in condition_variants(part):
                    pk = b.mesh_by_name.get(fold(v))
                    if pk:
                        break
            if pk and pk != dkey and pk not in b.generic_disease_keys:
                b.w.edge("STUDIES", key, pk, match_method="name_part",
                         source=source)
                linked = True

    # Last resort, and only for a trial that got nothing from its own words.
    # Not guarded against generic_disease_keys the way the name tiers are:
    # that guard exists because a generic TITLE matches unrelated prose by
    # accident, and a code cannot - the registry typed it to mean this.
    if not linked:
        for raw in icd_codes:
            # Exact first, then the three-character category. WHO's ICD-10
            # reference does not carry every subdivision a registry cites -
            # E11.9 is absent, E11 is there - and the category is a real level
            # of the classification, not a truncation: E11 IS "Type 2 diabetes
            # mellitus", which is what a trial citing E11.9 studies. Worth 87%
            # resolution against 63% for exact alone.
            dkey = b.icd_by_code.get(raw) or (
                b.icd_by_code.get(raw[:3]) if len(raw) > 3 else None)
            if dkey:
                b.w.edge("STUDIES", key, dkey, match_method="icd_code",
                         source=source)
                break

    for i in _terms(interventions, kind="intervention"):
        m = b.r.resolve(i)
        if m.key and m.resolved:                  # never provisional from prose
            b.w.edge("TESTED_IN", m.key, key, match_method=m.method, source=source)

    for code in iso:
        b.w.node("Country", f"COUNTRY:{code}", source=source, iso2=code,
                 name=countries.NAME.get(code, code))
        b.w.edge("CONDUCTED_IN", key, f"COUNTRY:{code}", match_method="name",
                 source=source)


def load_ctgov(b):
    """The richest registry and the one most other sources cross-reference."""
    t0 = b._step("ctgov")
    key = L["ctgov"]
    n = 0
    for row in lake.stream_csv(key, limit=b.limit):
        nct = (row.get("nct_id") or "").strip()
        if not nct:
            continue
        if not b.wanted_trial(row.get("interventions", ""), row.get("brief_title", "")):
            continue
        n += 1
        k = trial_key(nct)
        _trial(b, k, "clinicaltrials.gov", key,
               sponsor=row.get("lead_sponsor", ""),
               conditions=row.get("conditions", ""),
               interventions=_ctri_interventions(
                   row.get("interventions", "")),
               iso=countries.from_locations(row.get("locations", "")),
               title=row.get("brief_title", ""),
               status=row.get("overall_status", ""),
               phase=row.get("phases", ""),
               study_type=row.get("study_type", ""),
               enrollment=row.get("enrollment", ""),
               start_date=row.get("start_date", ""))
        b.w.identifier(k, "NCT", nct, source=key)
    b._done("ctgov", t0, n)


def load_who(b):
    """4.6 GB, and an aggregation of the registries around it.

    Loaded last of the trial sources so that native records win on properties.
    Its value is `Secondary_ID` and `other_records`: the same study registered
    in two places, stated by the registry itself rather than inferred from
    matching titles.
    """
    t0 = b._step("who_trials")
    key = L["who"]
    n = 0
    for row in lake.stream_csv(key, limit=b.limit):
        tid = (row.get("TrialID") or "").strip()
        if not tid:
            continue
        if not b.wanted_trial(row.get("Intervention", ""), row.get("Public_title", "")):
            continue
        n += 1
        k = trial_key(tid)
        _trial(b, k, (row.get("Source_Register") or "who").strip(), key,
               sponsor=row.get("Primary_sponsor", ""),
               conditions=row.get("Condition", ""),
               interventions=row.get("Intervention", ""),
               iso=countries.from_list(row.get("Countries", "")),
               title=row.get("Public_title", ""),
               status=row.get("Recruitment_Status", ""),
               phase=row.get("Phase", ""),
               study_type=row.get("Study_type", ""),
               enrollment=row.get("Target_size", ""),
               start_date=row.get("Date_enrollement", ""))
        # only ids landing in a known registry namespace; protocol codes and
        # sponsor references canonicalise to TRIAL: and are noise
        for other in _SEP.split(row.get("Secondary_ID", "") or ""):
            _same_study(b, k, other, key)
    b._done("who_trials", t0, n)


# EudraCT's own section numbering, which is what separates a real column from
# the criteria prose that polluted this header. A value sometimes runs into
# the NEXT section's label - "No E.8 Design of the trial" - because the
# scraper split on the wrong boundary, so a value is cut at the first section
# code it contains.
_EUCTR_BLEED = re.compile(r"\s+[A-Z]\.[0-9](?:\.[0-9]+)*\s+[A-Z].*$")


def _euctr(row: dict, col: str) -> str:
    """One EudraCT field, with the next section's label trimmed off."""
    v = (row.get(col) or "").strip()
    return _EUCTR_BLEED.sub("", v).strip() if v else ""


#: The four phase flags EudraCT publishes, each Yes or No. Better than the
#: single phase string other registries give: a trial that is both phase 1 and
#: phase 2 says Yes twice, which is exactly the combined phase norm_phase has
#: to infer from prose everywhere else.
_EUCTR_PHASE = [
    ("E.7.1 Human pharmacology (Phase I)", 1),
    ("E.7.2 Therapeutic exploratory (Phase II)", 2),
    ("E.7.3 Therapeutic confirmatory (Phase III)", 3),
    ("E.7.4 Therapeutic use (Phase IV)", 4),
]

_EUCTR_COMBINED = {
    (1,): "PHASE1", (2,): "PHASE2", (3,): "PHASE3", (4,): "PHASE4",
    (1, 2): "PHASE1_PHASE2", (2, 3): "PHASE2_PHASE3", (3, 4): "PHASE3_PHASE4",
}


def euctr_phase(row: dict) -> str:
    """The four Yes/No flags -> one canonical phase.

    A pair that EudraCT allows and this graph has no value for - phase 1 and
    phase 3 with nothing between them, or three flags at once - falls back to
    the LOWEST, because that is the phase the trial has actually reached.
    Claiming the highest would overstate development stage on 44,511 trials.
    """
    on = tuple(n for col, n in _EUCTR_PHASE
               if _euctr(row, col).lower().startswith("yes"))
    if not on:
        return PHASE_NA
    return _EUCTR_COMBINED.get(on, f"PHASE{on[0]}")


#: EudraCT repeats a section per sponsor and per investigational product, and
#: the scrape namespaces each repeat: "Sponsor 1 - B.1.1 Name of Sponsor",
#: "IMP 1 - D.3.1 Product name", ... up to IMP 30-odd.
#:
#: These were called destroyed by the header damage and they never were. The
#: check that "proved" it looked for columns STARTING with "B." and "D." -
#: these start with "Sponsor" and "IMP", so it found only the inclusion-criteria
#: junk that happens to begin with those letters. The sponsor column is filled
#: on 299 of 300 sampled rows and has been the whole time.
_EUCTR_SPONSOR = re.compile(r"^Sponsor\s+\d+\s*-\s*B\.1\.1\s+Name of Sponsor$", re.I)
#: Trade name, product name and INN for each IMP. All three are taken: a trial
#: naming only a trade name resolves through a different tier than one naming
#: an INN, and taking whichever exists costs one more column read.
_EUCTR_IMP = re.compile(
    r"^IMP\s+\d+\s*-\s*D\.(?:3\.1\s+Product name|3\.8\s+INN|2\.1\.1\.1\s+Trade name)",
    re.I)


def _euctr_sponsor(row: dict) -> str:
    """The first named sponsor. Later ones are co-sponsors, not the owner."""
    for col in row:
        if _EUCTR_SPONSOR.match(col.strip()):
            v = _euctr(row, col)
            if v:
                return v
    return ""


def _euctr_interventions(row: dict) -> str:
    """Every IMP name on the trial, in the separator _terms already splits on.

    Deduplicated case-insensitively: the same compound is routinely written
    into the trade-name, product-name and INN columns of one IMP, and three
    copies would be three identical resolver lookups.
    """
    out, seen = [], set()
    for col in row:
        if not _EUCTR_IMP.match(col.strip()):
            continue
        v = _euctr(row, col)
        if not v:
            continue
        f = v.casefold()
        if f not in seen:
            seen.add(f)
            out.append(v)
    return "; ".join(out)


_EUCTR_COND = "E.1.1 Medical condition(s) being investigated"
# EudraCT asks for the condition TWICE: once as free text, and once coded
# against MedDRA. The free text is what a sponsor typed in their own language
# - "Magas vercukor", "Cancer de l'ovaire", "Tratamiento del dolor agudo" -
# and mesh_by_name is English, so most of it resolves to nothing. That is most
# of why eu_ctr sits at 36% disease linkage.
#
# E.1.2 Term is the same condition as a MedDRA term, in English, drawn from a
# controlled list: "Rheumatoid arthritis", "Non-small cell lung cancer",
# "Multiple myeloma". 79% of rows carry one and nothing read it.
#
# Both are passed. The coded term is not a REPLACEMENT - free text is more
# specific when it is usable, and _terms already handles several conditions in
# one cell, so the tiers can take whichever lands.
_EUCTR_TERM = "E.1.2 Term"


def _euctr_conditions(row: dict) -> str:
    free = (row.get(_EUCTR_COND) or "").strip()
    term = (row.get(_EUCTR_TERM) or "").strip()
    if term and term.lower() != free.lower():
        return f"{free}; {term}" if free else term
    return free


def load_euctr(b):
    """EU CTR, and the columns the damaged header was hiding.

    The file has 8,102 columns because every distinct inclusion-criteria line
    across 44,511 trials leaked into the header during scraping. The real
    EudraCT fields are still among them and are identifiable, because EudraCT
    numbers its sections: a genuine column is "A.3 Full title of the trial" or
    "E.1.2 Term", while the junk is "A. Adequate renal function defined as".

    So this reads by section number rather than by position. Title, phase and
    the MedDRA-coded condition are filled on most rows and were all being
    discarded on the reasoning that the header was unreadable past column four.
    The header is; the data is not.
    """
    t0 = b._step("eu_ctr")
    key = L["euctr"]
    n = 0
    if b.slice is not None:
        # The four surviving columns carry no substance and no title, so there
        # is nothing to filter a slice on - loading it would add ~118k trials
        # to a three-molecule slice and make every count meaningless.
        b.stats["euctr_skipped_in_slice"] = True
        b._done("eu_ctr", t0, 0)
        return
    for row in lake.stream_csv(key, limit=b.limit):
        eid = (row.get("EudraCT Number") or "").strip()
        if not eid:
            continue
        n += 1
        k = trial_key(eid)
        # E.1.1 survived the header damage and holds the condition in
        # English. Only 3% of eu_ctr trials had a disease link before this,
        # not because the data was missing but because nothing read it.
        # The 22 translated copies - (de), (fr), (it) ... - are skipped:
        # mesh_by_name is English, so they would resolve to nothing and cost
        # a dictionary lookup each.
        # The header is damaged, the DATA is not. EudraCT numbers its sections,
        # so a real column is "A.3 Full title of the trial" while the junk is
        # "A. Adequate renal function defined as" - and 556 of the 8,102
        # columns match the real pattern. Title and phase are filled on ~100%
        # of rows and were being discarded.
        _trial(b, k, "eu_ctr", key, status=row.get("Trial Status", ""),
               conditions=_euctr_conditions(row),
               phase=euctr_phase(row),
               sponsor=_euctr_sponsor(row),
               interventions=_euctr_interventions(row),
               title=_euctr(row, "A.3 Full title of the trial"))
    b.stats["euctr_header_damaged_cols"] = 128
    b._done("eu_ctr", t0, n)


def load_chictr(b):
    t0 = b._step("chictr")
    key = L["chictr"]
    n = 0
    for row in lake.stream_csv(key, limit=b.limit):
        tid = (row.get("trial_id") or "").strip()
        if not tid:
            continue
        if not b.wanted_trial(row.get("i_freetext", ""), row.get("public_title", "")):
            continue
        n += 1
        _trial(b, trial_key(tid), "chictr", key,
               sponsor=row.get("primary_sponsor", ""),
               conditions=row.get("hc_freetext", ""),
               interventions=row.get("i_freetext", ""),
               title=row.get("public_title", ""),
               status=row.get("recruitment_status", ""),
               phase=row.get("phase", ""),
               study_type=row.get("study_type", ""),
               enrollment=row.get("target_size", ""),
               start_date=row.get("date_enrolment", ""))
    b._done("chictr", t0, n)


def load_anzctr(b):
    """Australia/NZ. Column names are upper case with spaces."""
    t0 = b._step("anzctr")
    key = L["anzctr"]
    n = 0
    for row in lake.stream_csv(key, limit=b.limit):
        tid = (row.get("ACTRN") or row.get("TRIAL ID") or "").strip()
        if not tid:
            continue
        if not b.wanted_trial(row.get("INTERVENTIONS", ""), row.get("STUDY TITLE", "")):
            continue
        n += 1
        _trial(b, trial_key(tid), "anzctr", key,
               interventions=row.get("INTERVENTIONS", ""),
               title=row.get("STUDY TITLE", ""),
               phase=row.get("PHASE", ""),
               study_type=row.get("STUDY TYPE", ""),
               start_date=row.get("ACTUAL START DATE", "") or
                          row.get("ANTICIPATED START DATE", ""))
    b._done("anzctr", t0, n)


def load_isrctn(b):
    t0 = b._step("isrctn")
    key = L["isrctn"]
    n = 0
    for row in lake.stream_csv(key, limit=b.limit):
        tid = (row.get("ISRCTN") or "").strip()
        if not tid:
            continue
        drug = row.get("Drug/device/biological/vaccine name(s)", "")
        if not b.wanted_trial(drug, row.get("Title", "")):
            continue
        n += 1
        k = trial_key(tid)
        _trial(b, k, "isrctn", key,
               sponsor=row.get("Sponsor", ""),
               conditions=row.get("Health condition(s) or problem(s) studied", ""),
               interventions=drug,
               iso=countries.from_list(row.get("Country of recruitment", "")),
               title=row.get("Title", ""),
               status=row.get("Overall study status", ""),
               phase=row.get("Phase", ""),
               study_type=row.get("Study type(s)", ""),
               start_date=row.get("Date of first enrolment", ""))
        # ISRCTN states its own cross-registrations in two dedicated columns
        for col in ("EudraCT/CTIS number", "ClinicalTrials.gov number"):
            _same_study(b, k, row.get(col, ""), key)
    b._done("isrctn", t0, n)


def load_ctis(b):
    t0 = b._step("ctis")
    key = L["ctis"]
    n = 0
    for row in lake.stream_csv(key, limit=b.limit):
        tid = (row.get("Trial number") or "").strip()
        if not tid:
            continue
        if not b.wanted_trial(row.get("Product", ""), row.get("Title of the trial", "")):
            continue
        n += 1
        _trial(b, trial_key(tid), "ctis", key,
               sponsor=row.get("Sponsor/Co-Sponsors", ""),
               conditions=row.get("Medical conditions", ""),
               interventions=row.get("Product", ""),
               title=row.get("Title of the trial", ""),
               status=row.get("Overall trial status", ""),
               phase=row.get("Trial phase", ""),
               enrollment=row.get("Number of participants enrolled", ""),
               start_date=row.get("Start date", ""))
    b._done("ctis", t0, n)


def _ctri_conditions(raw: str) -> str:
    """CTRI stores conditions as a JSON array, not as text.

        [{"health_type": "Patients",
          "condition": "(1) ICD-10 Condition: O80||Encounter for full-term..."}]

    The condition itself is wrapped in the registry's own coding notation - a
    numbered prefix, an ICD-10 label, and the human phrase after a || marker.
    _terms() cannot see through that, which is why passing the raw field would
    not have helped either.
    """
    if not raw or not raw.strip().startswith("["):
        return raw or ""
    try:
        items = json.loads(raw)
    except Exception:                                          # noqa: BLE001
        return ""
    out = []
    for it in items if isinstance(items, list) else []:
        c = str((it or {}).get("condition", "")).strip()
        if not c:
            continue
        # "(1) ICD-10 Condition: O80||Encounter for..." -> "Encounter for...",
        # and the O80 kept alongside it. 63% of this file's rows carry a code
        # and the name after it is an ICD rubric, which is most of why CTRI
        # sits at 27% disease linkage against ct.gov's 71%.
        code = _CTRI_CODE.search(c)
        if code:
            out.append(ICD_TERM + code.group(1).upper())
        if "||" in c:
            c = c.split("||", 1)[1]
        c = re.sub(r"^\s*\(\d+\)\s*", "", c)
        c = re.sub(r"^\s*ICD-10 Condition:\s*\S+\s*", "", c)
        if c.strip():
            out.append(c.strip())
    return "; ".join(out)


def _ctri_interventions(raw: str) -> str:
    """CTRI stores interventions as a JSON array, the same as its conditions.

        [{"type": "Intervention",
          "name": "Buccal misoprostol",
          "details": "Administration of buccal misoprostol for induction..."}]

    Passed through as text this is not prose, it is a serialised object, and
    _terms cannot see a drug name inside it. The 1,929 CTRI trials that did
    reach a drug got there by a substring of the blob happening to match -
    which is worse than no link, because it looks like the loader working.

    `name` only. `details` is a paragraph of protocol prose that mentions
    doses, comparators and routes, and mining it would resolve the comparator
    as readily as the drug under test.
    """
    if not raw or not raw.strip().startswith("["):
        return raw or ""
    try:
        items = json.loads(raw)
    except Exception:                                          # noqa: BLE001
        return ""
    out = []
    for it in items if isinstance(items, list) else []:
        nm = str((it or {}).get("name", "")).strip()
        if nm:
            out.append(nm)
    return "; ".join(out)


def load_ctri(b):
    t0 = b._step("ctri")
    key = L["ctri"]
    n = 0
    for row in lake.stream_csv(key, limit=b.limit):
        tid = (row.get("ctri_number") or row.get("ctri_id") or "").strip()
        if not tid:
            continue
        if not b.wanted_trial("", row.get("public_title_of_study", "")):
            continue
        n += 1
        # CTRI publishes `interventions` and this loader did not read it, so
        # all 61,738 of its trials had no drug link - the same oversight as
        # the condition columns three registries were publishing unread.
        _trial(b, trial_key(tid), "ctri", key,
               interventions=_ctri_interventions(
                   row.get("interventions", "")),
               conditions=_ctri_conditions(row.get("health_conditions", "")),
               sponsor=row.get("primary_sponsor_name", ""),
               iso=countries.from_list(row.get("countries_of_recruitment", "")),
               title=row.get("public_title_of_study", ""),
               study_type=row.get("type_of_study", ""),
               enrollment=row.get("target_sample_size", ""),
               start_date=row.get("registration_date", ""))
    b._done("ctri", t0, n)


#: jRCT publishes no phase field. Not "publishes it badly" - there is no such
#: column among the 80 it does publish, which is why all 2,492 jrct trials
#: carried a phase of NA and why zero of them could be filtered on.
#:
#: It states the phase in the TITLE instead, in the standard trial-title form:
#: "A phase I open-label, multi-center study to evaluate...". Reading it there
#: is reading, not inferring - the alternative is 0% coverage on a registry
#: that runs real phased trials.
#:
#: Strict on purpose: the literal word "phase" followed by a numeral or a roman
#: numeral. A title that merely contains the word does not qualify.
_JRCT_PHASE = re.compile(r"\bphase\s*(?:i{1,3}v?|[0-4])\b", re.I)
#: Enough of the title after the match for norm_phase to see a combined phase.
#: Matching the token alone read "Phase 2/3" as PHASE2 - 91 of 770 extractions,
#: 12%, silently recorded as a lower phase than the trial actually is.
_JRCT_PHASE_WINDOW = 24


def _jrct_phase(row: dict) -> str:
    for col in ("Scientific Title", "Public Title"):
        t = row.get(col) or ""
        m = _JRCT_PHASE.search(t)
        if m:
            return norm_phase(t[m.start():m.start() + _JRCT_PHASE_WINDOW])
    return PHASE_NA


def load_jrct(b):
    """Japan, and why it is small - which is the source, not this loader.

    jrct holds 478 trials with a phase of NA on every one, and jprn 1,173. Both
    look like parsing failures and neither is:

      * jrct_list.csv has 478 ROWS and 72 columns, and not one of them is a
        phase. There is nothing here to read. Japan's real jRCT is far larger,
        so the gap is in the scrape.
      * The WHO export carries 12,470 JPRN rows and only 1,232 DISTINCT trial
        ids - it repeats each study about ten times. 1,232 is what the graph
        holds, so nothing is being dropped. Counting rows rather than ids is
        what made this look like an 11,000-trial bug.
      * Korea is absent for the same kind of reason: the WHO export has 3 KCT
        rows in total, which is why cris has 1 trial.

    Primary Sponsor is present in this file and not read yet.
    """
    t0 = b._step("jrct")
    key = L["jrct"]
    n = 0
    for row in lake.stream_csv(key, limit=b.limit):
        tid = (row.get("Trial ID") or "").strip()
        if not tid:
            continue
        if not b.wanted_trial("", row.get("Public Title", "")):
            continue
        n += 1
        _trial(b, trial_key(tid), "jrct", key,
               phase=_jrct_phase(row),
               interventions=row.get("Intervention(s)", ""),
               # Present in the file and unread until now, which is why jrct
               # sat at 12.1% sponsored while every other native registry is
               # above 91%.
               sponsor=row.get("Primary Sponsor", ""),
               conditions=row.get("Health Condition(s) or Problem(s) Studied", ""),
               title=row.get("Public Title", ""),
               status=row.get("Recruitment status", ""),
               study_type=row.get("Study Type", ""),
               start_date=row.get("Actual date of first enrollment", "") or
                          row.get("Anticipated date of first enrollment", ""))
    b._done("jrct", t0, n)


#: cp_contents packs several conditions into one cell as
#:     English name(한국어),English name(한국어)
#: and the separator is a comma - but so is the punctuation INSIDE a name:
#: "Hyperlipidemia, unspecified(...)". Splitting on commas gives "Hyperlipidemia"
#: and "unspecified"; not splitting at all gives one 120-character string that
#: matches nothing. Both were measured, both are wrong.
#:
#: The Korean gloss is the reliable delimiter: every entry ends with one, so a
#: closing bracket followed by a comma separates entries and a comma inside a
#: name never does.
_CRIS_SPLIT = re.compile(r"\)\s*,")


#: Trailing bracket groups - the Korean gloss, and an inline ICD code before
#: it: "Malignant neoplasm of breast(C50)(유방의 악성신생물)".
_CRIS_GLOSS = re.compile(r"\s*\([^()]*\)\s*$")


def _cris_conditions(row: dict) -> str:
    """cp_contents -> the separator _terms already splits on, gloss removed.

    The first version left the brackets attached, reasoning that fold() drops
    bracketed text anyway. That is true of fold and irrelevant here: the
    variant ladder runs on the UNFOLDED string, and its ICD-rubric rule anchors
    "unspecified" at the end. With the gloss still there the string ends in
    "(상세불명의 고지혈증)", the rule never fires, and CRIS resolved 1,109 of
    12,391 trials.

    It passed its unit test because the test fed it "Hyperlipidemia,
    unspecified" - what the rubric looks like in ICD, not what this registry
    actually writes.
    """
    raw = (row.get("cp_contents") or "").strip()
    if not raw:
        return ""
    out = []
    for part in _CRIS_SPLIT.split(raw):
        part = part.strip()
        if not part:
            continue
        # Restore the bracket the split consumed - but ONLY when one is
        # actually missing. Appending it unconditionally put a stray ")" on
        # every entry that never had a gloss, so "Healthy Volunteers" became
        # "Healthy Volunteers)" and matched nothing.
        if part.count("(") > part.count(")"):
            part += ")"
        # Peel trailing bracket groups until none is left: the Korean gloss,
        # and an inline ICD code sitting in front of it.
        prev = None
        while prev != part:
            prev = part
            part = _CRIS_GLOSS.sub("", part).strip()
        # Anything still unbalanced is punctuation, not meaning.
        while part.endswith(")") and part.count(")") > part.count("("):
            part = part[:-1].strip()
        if part:
            out.append(part)
    return "; ".join(out)


def load_cris(b):
    """Korea, which reached this graph as ONE trial before the source existed.

    Not a loader bug and never was: the WHO ICTRP export carries 3 KCT rows in
    total, so there was nothing to read. CRIS is a WHO primary registry with
    12,391 studies and now has its own scraper.

    The id is `system_number` (KCT0000001), NOT `research_number` - that is the
    sponsor's own protocol code and is not unique across trials.

    Values that carry Korean and English in one string - "중재연구
    (Interventional Study)" - are passed through unchanged. norm_study_type and
    norm_status read the English out of prose already, and study_type_raw is
    supposed to hold the registry's own wording.
    """
    t0 = b._step("cris")
    key = L["cris"]
    n = 0
    for row in lake.stream_csv(key, limit=b.limit):
        tid = (row.get("system_number") or "").strip()
        if not tid:
            continue
        if not b.wanted_trial("", row.get("research_title_en", "")):
            continue
        n += 1
        # cp_contents is "English name(한국어),English name(한국어)". fold()
        # drops bracketed text, so the Korean removes itself and the English
        # names are what reach the resolver.
        _trial(b, trial_key(tid), "cris", key,
               conditions=_cris_conditions(row),
               sponsor=row.get("resrc_spp_en", ""),
               title=row.get("research_title_en", ""),
               phase=row.get("clinical_step", ""),
               status=row.get("research_step", ""),
               study_type=row.get("research_kind", ""),
               start_date=row.get("study_start_date", ""))
    b._done("cris", t0, n)


# WHO last: native registries win on properties, WHO supplies the links.
ALL = [load_ctgov, load_euctr, load_chictr, load_anzctr, load_isrctn,
       load_ctis, load_ctri, load_jrct, load_cris, load_who]
