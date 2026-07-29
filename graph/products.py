"""Product loaders: the marketed items, their companies, approvals and patents.

Product never needs cross-source resolution - its key is the agency's own
licence number, which is unique by construction. Only the CONTAINS edge needs
the resolver, because ingredient names are free text.

Every loader here follows the same shape: stream the file, resolve the
ingredient name to a Substance key, emit Product + CONTAINS + identifiers, and
record on the edge which resolver tier matched.
"""
from __future__ import annotations

import re

import countries
import lake
from normalise import fold, norm_company

L = {
    "ca_drug":    "Regulatory_Approvals/health-products.canada.ca/canada_dpd_data/drug.csv",
    "ca_ingred":  "Regulatory_Approvals/health-products.canada.ca/canada_dpd_data/ingred.csv",
    "ca_comp":    "Regulatory_Approvals/health-products.canada.ca/canada_dpd_data/comp.csv",
    "ca_ther":    "Regulatory_Approvals/health-products.canada.ca/canada_dpd_data/ther.csv",
    "ca_route":   "Regulatory_Approvals/health-products.canada.ca/canada_dpd_data/route.csv",
    "ca_status":  "Regulatory_Approvals/health-products.canada.ca/canada_dpd_data/status.csv",
    "mhra":       "Regulatory_Approvals/products.mhra.gov.uk/mhra_data/raw_metadata.csv",
    "ema":        "Regulatory_Approvals/ema.europa.eu/ema_data/ema_medicines.csv",
    "ob":         "Regulatory_Approvals/accessdata.fda.gov-orangebook/orangebook_data/orange_book_unified.csv",
    "ob_patents": "Regulatory_Approvals/accessdata.fda.gov-orangebook/orangebook_data/patents_enriched.csv",
    "ob_excl":    "Regulatory_Approvals/accessdata.fda.gov-orangebook/orangebook_data/exclusivity_enriched.csv",
    "pb":         "Regulatory_Approvals/purplebooksearch.fda.gov/purplebook_data/purplebook_enriched_products.csv",
    "pb_patents": "Regulatory_Approvals/purplebooksearch.fda.gov/purplebook_data/patent_list.csv",
    "openfda":    "Regulatory_Approvals/open.fda.gov/openfda_data/openfda_drugs.csv",
    "sfda":       "MENA_GCC_Regulatory_Market/sfda.gov.sa/List_of_Registered_HumanHerbalVeterinary_Drugs.csv",
    "pmda":       "Regulatory_Approvals/pmda.go.jp/pmda_data/pmda_metadata.csv",
}

# Fixed vocabulary. Region is coarser than agency on purpose: "is this
# available in the Gulf" spans four agencies, and that is the granularity
# people ask in.
AGENCIES = [
    ("FDA",     "US Food and Drug Administration",       "US", "North America"),
    ("EMA",     "European Medicines Agency",             "EU", "Europe"),
    ("MHRA",    "Medicines and Healthcare products Regulatory Agency", "GB", "Europe"),
    ("PMDA",    "Pharmaceuticals and Medical Devices Agency", "JP", "Asia"),
    ("HC",      "Health Canada",                         "CA", "North America"),
    ("SFDA",    "Saudi Food and Drug Authority",         "SA", "MENA/GCC"),
    ("NHRA",    "National Health Regulatory Authority",  "BH", "MENA/GCC"),
    ("DHA",     "Dubai Health Authority",                "AE", "MENA/GCC"),
    ("DOH",     "Department of Health Abu Dhabi",        "AE", "MENA/GCC"),
    ("MOH-OM",  "Ministry of Health Oman",               "OM", "MENA/GCC"),
    ("MOPH-QA", "Ministry of Public Health Qatar",       "QA", "MENA/GCC"),
]

_SPLIT = re.compile(r"\s*(?:;|\||,|\band\b|\+)\s*", re.I)


def split_ingredients(raw: str) -> list[str]:
    """Combination products list several actives in one cell.

    Splitting on comma is safe here, unlike in gsrs synonyms: these are common
    names ("amoxicillin, clavulanic acid"), not IUPAC systematic names.
    """
    if not raw:
        return []
    return [p.strip() for p in _SPLIT.split(raw) if p and p.strip()]


