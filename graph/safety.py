"""RegulatoryEvent and AdverseEvent.

RegulatoryEvent is one label for eight different files - referrals, shortages,
orphan designations, DHPCs, paediatric plans, recalls, safety alerts. They are
one node type because they are one question: "has anything happened to this
drug". A `type` property keeps them distinguishable, and the alternative -
eight labels - would mean eight queries to answer that.

AdverseEvent is where a graph can quietly explode. FAERS is ~2.9M individual
reports across 12 files. One node per report would add three million leaf nodes
carrying no relationships anyone queries, so the load aggregates instead: one
HAS_ADVERSE_EVENT edge per (substance, reaction) with report, serious and death
counts as properties. That is the shape the question actually takes - "what is
reported for this drug, and how often" - and it turns 2.9M nodes into ~25k.
"""
from __future__ import annotations

import lake
from normalise import fold, norm_company

L = {
    "referrals":  "Regulatory_Approvals/ema.europa.eu/ema_data/additional_tables/referrals.csv",
    "shortages":  "Regulatory_Approvals/ema.europa.eu/ema_data/additional_tables/shortages.csv",
    "orphan":     "Regulatory_Approvals/ema.europa.eu/ema_data/additional_tables/orphan_designations.csv",
    "dhpc":       "Regulatory_Approvals/ema.europa.eu/ema_data/additional_tables/dhpc.csv",
    "pip":        "Regulatory_Approvals/ema.europa.eu/ema_data/additional_tables/paediatric_investigation_plans.csv",
    "recalls":    "Safety_Pharmacovigilance/open.fda.gov/Drug_Recalls/drug_recalls.csv",
    "sfda_alert": "MENA_GCC_Regulatory_Market/sfda.gov.sa/Safety_Alert.csv",
    "sfda_short": "MENA_GCC_Regulatory_Market/sfda.gov.sa/Shortage_Drugs_List.csv",
}

FAERS_QUARTERS = ("2020", "2021", "2022", "2023", "2024Q1", "2024Q2", "2024Q3",
                  "2024Q4", "2025Q1", "2025Q2", "2025Q3_Q4", "2026Q1")

# A single spontaneous report is not evidence of anything; it is one person
# filling in a form. Below this many reports the pair is dropped, and the count
# of dropped pairs is recorded in the manifest so the cut is visible rather
# than silent.
MIN_REPORTS = 3


def _subject(b, ekey, names, source, sep=";"):
    """Attach an event to the substances it concerns.

    These are controlled INN fields on small files, so an unresolved name gets
    a provisional node - the same contract as the product loaders. Trials do
    the opposite because their volume is three orders of magnitude larger.
    """
    for raw in (names or "").split(sep):
        name = raw.strip()
        if len(name) < 3:
            continue
        m = b.r.resolve(name)
        if not m.key:
            continue
        if not m.resolved:
            b.w.node("Substance", m.key, source=source, name=name,
                     norm_name=fold(name), resolved_by="provisional")
        b.w.edge("SUBJECT_OF", m.key, ekey, match_method=m.method, source=source)


def _event(b, ekey, etype, source, subjects="", sep=";", **props):
    # Slice scope is decided on the subject, not the event: these files have no
    # substance key, only a name. Without this a three-molecule slice picks up
    # all 7,360 EMA events and ~5,000 provisional substances with them, and the
    # resulting counts describe nothing.
    if not b.wanted(subjects, props.get("name", "")):
        return
    b.w.node("RegulatoryEvent", ekey, source=source, type=etype, **props)
    _subject(b, ekey, subjects, source, sep=sep)


