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
_CTIS = re.compile(r"^(\d{4}-\d{6}-\d{2}-\d{2})$")


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
    if _CTIS.match(s):                   # 4 groups = CTIS, before EudraCT's 3
        return f"CTIS:{s}"
    if s.startswith("EUCTR") or _EUDRACT.fullmatch(s) or s.startswith("20") and _EUDRACT.match(s):
        m = _EUDRACT.search(s)
        if m:                            # drop WHO's trailing country suffix
            return f"EUCTR:{m.group(1)}"
    for pref, ns in _PREFIX:
        if s.startswith(pref):
            return f"{ns}:{s}"
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
    b.w.node("ClinicalTrial", key, source=source, registry=registry, **props)

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