def load_vocab(b):
    t0 = b._step("agencies/regions")
    n = 0
    for code, name, country, region in AGENCIES:
        b.w.node("RegulatoryAgency", f"AGENCY:{code}", source="vocab",
                 code=code, name=name, country=country, region=region)
        b.w.node("Region", f"REGION:{fold(region)}", source="vocab", name=region)
        b.w.node("Country", f"COUNTRY:{country}", source="vocab",
                 iso2=country, name=country)
        n += 1
    b._done("agencies/regions", t0, n)


def _product(b, key, agency, source, contains, **props):
    """Emit a Product, its agency edges, and its CONTAINS edges."""
    b.w.node("Product", key, source=source, agency=agency, **props)
    b.w.edge("APPROVED_BY", key, f"AGENCY:{agency}", match_method="derived",
             source=source)
    region = next((r for c, _, _, r in AGENCIES if c == agency), None)
    if region:
        b.w.edge("APPROVED_IN", key, f"REGION:{fold(region)}",
                 match_method="derived", source=source)
    for ing in contains:
        m = b.r.resolve(ing)
        if not m.key:
            continue
        if not m.resolved:
            # An unresolved ingredient still gets a node, or the CONTAINS edge
            # dangles and the graph fails referential integrity. These are real
            # and expected: a combination partner outside a slice, or a source
            # typo - "atorvastatin calcium trihydate" is in the MHRA data. The
            # merge pass later folds them into a resolved node when an
            # identifier connects them; until then they are visibly provisional
            # rather than silently missing.
            b.w.node("Substance", m.key, source=source, name=ing,
                     norm_name=fold(ing), resolved_by="provisional")
        b.w.edge("CONTAINS", key, m.key, match_method=m.method, source=source)


def load_canada(b):
    """Canada DPD is already relational - drug, ingred, comp, route, ther as
    separate tables joined on DRUG_CODE. The cleanest product source there is.

    Ingredients come first because in slice mode they decide which products are
    in scope, and drug.csv has no ingredient column to filter on.
    """
    t0 = b._step("canada")
    ing_by_code: dict[str, list[str]] = {}
    for row in lake.stream_csv(L["ca_ingred"], limit=b.limit):
        code = (row.get("DRUG_CODE") or "").strip()
        name = (row.get("INGREDIENT_NAME") or "").strip()
        if code and name:
            ing_by_code.setdefault(code, []).append(name)

    # Read into memory to drive the product filter; it emits no rows of its
    # own, so the read is recorded explicitly or the coverage check calls it
    # unread.
    b.w.sid(L["ca_ingred"])
    keep = {c for c, ings in ing_by_code.items() if b.wanted(*ings)}
    n = 0
    for row in lake.stream_csv(L["ca_drug"], limit=b.limit):
        code = (row.get("DRUG_CODE") or "").strip()
        din = (row.get("DRUG_IDENTIFICATION_NUMBER") or "").strip()
        if not code or not din or code not in keep:
            continue
        n += 1
        key = f"CA:{din}"
        _product(b, key, "HC", L["ca_drug"], ing_by_code.get(code, []),
                 name=row.get("BRAND_NAME", ""), brand_name=row.get("BRAND_NAME", ""),
                 status=row.get("STATUS_CATEGORY", ""),
                 form=row.get("PRODUCT_CATEGORIZATION", ""))
        b.w.identifier(key, "CA_DIN", din, source=L["ca_drug"])
        b.ca_code_key[code] = key

    for row in lake.stream_csv(L["ca_comp"], limit=b.limit):
        key = b.ca_code_key.get((row.get("DRUG_CODE") or "").strip())
        cname = (row.get("COMPANY_NAME") or "").strip()
        if not key or not cname:
            continue
        ckey = f"COMPANY:{norm_company(cname)}"
        b.w.node("Company", ckey, source=L["ca_comp"], name=cname, raw_names=cname)
        b.w.edge("DEVELOPS", ckey, key, match_method="structured", source=L["ca_comp"])

    for row in lake.stream_csv(L["ca_route"], limit=b.limit):
        key = b.ca_code_key.get((row.get("DRUG_CODE") or "").strip())
        rt = (row.get("ROUTE_OF_ADMINISTRATION_EN") or "").strip()
        if not key or not rt:
            continue
        rkey = f"ROUTE:{fold(rt)}"
        b.w.node("Route", rkey, source=L["ca_route"], name=rt)
        b.w.edge("HAS_ROUTE", key, rkey, source=L["ca_route"])

    for row in lake.stream_csv(L["ca_ther"], limit=b.limit):
        key = b.ca_code_key.get((row.get("DRUG_CODE") or "").strip())
        atc = (row.get("TC_ATC_NUMBER") or "").strip()
        if key and atc:
            if atc in b.atc_codes:
                b.w.edge("IN_CLASS", key, f"ATC:{atc}", source=L["ca_ther"])
    b._done("canada", t0, n)


