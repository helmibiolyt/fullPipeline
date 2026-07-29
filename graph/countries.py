"""Country names -> ISO 3166-1 alpha-2, and how to find them in trial records.

CONDUCTED_IN is the edge that answers "which trials ran in the Gulf", which is
the question this lake exists to answer, so the mapping is a real list rather
than the handful of countries the regulatory agencies happen to sit in.

Names are matched after fold(), and the common alternates registries actually
write ("USA", "Korea, South", "UK", "Viet Nam") are listed alongside the
official name rather than being normalised by rule - there is no rule that
turns "Cote d'Ivoire" into "CI" except a table.
"""
from __future__ import annotations

import re

from normalise import fold

# official name : iso2, then alternates that appear in these registries
_RAW = """
Afghanistan:AF|Albania:AL|Algeria:DZ|Andorra:AD|Angola:AO|Argentina:AR
Armenia:AM|Aruba:AW|Australia:AU|Austria:AT|Azerbaijan:AZ|Bahamas:BS
Bahrain:BH|Bangladesh:BD|Barbados:BB|Belarus:BY|Belgium:BE|Belize:BZ
Benin:BJ|Bermuda:BM|Bhutan:BT|Bolivia:BO|Bosnia and Herzegovina:BA
Botswana:BW|Brazil:BR|Brunei:BN|Brunei Darussalam:BN|Bulgaria:BG
Burkina Faso:BF|Burundi:BI|Cambodia:KH|Cameroon:CM|Canada:CA|Cape Verde:CV
Cabo Verde:CV|Central African Republic:CF|Chad:TD|Chile:CL|China:CN
Colombia:CO|Comoros:KM|Congo:CG|Costa Rica:CR|Cote d'Ivoire:CI
Ivory Coast:CI|Croatia:HR|Cuba:CU|Cyprus:CY|Czechia:CZ|Czech Republic:CZ
Denmark:DK|Djibouti:DJ|Dominica:DM|Dominican Republic:DO|Ecuador:EC
Egypt:EG|El Salvador:SV|Estonia:EE|Eswatini:SZ|Ethiopia:ET|Fiji:FJ
Finland:FI|France:FR|Gabon:GA|Gambia:GM|Georgia:GE|Germany:DE|Ghana:GH
Gibraltar:GI|Greece:GR|Greenland:GL|Guadeloupe:GP|Guam:GU|Guatemala:GT
Guinea:GN|Guinea-Bissau:GW|Guyana:GY|Haiti:HT|Honduras:HN|Hong Kong:HK
Hungary:HU|Iceland:IS|India:IN|Indonesia:ID|Iran:IR
Iran, Islamic Republic of:IR|Iraq:IQ|Ireland:IE|Israel:IL|Italy:IT
Jamaica:JM|Japan:JP|Jordan:JO|Kazakhstan:KZ|Kenya:KE|Kosovo:XK|Kuwait:KW
Kyrgyzstan:KG|Laos:LA|Lao People's Democratic Republic:LA|Latvia:LV
Lebanon:LB|Lesotho:LS|Liberia:LR|Libya:LY|Liechtenstein:LI|Lithuania:LT
Luxembourg:LU|Macao:MO|Madagascar:MG|Malawi:MW|Malaysia:MY|Maldives:MV
Mali:ML|Malta:MT|Martinique:MQ|Mauritania:MR|Mauritius:MU|Mexico:MX
Moldova:MD|Republic of Moldova:MD|Monaco:MC|Mongolia:MN|Montenegro:ME
Morocco:MA|Mozambique:MZ|Myanmar:MM|Burma:MM|Namibia:NA|Nepal:NP
Netherlands:NL|New Caledonia:NC|New Zealand:NZ|Nicaragua:NI|Niger:NE
Nigeria:NG|North Macedonia:MK|Macedonia:MK|Norway:NO|Oman:OM|Pakistan:PK
Palestine:PS|Panama:PA|Papua New Guinea:PG|Paraguay:PY|Peru:PE
Philippines:PH|Poland:PL|Portugal:PT|Puerto Rico:PR|Qatar:QA|Reunion:RE
Romania:RO|Russia:RU|Russian Federation:RU|Rwanda:RW|Saudi Arabia:SA
Senegal:SN|Serbia:RS|Seychelles:SC|Sierra Leone:SL|Singapore:SG
Slovakia:SK|Slovenia:SI|Somalia:SO|South Africa:ZA|South Korea:KR
Korea, South:KR|Korea, Republic of:KR|Republic of Korea:KR|Korea:KR
North Korea:KP|South Sudan:SS|Spain:ES|Sri Lanka:LK|Sudan:SD|Suriname:SR
Sweden:SE|Switzerland:CH|Syria:SY|Syrian Arab Republic:SY|Taiwan:TW
Tajikistan:TJ|Tanzania:TZ|United Republic of Tanzania:TZ|Thailand:TH
Timor-Leste:TL|Togo:TG|Trinidad and Tobago:TT|Tunisia:TN|Turkey:TR
Turkiye:TR|Turkmenistan:TM|Uganda:UG|Ukraine:UA|United Arab Emirates:AE
UAE:AE|United Kingdom:GB|UK:GB|Great Britain:GB|England:GB|Scotland:GB
Wales:GB|Northern Ireland:GB|United States:US|United States of America:US
USA:US|Uruguay:UY|Uzbekistan:UZ|Venezuela:VE|Vietnam:VN|Viet Nam:VN
Yemen:YE|Zambia:ZM|Zimbabwe:ZW
"""