def load_ema_events(b):
    """Five EMA tables, five event types, one shape."""
    t0 = b._step("ema_events")
    n = 0

    for row in lake.stream_csv(L["referrals"], limit=b.limit):
        ref = (row.get("reference_number") or "").strip()
        if not ref:
            continue
        # the file covers veterinary referrals too; this is a human graph
        if (row.get("category") or "").strip().lower() == "veterinary":
            continue
        n += 1
        _event(b, f"EVENT:REFERRAL:{ref}", "referral", L["referrals"],
               subjects=row.get("international_non_proprietary_name_inn_common_name", ""),
               name=row.get("referral_name", ""),
               status=row.get("current_status", ""),
               start_date=row.get("procedure_start_date", ""),
               end_date=row.get("european_commission_decision_date", ""),
               url=row.get("referral_url", ""))

    for row in lake.stream_csv(L["shortages"], limit=b.limit):
        med = (row.get("medicine_affected") or "").strip()
        if not med:
            continue
        n += 1
        _event(b, f"EVENT:SHORTAGE:EMA:{fold(med)}", "shortage", L["shortages"],
               subjects=row.get("international_non_proprietary_name_inn_or_common_name", ""),
               name=med, status=row.get("supply_shortage_status", ""),
               start_date=row.get("start_of_shortage_date", ""),
               end_date=row.get("expected_resolution_date", ""),
               url=row.get("shortage_url", ""))

    for row in lake.stream_csv(L["orphan"], limit=b.limit):
        des = (row.get("eu_designation_number") or "").strip()
        if not des:
            continue
        n += 1
        _event(b, f"EVENT:ORPHAN:{des}", "orphan_designation", L["orphan"],
               subjects=row.get("active_substance", ""),
               name=row.get("intended_use", ""),
               status=row.get("status", ""),
               start_date=row.get("date_of_designation_refusal", ""),
               url=row.get("orphan_designation_url", ""))

    for row in lake.stream_csv(L["dhpc"], limit=b.limit):
        med = (row.get("name_of_medicine") or "").strip()
        if not med or (row.get("category") or "").strip().lower() == "veterinary":
            continue
        n += 1
        _event(b, f"EVENT:DHPC:{fold(med)}:{fold(row.get('dhpc_type', ''))}",
               "dhpc", L["dhpc"],
               subjects=row.get("active_substances", ""),
               name=med, reason=row.get("dhpc_type", ""),
               status=row.get("regulatory_outcome", ""),
               start_date=row.get("dissemination_date", ""),
               url=row.get("dhpc_url", ""))

    for row in lake.stream_csv(L["pip"], limit=b.limit):
        dec = (row.get("decision_number") or "").strip()
        if not dec:
            continue
        n += 1
        _event(b, f"EVENT:PIP:{dec}", "paediatric_investigation_plan", L["pip"],
               subjects=row.get("active_substance", ""),
               name=row.get("condition_indication", ""),
               status=row.get("compliance_outcome", ""),
               start_date=row.get("decision_date", ""),
               url=row.get("pip_url", ""))

    b._done("ema_events", t0, n)


def load_recalls(b):
    """FDA recalls. `classification` is the severity: Class I is the one that
    can kill someone, Class III is a labelling defect."""
    t0 = b._step("fda_recalls")
    key = L["recalls"]
    n = 0
    for row in lake.stream_csv(key, limit=b.limit):
        rn = (row.get("recall_number") or "").strip()
        generic = (row.get("generic_name") or "").strip()
        if not rn:
            continue
        if not b.wanted(generic, row.get("brand_name", "")):
            continue
        n += 1
        ekey = f"EVENT:RECALL:{rn}"
        _event(b, ekey, "recall", key,
               subjects=generic, sep=",",
               name=row.get("product_description", "")[:300],
               status=row.get("status", ""),
               reason=row.get("reason_for_recall", "")[:500],
               start_date=row.get("recall_initiation_date", ""),
               end_date=row.get("termination_date", ""))
        firm = (row.get("recalling_firm") or "").strip()
        if len(firm) > 2:
            ckey = f"COMPANY:{norm_company(firm)}"
            b.w.node("Company", ckey, source=key, name=firm, raw_names=firm)
            b.w.edge("ISSUED_BY", ekey, ckey, match_method="structured", source=key)
        cls = (row.get("classification") or "").strip()
        if cls:
            b.w.node("RegulatoryEvent", ekey, source=key, type="recall",
                     reason=cls)      # no-op if already written; kept for clarity
    b._done("fda_recalls", t0, n)


def load_sfda_events(b):
    """Saudi safety signals and shortages - the MENA/GCC coverage that is the
    reason this lake exists rather than being a US/EU-only graph."""
    t0 = b._step("sfda_events")
    n = 0
    for i, row in enumerate(lake.stream_csv(L["sfda_alert"], limit=b.limit)):
        title = (row.get("Title") or "").strip()
        if not title:
            continue
        n += 1
        b.w.node("RegulatoryEvent", f"EVENT:SFDA_ALERT:{row.get('#', i)}",
                 source=L["sfda_alert"], type="safety_alert", name=title,
                 reason=row.get("Type of news", ""),
                 start_date=row.get("Date", ""), url=row.get("Link of news", ""))

    for row in lake.stream_csv(L["sfda_short"], limit=b.limit):
        reg = (row.get("REGISTRATION_NO") or "").strip()
        sci = (row.get("SCIENTIFICNAME_EN") or "").strip()
        if not reg:
            continue
        if not b.wanted(sci, row.get("TRADENAME_EN", "")):
            continue
        n += 1
        _event(b, f"EVENT:SHORTAGE:SFDA:{reg}", "shortage", L["sfda_short"],
               subjects=sci, sep=",",
               name=row.get("TRADENAME_EN", ""),
               status=row.get("SHORTAGE_TYPE_EN", ""),
               reason=row.get("SHORTAGE_REASON_EN", ""),
               start_date=row.get("SHORTAGE_START_DATE", ""))
    b._done("sfda_events", t0, n)