def load_ca_status(b):
    """Canada's regulatory status history - the Approval nodes for HC products.

    200k rows against 59k products because this is a history, not a snapshot:
    a product moves APPROVED -> MARKETED -> DORMANT and each transition is a
    row. All of them are kept, because "when did this stop being marketed in
    Canada" is exactly the kind of question the current-status flag cannot
    answer.
    """
    t0 = b._step("canada_status")
    key = L["ca_status"]
    n = 0
    for row in lake.stream_csv(key, limit=b.limit):
        pkey = b.ca_code_key.get((row.get("DRUG_CODE") or "").strip())
        status = (row.get("STATUS_EN") or "").strip()
        date = (row.get("HISTORY_DATE") or "").strip()
        if not pkey or not status:
            continue
        n += 1
        akey = f"APPROVAL:CA:{(row.get('DRUG_CODE') or '').strip()}:{fold(status)}:{fold(date)}"
        b.w.node("Approval", akey, source=key, date=date, type="status_change",
                 status=status, agency="HC")
        b.w.edge("HAS_APPROVAL", pkey, akey, match_method="structured", source=key)
    b._done("canada_status", t0, n)


def load_pb_patents(b):
    """Biologics patents, joined on the reference product's BLA number.

    Separate from the Orange Book patent loader because biologics patents are
    listed against the BLA rather than an application/product pair, so the join
    key is different even though the node type is the same.
    """
    t0 = b._step("purplebook_patents")
    key = L["pb_patents"]
    n = 0
    for row in lake.stream_csv(key, limit=b.limit):
        bla = (row.get("Reference Product BLA Number") or "").strip()
        pno = (row.get("Patent Number") or "").strip().replace(",", "")
        if not bla or not pno:
            continue
        # Join to the product purplebook actually created, rather than
        # assuming the key exists. patent_list is not slice-filtered and the
        # product loader is, so an unconditional edge dangles for every
        # biologic outside the slice - 424 of 424 on the first attempt.
        pkey = b.bla_key.get(bla)
        if not pkey:
            continue
        n += 1
        pat = f"US:{pno}"
        b.w.node("Patent", pat, source=key, patent_no=pno,
                 expire_date=row.get("Patent Expiration Date", ""))
        b.w.edge("PROTECTED_BY", pkey, pat, match_method="structured",
                 source=key)
    b._done("purplebook_patents", t0, n)


def load_mhra(b):
    """The UK product population - the same documents the vector store indexes,
    joined here by licence number."""
    t0 = b._step("mhra")
    n = 0
    for row in lake.stream_csv(L["mhra"], limit=b.limit):
        pl = (row.get("pl_number") or "").strip()
        subs = split_ingredients(row.get("substance_name", ""))
        if not pl or not b.wanted(*subs, row.get("product_name", "")):
            continue
        n += 1
        key = f"MHRA:{pl}"
        _product(b, key, "MHRA", L["mhra"], subs,
                 name=row.get("product_name", ""),
                 brand_name=row.get("product_name", ""),
                 status=row.get("release_state", ""))
        b.w.identifier(key, "MHRA_PL", pl, source=L["mhra"])
    b._done("mhra", t0, n)


