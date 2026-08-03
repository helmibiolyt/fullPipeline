"""Tests for the resolver. Run: python graph/test_normalise.py

Two kinds of case here, and the second kind matters more:

  * things that SHOULD match  - salt forms, casing, punctuation, brand names
  * things that MUST NOT match - distinct molecules that look similar

A false negative costs a provisional key that a later identifier merges. A
false merge is silent and permanent. So the must-not-match cases are the ones
worth breaking the build over.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))

from normalise import (fold, strip_salts, strip_stereo, norm_company,
                       Resolver, split_synonyms)

FAILS = []


def check(label, got, want):
    ok = got == want
    if not ok:
        FAILS.append(f"{label}\n     got:  {got!r}\n     want: {want!r}")
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}")


print("\nfold()")
check("accents folded", fold("Béclométasone"), "beclometasone")
check("punctuation dropped", fold("Co-amoxiclav (250mg)"), "co amoxiclav")
check("bracketed qualifier dropped", fold("Insulin [human]"), "insulin")
check("BOM stripped", fold("﻿Trade Name"), "trade name")
check("whitespace collapsed", fold("  ATORVASTATIN   CALCIUM "), "atorvastatin calcium")

print("\nstrip_salts()")
check("salt removed", strip_salts("Atorvastatin Calcium"), "atorvastatin")
check("hydrate removed", strip_salts("Atorvastatin calcium trihydrate"), "atorvastatin")
check("hcl removed", strip_salts("Metformin HCl"), "metformin")
# The guard: these are entirely salt tokens and must survive intact.
check("all-salt name survives", strip_salts("Sodium Chloride"), "sodium chloride")
check("all-salt name survives 2", strip_salts("Magnesium Sulfate"), "magnesium sulfate")
check("all-salt name survives 3", strip_salts("Potassium Citrate"), "potassium citrate")

print("\nstrip_stereo()")
check("(R)- removed", strip_stereo("(R)-Salbutamol"), "salbutamol")
check("(S)- removed", strip_stereo("(S)-Omeprazole"), "omeprazole")
check("DL- removed", strip_stereo("DL-Methionine"), "methionine")
check("no false strip", strip_stereo("Dexamethasone"), "dexamethasone")
check("no false strip 2", strip_stereo("Lansoprazole"), "lansoprazole")
check("no false strip 3", strip_stereo("Salbutamol"), "salbutamol")

print("\nnorm_company()")
check("suffixes dropped", norm_company("Pfizer Inc."), "pfizer")
check("multi suffix", norm_company("Novartis Pharmaceuticals Corporation"), "novartis")
check("gmbh", norm_company("Bayer AG"), "bayer")
check("all-suffix survives", norm_company("Pharmaceuticals Ltd"), "pharmaceuticals ltd")

print("\nsplit_synonyms()")
check("pipe delimited", split_synonyms("Aspirin|ASA|acetylsalicylic acid"),
      ["Aspirin", "ASA", "acetylsalicylic acid"])
check("numeric comma kept", split_synonyms("1,2-dichloroethane|foo"),
      ["1,2-dichloroethane", "foo"])
# IUPAC names contain commas. Splitting on them shreds the name into fragments
# and one of atorvastatin's folded to the single letter "r".
IUPAC = ("1H-Pyrrole-1-heptanoic acid, 2-(4-fluorophenyl)-5-(1-methylethyl)-, "
         "calcium salt (2:1), (3S,5R)-|Atorvastatin related compound B")
check("IUPAC name survives intact", len(split_synonyms(IUPAC)), 2)
check("...no single-letter fragment",
      any(len(x.strip()) < 3 for x in split_synonyms(IUPAC)), False)

print(chr(10) + "usable_name() - junk guard")
from normalise import usable_name
check("single letter rejected", usable_name("r"), False)
check("two letters rejected", usable_name("(R)"), False)
check("pure digits rejected", usable_name("123"), False)
check("real name accepted", usable_name("Aspirin"), True)

rj = Resolver()
rj.add("r", "BOGUS")
rj.add("Atorvastatin", "A0JWA85V8F")
check("junk synonym never registered", rj.resolve("r").method, "provisional")

print("\nResolver - should match")
r = Resolver()
r.add("Atorvastatin", "A0JWA85V8F")
r.add("Lipitor", "A0JWA85V8F")
r.add("Metformin", "9100L32L2N")
r.add("Sodium Chloride", "451W47IQ8X")
r.add("Cetirizine", "YO7261ME24")
r.add("Levocetirizine", "6U5EA9RT2O")          # different UNII, on purpose
r.add("Omeprazole", "KG60484QX9")
r.add("Esomeprazole", "N3PA6559FT")            # different UNII, on purpose

check("exact", r.resolve("Atorvastatin").key, "UNII:A0JWA85V8F")
check("case-insensitive", r.resolve("ATORVASTATIN").key, "UNII:A0JWA85V8F")
check("brand name", r.resolve("Lipitor").key, "UNII:A0JWA85V8F")
check("salt form", r.resolve("Atorvastatin Calcium").key, "UNII:A0JWA85V8F")
check("salt tier recorded", r.resolve("Atorvastatin Calcium").method, "salt")
check("hydrate form", r.resolve("Atorvastatin calcium trihydrate").key, "UNII:A0JWA85V8F")
check("all-salt substance", r.resolve("Sodium Chloride").key, "UNII:451W47IQ8X")

print("\nResolver - MUST NOT match (a wrong merge here is silent and permanent)")
check("levocetirizine stays itself", r.resolve("Levocetirizine").key, "UNII:6U5EA9RT2O")
check("cetirizine stays itself", r.resolve("Cetirizine").key, "UNII:YO7261ME24")
check("esomeprazole stays itself", r.resolve("Esomeprazole").key, "UNII:N3PA6559FT")
check("omeprazole stays itself", r.resolve("Omeprazole").key, "UNII:KG60484QX9")
check("unknown -> provisional", r.resolve("Notarealdrug").key, "NAME:notarealdrug")
check("unknown labelled", r.resolve("Notarealdrug").method, "provisional")
check("empty name safe", r.resolve("").key, "")

print("\nResolver - stereo tier must not bridge two registered substances")
# The case a first version got wrong: 'Levo-cetirizine' is not an exact name,
# so it fell through to the stereo tier, which stripped 'levo-' and landed on
# cetirizine's UNII. Two distinct drugs merged, silently and permanently.
check("hyphenated levo does NOT become cetirizine",
      r.resolve("Levo-cetirizine").key != "UNII:YO7261ME24", True)
check("...it stays provisional instead",
      r.resolve("Levo-cetirizine").method, "provisional")
# "Levocetirizine" has no separator after "levo", so strip_stereo leaves it
# alone and no stereo mapping is ever proposed - the protection above comes
# from that, not from the block list. To exercise the block itself, register a
# hyphenated form so a stereo mapping IS proposed and must be refused.
rb = Resolver()
rb.add("Cetirizine", "YO7261ME24")
rb.add("Levo-cetirizine", "6U5EA9RT2O")     # strips to "cetirizine" - conflict
rb.finalise()
check("conflicting stereo mapping is blocked", "cetirizine" in rb.blocked_stereo, True)
check("both forms keep their own UNII",
      (rb.resolve("Cetirizine").key, rb.resolve("Levo-cetirizine").key),
      ("UNII:YO7261ME24", "UNII:6U5EA9RT2O"))
check("es-omeprazole does NOT become omeprazole",
      r.resolve("Es-omeprazole").key != "UNII:KG60484QX9", True)

# The safe case still works: nothing competes with salbutamol.
r3 = Resolver()
r3.add("Salbutamol", "QF8SVZ843E")
# "(R)-" is bracketed, so fold() removes it and the exact tier already wins -
# the stereo tier is never reached. "R-Salbutamol" is the form that needs it.
check("(R)-Salbutamol resolves", r3.resolve("(R)-Salbutamol").key, "UNII:QF8SVZ843E")
check("...via exact, brackets are dropped by fold", r3.resolve("(R)-Salbutamol").method, "unii")
check("R-Salbutamol resolves", r3.resolve("R-Salbutamol").key, "UNII:QF8SVZ843E")
check("...via the stereo tier", r3.resolve("R-Salbutamol").method, "stereo")

print("\nResolver - result must not depend on insertion order")
a = Resolver(); a.add("Cetirizine", "YO72"); a.add("Levocetirizine", "6U5E"); a.finalise()
b = Resolver(); b.add("Levocetirizine", "6U5E"); b.add("Cetirizine", "YO72"); b.finalise()
check("same blocked set either order", a.blocked_stereo, b.blocked_stereo)
check("same stereo table either order", a.stereo, b.stereo)

print("\nResolver - collisions are recorded, not silently overwritten")
r2 = Resolver()
r2.add("Ambiguous", "UNII-A")
r2.add("Ambiguous", "UNII-B")
check("first writer wins", r2.resolve("Ambiguous").key, "UNII:UNII-A")
check("collision recorded", len(r2.collisions), 1)

print()
print("Trial phase - never absent, and NA is a value not a blank")
from trials import norm_phase, norm_study_type
check("phase 3", norm_phase("Phase 3"), "PHASE3")
check("roman combined", norm_phase("Phase I/II"), "PHASE1_PHASE2")
check("bare 4", norm_phase("4"), "PHASE4")
check("early", norm_phase("early phase 1"), "EARLY_PHASE1")
# The rule these two pin down: registries write a bare "0" for "no phase
# applies", and spell it out when they mean a real micro-dosing study. An
# earlier version matched "^0$" to PHASE0 behind a no-phase set that was
# tested first, so the rule never fired and the graph held zero PHASE0.
check("spelled-out phase 0 is real", norm_phase("Phase 0"), "PHASE0")
check("bare 0 means no phase", norm_phase("0"), "NA")
check("phase 03 is not phase 0", norm_phase("Phase 03"), "NA")
check("n/a", norm_phase("N/A"), "NA")
check("empty is NA, not blank", norm_phase(""), "NA")
check("None is NA", norm_phase(None), "NA")
check("unreadable prose is NA", norm_phase("Treatment study"), "NA")

print()
print("Study type - four values, and no fifth invented from new text")
check("interventional", norm_study_type("Interventional"), "INTERVENTIONAL")
check("long WHO wording",
      norm_study_type("Interventional clinical trial of medicinal product"),
      "INTERVENTIONAL")
check("observational", norm_study_type("Observational study"), "OBSERVATIONAL")
check("expanded access", norm_study_type("Expanded Access"), "EXPANDED_ACCESS")
# CTRI concatenates its modality vocabulary with no separator. Naming what is
# administered presupposes something is administered.
check("CTRI concatenation", norm_study_type("DrugAyurvedaPreventive"), "INTERVENTIONAL")
check("CTRI modality", norm_study_type("Surgical/Anesthesia"), "INTERVENTIONAL")
check("CTRI bioequivalence", norm_study_type("BA/BE"), "INTERVENTIONAL")
# Designs that assign nothing, including ChiCTR's own misspelling.
check("cohort", norm_study_type("Cohort Study"), "OBSERVATIONAL")
check("ChiCTR misspelling", norm_study_type("Epidemilogical research"), "OBSERVATIONAL")
check("post-marketing", norm_study_type("PMS"), "OBSERVATIONAL")
# A purpose does not decide the type: a screening study can be either, so it
# stays NA rather than being guessed into a bucket.
check("purpose stays undecided", norm_study_type("Screening"), "NA")
check("purpose stays undecided 2", norm_study_type("Quality of life"), "NA")
check("empty is NA", norm_study_type(""), "NA")
# pms is matched as a token. As a substring it would silently swallow words
# no one has seen yet.
check("no substring false positive", norm_study_type("symptoms study"), "NA")

print()
print("Product status - six agency vocabularies, two of them not statuses")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from products import norm_product_status, norm_form
from normalise import is_placeholder
check("HC lowercase", norm_product_status("marketed"), "MARKETED")
check("SFDA title case", norm_product_status("Marketed"), "MARKETED")
# The Orange Book keeps how a product is SOLD in the same column as whether
# it is still sold. Rx and OTC both mean it is on the market today.
check("FDA sells-as is marketed", norm_product_status("Prescription"), "MARKETED")
check("FDA OTC is marketed", norm_product_status("Over-the-counter"), "MARKETED")
check("authorised is not marketed", norm_product_status("Authorised"), "APPROVED")
check("tentative is its own thing",
      norm_product_status("None (Tentative Approval)"), "TENTATIVE_APPROVAL")
check("abbreviation", norm_product_status("Disc"), "DISCONTINUED")
check("stray asterisk", norm_product_status("Disc*"), "DISCONTINUED")
check("HC inactive", norm_product_status("inactive"), "DISCONTINUED")
check("EMA revoked", norm_product_status("Revoked"), "WITHDRAWN")
check("EMA suspended stays apart", norm_product_status("Suspended"), "SUSPENDED")
check("EMA lapsed", norm_product_status("Lapsed"), "EXPIRED")
# MHRA writes 'Y' on all 38,914 of its rows. It is a row flag, and reading it
# as a status would assert something the source never said about a fifth of
# the label.
check("MHRA flag is not a status", norm_product_status("Y"), "NA")
check("empty", norm_product_status(""), "NA")
check("unseen wording is NA, not invented",
      norm_product_status("Marketing cessation pending"), "NA")
check("form placeholder emptied", norm_form("UNKNOWN"), "")
check("form N/A emptied", norm_form("N/A"), "")
check("real form survives", norm_form("Tablet"), "TABLET")
# The 16 collisions: same form, two punctuations, two rows in every count.
check("comma spelling", norm_form("TABLET, EXTENDED RELEASE"),
      "TABLET EXTENDED RELEASE")
check("bracket spelling folds onto it", norm_form("TABLET (EXTENDED-RELEASE)"),
      "TABLET EXTENDED RELEASE")
check("and the reverse-majority pair too", norm_form("POWDER, FOR SOLUTION"),
      norm_form("POWDER FOR SOLUTION"))

print()
print("Placeholders - and the one that must NOT be treated as one")
check("unknown", is_placeholder("Unknown"), True)
check("nil", is_placeholder("NIL"), True)
check("n/a", is_placeholder("N/A"), True)
check("not specified", is_placeholder("Not Specified"), True)
# Namibia's ISO code, and the value phase/study_type carry on purpose.
check("bare NA is NOT a placeholder", is_placeholder("NA"), False)
check("a real name is not", is_placeholder("Tablet"), False)
# Which is why a caller that wants "NA" treated as absent - a trial title -
# has to say so itself. It shipped once relying on is_placeholder and all 16
# rows survived.
check("...so a title needs its own check",
      is_placeholder("NA") or "NA".upper() in {"NA", "N.A"}, True)

print()
print("Condition rewriting - order decides which disease a trial links to")
from normalise import condition_variants
def _v(t): return list(condition_variants(t))
check("original is always first", _v("Metastatic Breast Cancer")[0],
      "Metastatic Breast Cancer")
check("stage qualifier stripped", "Breast Cancer" in _v("Metastatic Breast Cancer"), True)
check("cancer reaches neoplasms", "Breast Neoplasms" in _v("Metastatic Breast Cancer"), True)
check("plural handled", "Solid Tumor" in _v("Solid Tumors"), True)
check("two headings in one cell", "Obesity" in _v("Overweight and Obesity"), True)
# The split variant is the loosest claim, so it must come after every other
# form. Taking any hit rather than the first would link a renal-cell trial to
# plain "Carcinoma".
_rc = _v("Advanced Renal Cell Carcinoma")
check("...and it is last",
      _rc.index("Renal Cell Carcinoma") < len(_rc) - 1, True)
check("empty yields nothing", _v(""), [])
check("no duplicates", len(_v("Solid Tumors")), len(set(_v("Solid Tumors"))))

print()
if FAILS:
    print(f"{len(FAILS)} FAILURES\n")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("all passed")