BY_NAME: dict[str, str] = {}
NAME: dict[str, str] = {}

# Not an ISO country, but it is what the EMA row carries as its jurisdiction,
# so it needs a readable name rather than falling back to the bare code.
_EXTRA = {"EU": "European Union"}
for _entry in _RAW.replace("\n", "|").split("|"):
    _entry = _entry.strip()
    if not _entry or ":" not in _entry:
        continue
    _n, _c = _entry.rsplit(":", 1)
    BY_NAME.setdefault(fold(_n), _c.strip())
    NAME.setdefault(_c.strip(), _n.strip())
NAME.update(_EXTRA)

# Country -> Region. Without this Region is reachable only from Product, and
# "which trials ran in the Gulf" cannot be answered by traversal at all - it
# needs six ISO codes typed by hand.
#
# Two deliberate departures from pure geography, both because this graph is
# about drug regulation rather than atlases:
#
#   MENA/GCC spans Asia and Africa. It is kept because it is the grouping this
#   lake exists to serve, and it takes precedence over the continent - Egypt is
#   MENA/GCC, not Africa.
#   South Asia is separate from Asia. India alone carries ~62k trials through
#   CTRI, and folding it into "Asia" would bury that behind China and Japan.
#
# Every country gets exactly one region. Overlaps would double-count any
# aggregation that traverses this edge.
_REGIONS = {
    "North America": "US CA BM GL PR",
    "Latin America": "MX GT BZ SV HN NI CR PA CU DO HT JM TT BB BS DM AW GP MQ "
                     "CO VE EC PE BO BR PY UY AR CL GY SR",
    "Europe": "AL AD AT BY BE BA BG HR CY CZ DK EE FI FR DE GI GR HU IS IE IT "
              "XK LV LI LT LU MT MD MC ME NL MK NO PL PT RO RU RS SK SI ES SE "
              "CH UA GB EU TR",
    "MENA/GCC": "SA AE BH QA OM KW YE JO LB SY IQ IR IL PS EG LY TN DZ MA",
    "Sub-Saharan Africa": "AO BJ BW BF BI CM CV CF TD KM CG CI SZ ET GA GM GH "
                          "GN GW KE LS LR MG MW ML MU MZ NA NE NG RW RE SN SC "
                          "SL ZA SS TZ TG UG ZM ZW SO SD MR DJ",
    "Asia": "CN JP KR KP TW HK MO MN SG MY ID TH VN PH KH LA MM BN TL",
    "South Asia": "IN PK BD LK NP BT MV AF",
    "Central Asia": "KZ UZ KG TJ TM AZ AM GE",
    "Oceania": "AU NZ FJ PG NC GU",
}

REGION: dict[str, str] = {}
for _r, _codes in _REGIONS.items():
    for _c in _codes.split():
        REGION[_c] = _r


# "Mayo Clinic Hospital in Arizona (Phoenix, Arizona, United States)" - the
# country is the last comma-field inside the parentheses. Parsing the structure
# is both cheaper and safer than searching for 200 country names in free text,
# where "Georgia" and "Jordan" are also a US state and a person's name.
_PAREN = re.compile(r"\(([^()]{2,120})\)")


def from_locations(blob: str) -> set[str]:
    """ISO codes mentioned in a ClinicalTrials.gov `locations` value."""
    out = set()
    for m in _PAREN.finditer(blob or ""):
        c = BY_NAME.get(fold(m.group(1).rsplit(",", 1)[-1]))
        if c:
            out.add(c)
    return out


def from_list(blob: str, sep: str = ";|,") -> set[str]:
    """ISO codes from a plain delimited country list (WHO, ISRCTN, CTRI)."""
    out = set()
    for part in re.split(f"[{sep}]", blob or ""):
        c = BY_NAME.get(fold(part))
        if c:
            out.add(c)
    return out