def load_ema(b):
    """39 columns and only 2,666 rows - the densest product source in the lake.
    Carries active substance, MAH, ATC code and the authorisation date."""
    t0 = b._step("ema")
    n = 0
    for row in lake.stream_csv(L["ema"], limit=b.limit):
        pnum = (row.get("ema_product_number") or "").strip()
        subs = split_ingredients(row.get("active_substance", "")
                                 or row.get("international_non_proprietary_name_inn_common_name", ""))
        name = (row.get("name_of_medicine") or "").strip()
        if not pnum or not b.wanted(*subs, name):
            continue
        n += 1
        key = f"EMA:{pnum}"
        _product(b, key, "EMA", L["ema"], subs, name=name, brand_name=name,
                 status=row.get("medicine_status", ""))
        b.w.identifier(key, "EMA_PRODUCT", pnum, source=L["ema"])
        atc = (row.get("atc_code_human") or "").strip()
        if atc:
            if atc in b.atc_codes:
                b.w.edge("IN_CLASS", key, f"ATC:{atc}", source=L["ema"])
        date = (row.get("marketing_authorisation_date") or "").strip()
        if date:
            akey = f"EMA:{pnum}:{date}"
            b.w.node("Approval", akey, source=L["ema"], date=date,
                     type="marketing_authorisation", agency="EMA",
                     status=row.get("medicine_status", ""))
            b.w.edge("HAS_APPROVAL", key, akey, source=L["ema"])
            b.w.edge("ISSUED_BY", akey, "AGENCY:EMA", match_method="derived",
                     source=L["ema"])
        mah = (row.get("marketing_authorisation_developer_applicant_holder") or "").strip()
        if mah:
            ckey = f"COMPANY:{norm_company(mah)}"
            b.w.node("Company", ckey, source=L["ema"], name=mah, raw_names=mah)
            b.w.edge("DEVELOPS", ckey, key, source=L["ema"])
    b._done("ema", t0, n)


def load_orangebook(b):
    """FDA small molecules, plus the patents and exclusivities that make the
    graph able to answer when protection ends."""
    t0 = b._step("orangebook")
    n = 0
    for row in lake.stream_csv(L["ob"], limit=b.limit):
        appl, prod = (row.get("Appl_No") or "").strip(), (row.get("Product_No") or "").strip()
        ing = split_ingredients(row.get("Ingredient", ""))
        if not appl or not prod or not b.wanted(*ing, row.get("Trade_Name", "")):
            continue
        n += 1
        key = f"FDA:{appl}:{prod}"
        dfr = (row.get("Dosage_Form_Route") or "")
        form, _, route = dfr.partition(";")
        _product(b, key, "FDA", L["ob"], ing,
                 name=row.get("Trade_Name", ""), brand_name=row.get("Trade_Name", ""),
                 strength=row.get("Strength", ""), form=form.strip())
        b.w.identifier(key, "FDA_APPL_NO", appl, source=L["ob"])
        if route.strip():
            rkey = f"ROUTE:{fold(route)}"
            b.w.node("Route", rkey, source=L["ob"], name=route.strip())
            b.w.edge("HAS_ROUTE", key, rkey, source=L["ob"])
        appr = (row.get("Approval_Date") or "").strip()
        if appr:
            akey = f"FDA:{appl}:{appr}"
            b.w.node("Approval", akey, source=L["ob"], date=appr,
                     type=row.get("Appl_Type", ""), agency="FDA")
            b.w.edge("HAS_APPROVAL", key, akey, source=L["ob"])
            b.w.edge("ISSUED_BY", akey, "AGENCY:FDA", match_method="derived",
                     source=L["ob"])
        appn = (row.get("Applicant_Full_Name") or row.get("Applicant") or "").strip()
        if appn:
            ckey = f"COMPANY:{norm_company(appn)}"
            b.w.node("Company", ckey, source=L["ob"], name=appn, raw_names=appn)
            b.w.edge("DEVELOPS", ckey, key, source=L["ob"])
        b.fda_appl_products.setdefault(appl, set()).add((prod, key))
    b._done("orangebook", t0, n)


