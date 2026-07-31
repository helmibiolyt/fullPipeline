"""ClinicalTrial, across nine registries.

The hard problem here is not loading nine files, it is that the same study is
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

import re

import countries
import lake
from normalise import fold, norm_company

# --------------------------------------------------------------------------
# Nine registries describe the same three facts in their own words. Left raw,
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
    (r"^\s*0\s*$", "PHASE0"),
    (r"^\s*4\s*$", "PHASE4"),
    (r"^\s*3\s*$", "PHASE3"),
    (r"^\s*2\s*$", "PHASE2"),
    (r"^\s*1\s*$", "PHASE1"),
]

# Values meaning "no phase applies". Stored as "" rather than kept, so a
# filter on phase does not return 409,721 rows that assert nothing.
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
    """112 registry spellings -> one of eight canonical values, or ""."""
    s = (raw or "").strip()
    if s.lower() in _PHASE_NONE:
        return ""
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
    return ""


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
    (re.compile(r"^PER-\d{3}-\d{2}$"), "REPEC", ""),   # Peru
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


# Split on ; and | only. NOT on comma: MeSH inverts its descriptor names, so
# "Scleroderma, Diffuse" and "Carcinoma, Non-Small-Cell Lung" are single terms,
# and comma-splitting them guarantees the dictionary lookup misses.
_SEP = re.compile(r"\s*[;|]\s*")
# CT.gov writes "Drug: Atorvastatin", ISRCTN writes "Other: ..."
_TYPE = re.compile(r"^(?:drug|biological|device|procedure|behavioral|dietary"
                   r"\s+supplement|radiation|genetic|diagnostic\s+test|other|"
                   r"combination\s+product)\s*:\s*", re.I)
_DOSE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:mg|mcg|ug|g|ml|iu|%|mg/kg|mg/ml)\b.*$", re.I)


def _terms(raw: str) -> list[str]:
    out = []
    for part in _SEP.split(raw or ""):
        p = _DOSE.sub("", _TYPE.sub("", part or "")).strip(" -\t")
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
    b.w.node("ClinicalTrial", ok, source=source, registry="referenced",
             title="", status="")
    b.w.edge("SAME_STUDY_AS", key, ok, match_method="structured", source=source)


def _trial(b, key, registry, source, sponsor="", conditions="", interventions="",
           iso=(), **props):
    """One trial node plus its edges. Shared by all nine registries."""
    # Normalise here rather than in each loader: this is the one funnel every
    # registry passes through, so a tenth registry gets the same treatment
    # without anyone remembering to add it.
    if "phase" in props:
        props["phase"] = norm_phase(props["phase"])
    if "status" in props:
        props["status"] = norm_status(props["status"])
    b.w.node("ClinicalTrial", key, source=source,
             registry=norm_registry(registry), **props)

    if sponsor and len(sponsor) > 2:
        ckey = f"COMPANY:{norm_company(sponsor)}"
        b.w.node("Company", ckey, source=source, name=sponsor, raw_names=sponsor)
        b.w.edge("SPONSORED_BY", key, ckey, match_method="structured", source=source)

    for c in _terms(conditions):
        dkey = b.mesh_by_name.get(fold(c))
        if dkey:                                  # exact dictionary hit only
            b.w.edge("STUDIES", key, dkey, match_method="name", source=source)

    for i in _terms(interventions):
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
               interventions=row.get("interventions", ""),
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


def load_euctr(b):
    """EU CTR, loaded for four columns only.

    The file has 132 columns, but from the fifth onward the header row is
    inclusion-criteria prose that leaked into the header during scraping -
    column names like 'A.\\tAgenti anticolinesterasici (azione indiretta)'.
    The first four columns are intact and are the only ones used; the rest are
    unreadable without re-scraping, which is a scraper fix, not a loader fix.
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
        _trial(b, k, "eu_ctr", key, status=row.get("Trial Status", ""),
               title="")
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
        _trial(b, trial_key(tid), "ctri", key,
               sponsor=row.get("primary_sponsor_name", ""),
               iso=countries.from_list(row.get("countries_of_recruitment", "")),
               title=row.get("public_title_of_study", ""),
               study_type=row.get("type_of_study", ""),
               enrollment=row.get("target_sample_size", ""),
               start_date=row.get("registration_date", ""))
    b._done("ctri", t0, n)


def load_jrct(b):
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
               title=row.get("Public Title", ""),
               status=row.get("Recruitment status", ""),
               study_type=row.get("Study Type", ""),
               start_date=row.get("Actual date of first enrollment", "") or
                          row.get("Anticipated date of first enrollment", ""))
    b._done("jrct", t0, n)


# WHO last: native registries win on properties, WHO supplies the links.
ALL = [load_ctgov, load_euctr, load_chictr, load_anzctr, load_isrctn,
       load_ctis, load_ctri, load_jrct, load_who]