def load_faers(b):
    """12 quarterly files, aggregated to (substance, reaction) counts.

    Two decisions worth stating:

    * Only drugs marked *suspect* are counted. FAERS records concomitant
      medication on the same report, and counting those would attribute every
      reaction to whatever the patient happened to also be taking. The
      characterization column is parallel to the substance column, so they are
      zipped; where the lengths disagree the row is counted for all its drugs,
      which is the conservative reading.
    * Reactions and drugs are both multi-valued, and FAERS does not say which
      drug caused which reaction. The cross product is the only honest
      reading, and it is why counts are reported rather than treated as causal.
      The edge means "co-reported", not "caused".
    """
    t0 = b._step("faers")
    pairs: dict[tuple[str, str], list[int]] = {}
    terms: dict[str, str] = {}
    n = 0
    # One report appears on several consecutive rows - one per drug - and each
    # of those rows repeats the report's FULL reaction list. Counting them all
    # would multiply every reaction by the number of drugs on the report. Rows
    # for a report are contiguous, so a per-report seen-set bounded to the
    # current report is enough, and costs nothing in memory.
    cur_report, seen_pairs = None, set()
    for q in FAERS_QUARTERS:
        key = f"Safety_Pharmacovigilance/open.fda.gov/Adverse_Events/faers_{q}.csv"
        try:
            # Every quarter is recorded as read even though the edges carry a
            # single "faers" source id - twelve S3 keys aggregated into one
            # fact, and the coverage check needs to see all twelve.
            b.w.sid(key)
            stream = lake.stream_csv(key, limit=b.limit)
            for row in stream:
                subs = [s.strip() for s in (row.get("drug_substance") or "").split(";") if s.strip()]
                if not subs:
                    continue
                chars = [c.strip() for c in (row.get("drug_characterization") or "").split(";")]
                if len(chars) == len(subs):
                    subs = [s for s, c in zip(subs, chars) if c == "1"] or []
                if not subs:
                    continue
                reactions = [r.strip() for r in (row.get("reaction") or "").split(";") if r.strip()]
                if not reactions:
                    continue
                rid = (row.get("safetyreportid") or "").strip()
                if rid != cur_report:
                    cur_report, seen_pairs = rid, set()
                serious = 1 if (row.get("serious") or "").strip() == "1" else 0
                # openFDA encodes seriousnessdeath as 1=yes, 2=no
                death = 1 if (row.get("seriousnessdeath") or "").strip() == "1" else 0
                n += 1
                for s in subs:
                    m = b.r.resolve(s)
                    if not m.key or not m.resolved:
                        continue          # 2.9M reports of prose: no provisionals
                    for rx in reactions:
                        rf = fold(rx)
                        if len(rf) < 3:
                            continue
                        if (m.key, rf) in seen_pairs:
                            continue
                        seen_pairs.add((m.key, rf))
                        terms.setdefault(rf, rx)
                        c = pairs.get((m.key, rf))
                        if c is None:
                            pairs[(m.key, rf)] = [1, serious, death]
                        else:
                            c[0] += 1
                            c[1] += serious
                            c[2] += death
        except Exception as e:
            b.stats.setdefault("faers_missing", []).append(f"{q}: {type(e).__name__}")

    dropped = 0
    for (skey, rf), (cnt, ser, dth) in pairs.items():
        if cnt < MIN_REPORTS:
            dropped += 1
            continue
        akey = f"AE:{rf}"
        b.w.node("AdverseEvent", akey, source="faers", term=terms[rf])
        b.w.edge("HAS_ADVERSE_EVENT", skey, akey, match_method="aggregated",
                 source="faers", report_count=cnt, serious_count=ser,
                 death_count=dth)
    b.stats["faers_reports_counted"] = n
    b.stats["faers_pairs_total"] = len(pairs)
    b.stats[f"faers_pairs_dropped_below_{MIN_REPORTS}"] = dropped
    b._done("faers", t0, n)


ALL = [load_ema_events, load_recalls, load_sfda_events, load_faers]