def load_patents(b):
    """Patent and Exclusivity, joined to Product on Appl_No + Product_No.

    This is the pair that lets the graph answer "when does this lose
    protection" - a question no other layer can touch, because an SPC does not
    discuss patents.
    """
    t0 = b._step("patents/exclusivity")
    n = 0
    for row in lake.stream_csv(L["ob_patents"], limit=b.limit):
        appl, prod = (row.get("Appl_No") or "").strip(), (row.get("Product_No") or "").strip()
        pno = (row.get("Patent_No") or "").strip()
        if not pno:
            continue
        prods = {k for p, k in b.fda_appl_products.get(appl, set()) if p == prod}
        if not prods:
            continue
        n += 1
        pkey = f"PATENT:US{pno}"
        b.w.node("Patent", pkey, source=L["ob_patents"], patent_no=pno,
                 expire_date=row.get("Patent_Expire_Date_Text", ""),
                 use_code=row.get("Patent_Use_Code", ""),
                 use_definition=row.get("Patent_Use_Definition", ""),
                 drug_substance_flag=row.get("Drug_Substance_Flag", ""),
                 drug_product_flag=row.get("Drug_Product_Flag", ""))
        for k in prods:
            b.w.edge("PROTECTED_BY", k, pkey, source=L["ob_patents"])

    for row in lake.stream_csv(L["ob_excl"], limit=b.limit):
        appl, prod = (row.get("Appl_No") or "").strip(), (row.get("Product_No") or "").strip()
        code = (row.get("Exclusivity_Code") or "").strip()
        if not code:
            continue
        prods = {k for p, k in b.fda_appl_products.get(appl, set()) if p == prod}
        if not prods:
            continue
        ekey = f"EXCL:{appl}:{prod}:{code}"
        b.w.node("Exclusivity", ekey, source=L["ob_excl"], code=code,
                 date=row.get("Exclusivity_Date", ""),
                 definition=row.get("Exclusivity_Definition", ""))
        for k in prods:
            b.w.edge("HAS_EXCLUSIVITY", k, ekey, source=L["ob_excl"])
    b._done("patents/exclusivity", t0, n)


def load_purplebook(b):
    """Biologics. license_type 351(a)/351(k) plus resolved_reference_bla give
    the biosimilar -> originator edge already resolved by the source."""
    t0 = b._step("purplebook")
    n = 0
    bla_key = b.bla_key
    rows = list(lake.stream_csv(L["pb"], limit=b.limit))
    for row in rows:
        bla = (row.get("bla_number") or "").strip()
        proper = (row.get("proper_name") or "").strip()
        prop = (row.get("proprietary_name") or "").strip()
        if not bla or not b.wanted(proper, prop):
            continue
        n += 1
        key = f"FDA:BLA{bla}"
        bla_key[bla] = key
        _product(b, key, "FDA", L["pb"], split_ingredients(proper),
                 name=prop or proper, brand_name=prop,
                 strength=row.get("strength", ""),
                 form=row.get("dosage_form", ""),
                 status=row.get("marketing_status", ""))
        appr = (row.get("approval_date") or "").strip()
        if appr:
            akey = f"FDA:BLA{bla}:{appr}"
            b.w.node("Approval", akey, source=L["pb"], date=appr,
                     type=row.get("license_type", ""), agency="FDA")
            b.w.edge("HAS_APPROVAL", key, akey, source=L["pb"])
        for col, label in (("exclusivity_expiration_date", "exclusivity"),
                           ("ref_product_exclusivity_exp_date", "reference_product"),
                           ("orphan_exclusivity_exp_date", "orphan")):
            d = (row.get(col) or "").strip()
            if d:
                ekey = f"EXCL:BLA{bla}:{label}"
                b.w.node("Exclusivity", ekey, source=L["pb"], code=label, date=d,
                         definition=col)
                b.w.edge("HAS_EXCLUSIVITY", key, ekey, source=L["pb"])
        for pno in [p.strip() for p in (row.get("patent_numbers") or "").split(",") if p.strip()]:
            pkey = f"PATENT:US{pno}"
            b.w.node("Patent", pkey, source=L["pb"], patent_no=pno)
            b.w.edge("PROTECTED_BY", key, pkey, source=L["pb"])
    # second pass: biosimilar -> reference, once every BLA has a key
    for row in rows:
        bla = (row.get("bla_number") or "").strip()
        ref = (row.get("resolved_reference_bla") or "").strip()
        if bla in bla_key and ref and ref in bla_key and ref != bla:
            b.w.edge("BIOSIMILAR_OF", bla_key[bla], bla_key[ref], source=L["pb"])
    b._done("purplebook", t0, n)


def load_openfda(b):
    t0 = b._step("openfda")
    n = 0
    for row in lake.stream_csv(L["openfda"], limit=b.limit):
        appl = (row.get("application_number") or "").strip()
        pno = (row.get("product_number") or "").strip()
        subs = split_ingredients(row.get("openfda_substance_names", "")
                                 or row.get("active_ingredients", ""))
        if not appl or not b.wanted(*subs, row.get("brand_name", "")):
            continue
        n += 1
        key = f"FDA:{appl}:{pno}" if pno else f"FDA:{appl}"
        _product(b, key, "FDA", L["openfda"], subs,
                 name=row.get("brand_name", ""), brand_name=row.get("brand_name", ""),
                 form=row.get("dosage_form", ""),
                 status=row.get("marketing_status", ""))
        for ndc in [x.strip() for x in (row.get("openfda_product_ndcs") or "").split(",") if x.strip()][:5]:
            b.w.identifier(key, "NDC", ndc, source=L["openfda"])
    b._done("openfda", t0, n)


def load_sfda(b):
    """Saudi registrations. Headers carry a UTF-8 BOM; lake.stream_csv strips it."""
    t0 = b._step("sfda")
    n = 0
    for row in lake.stream_csv(L["sfda"], limit=b.limit):
        reg = (row.get("registerNumber") or "").strip()
        sci = (row.get("scientificName") or "").strip()
        trade = (row.get("tradeName") or "").strip()
        if not reg or not b.wanted(sci, trade):
            continue
        n += 1
        key = f"SFDA:{reg}"
        _product(b, key, "SFDA", L["sfda"], split_ingredients(sci),
                 name=trade, brand_name=trade,
                 strength=row.get("strength", ""),
                 form=row.get("pharmaceuticalForm", ""),
                 status=row.get("marketingStatus", ""))
        for atc in (row.get("atcCode1", ""), row.get("atcCode2", "")):
            if (atc or "").strip():
                if atc.strip() in b.atc_codes:
                    b.w.edge("IN_CLASS", key, f"ATC:{atc.strip()}", source=L["sfda"])
        rt = (row.get("administrationRoute") or "").strip()
        if rt:
            b.w.node("Route", f"ROUTE:{fold(rt)}", source=L["sfda"], name=rt)
            b.w.edge("HAS_ROUTE", key, f"ROUTE:{fold(rt)}", source=L["sfda"])
        comp = (row.get("company") or "").strip()
        if comp:
            ckey = f"COMPANY:{norm_company(comp)}"
            b.w.node("Company", ckey, source=L["sfda"], name=comp, raw_names=comp)
            b.w.edge("DEVELOPS", ckey, key, source=L["sfda"])
    b._done("sfda", t0, n)


def load_pmda(b):
    t0 = b._step("pmda")
    n = 0
    for row in lake.stream_csv(L["pmda"], limit=b.limit):
        pid = (row.get("id") or "").strip()
        gen = (row.get("generic_name") or "").strip()
        brand = (row.get("brand_name") or "").strip()
        if not pid or not b.wanted(gen, brand):
            continue
        n += 1
        key = f"PMDA:{pid}"
        _product(b, key, "PMDA", L["pmda"], split_ingredients(gen),
                 name=brand or gen, brand_name=brand)
        date = (row.get("approval_date") or "").strip()
        if date:
            akey = f"PMDA:{pid}:{date}"
            b.w.node("Approval", akey, source=L["pmda"], date=date,
                     type=row.get("approval_type", ""), agency="PMDA")
            b.w.edge("HAS_APPROVAL", key, akey, source=L["pmda"])
            b.w.edge("ISSUED_BY", akey, "AGENCY:PMDA", match_method="derived",
                     source=L["pmda"])
    b._done("pmda", t0, n)


ALL = [load_vocab, load_canada, load_ca_status, load_mhra, load_ema, load_orangebook,
       load_patents, load_purplebook, load_pb_patents, load_openfda, load_sfda, load_pmda]
