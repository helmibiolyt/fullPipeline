"""Name normalisation and substance resolution.

Everything in the graph depends on this module. If it maps two different
molecules to one key, the graph merges them and no later step notices.

`GRAPH_HOW.md` specified a single aggressive `norm()` that stripped salts and
stereochemistry in one pass. That is not safe, for two reasons found while
writing the tests:

  * "sodium chloride" is entirely salt tokens - stripping them leaves the empty
    string, which would collapse every all-salt substance into one node.
  * levocetirizine and cetirizine are different substances with different
    UNIIs; stripping the stereo prefix merges them. Same for esomeprazole and
    omeprazole, levofloxacin and ofloxacin.

So normalisation is tiered instead. Each tier is tried in turn and the tier
that matched is recorded on the result, which means precision is measurable
afterwards rather than assumed:

    exact   casefold + punctuation only          safest
    salt    + salt tokens removed                safe: salt forms share a parent
    stereo  + stereo prefix removed              risky: recorded, never silent

The strong identifiers (UNII, InChIKey, RXCUI, CHEMBL_ID) remain the only thing
allowed to assert that two nodes are the same substance. Name matching finds
candidates; identifiers decide.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Salt and hydrate tokens. Removing these is safe *as a fallback tier* because
# a salt form and its parent are the same active moiety - but see SALT_ONLY
# below for the case where the whole name is salts.
SALTS = {
    "hydrochloride", "hcl", "hydrobromide", "sodium", "potassium", "calcium",
    "magnesium", "zinc", "aluminium", "aluminum", "lithium", "sulfate",
    "sulphate", "mesylate", "mesilate", "maleate", "tartrate", "citrate",
    "acetate", "phosphate", "succinate", "fumarate", "besylate", "besilate",
    "tosylate", "bromide", "chloride", "iodide", "nitrate", "oxalate",
    "malate", "lactate", "gluconate", "carbonate", "bicarbonate", "stearate",
    "palmitate", "valerate", "propionate", "benzoate", "salicylate",
    "dihydrate", "monohydrate", "trihydrate", "hemihydrate", "anhydrous",
    "hydrate", "sesquihydrate", "disodium", "dipotassium", "hydroxide",
}

# Stereochemistry and racemate prefixes. Deliberately anchored and explicit -
# the pattern in GRAPH_HOW.md was a character class, [rsd|l|dl|rac], which
# matches any mixture of those letters and would strip real name prefixes.
STEREO_PREFIX = re.compile(
    r"^\(?(?:[+-]|\+/-|±|r|s|rs|sr|d|l|dl|ld|rac|racemic|levo|dextro)\)?[-\s]+",
    re.IGNORECASE,
)

# Looser variant, used ONLY to detect conflicts - never to match. It allows the
# prefix to run straight into the name with no separator, which is how the
# real risk is spelled: "levocetirizine", "esomeprazole", "esketamine". Single
# letters are excluded here because "salbutamol" would lose its "s".
# Over-stripping is harmless for this purpose: a bogus result like
# "estradiol" -> "tradiol" only blocks something if "tradiol" is itself a
# registered substance, which it is not.
_STEREO_LOOSE = re.compile(r"^\(?(?:levo|dextro|racemic|rac|es|ar)\)?[-\s]*",
                           re.IGNORECASE)


def _loose_strip(s: str) -> str:
    return fold(_STEREO_LOOSE.sub("", fold(s)))

# Legal-entity suffixes, for Company only.
COMPANY_SUFFIXES = {
    "inc", "inc.", "ltd", "ltd.", "llc", "llp", "lp", "gmbh", "plc", "sa",
    "s.a", "nv", "n.v", "bv", "b.v", "ag", "co", "co.", "corp", "corp.",
    "corporation", "limited", "company", "holdings", "group", "international",
    "pharma", "pharms", "pharmaceutical", "pharmaceuticals", "laboratories",
    "laboratory", "labs", "lab", "healthcare", "health", "therapeutics",
    "biosciences", "bioscience", "biotech", "sciences", "science", "srl",
    "spa", "s.p.a", "oy", "ab", "as", "a/s", "aps", "kk", "k.k", "pty",
    "pvt", "private", "usa", "us", "uk", "europe", "eu",
}

_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)
_WS = re.compile(r"\s+")


def fold(s: str) -> str:
    """Unicode-fold, lowercase, strip punctuation, collapse whitespace.

    The floor everything else builds on. Deliberately does not remove tokens -
    "atorvastatin calcium" stays two words here.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = s.encode("ascii", "ignore").decode()          # drops accents
    s = s.lower().replace("﻿", "")               # SFDA headers carry a BOM
    s = re.sub(r"\[.*?\]|\(.*?\)", " ", s)            # bracketed qualifiers
    s = _PUNCT.sub(" ", s)
    return _WS.sub(" ", s).strip()


def strip_salts(s: str) -> str:
    """Remove salt/hydrate tokens - unless that would leave nothing.

    "sodium chloride" and "magnesium sulfate" are entirely salt tokens. They are
    real substances, so the guard returns the folded name unchanged rather than
    the empty string. Without it every all-salt substance collapses to one node.
    """
    toks = [t for t in fold(s).split() if t not in SALTS]
    return " ".join(toks) if toks else fold(s)


def strip_stereo(s: str) -> str:
    """Remove a leading stereochemistry or racemate marker.

    Risky on purpose: levocetirizine/cetirizine and esomeprazole/omeprazole are
    distinct substances with distinct UNIIs. Only ever used as the last tier,
    and the match is labelled `stereo` so it can be audited.
    """
    return fold(STEREO_PREFIX.sub("", fold(s)))


def norm_company(s: str) -> str:
    """Company key: fold, then drop legal and descriptive suffixes.

    Company is the weakest node type in the schema - no source carries a
    registry identifier, so this is clustering, not resolution. Kept separate
    from substance normalisation because the rules have nothing in common.
    """
    toks = [t for t in fold(s).split() if t not in COMPANY_SUFFIXES]
    return " ".join(toks) if toks else fold(s)


@dataclass
class Match:
    """The outcome of resolving a name. `method` is carried onto the edge."""
    key: str
    method: str          # unii | salt | stereo | provisional
    matched_name: str = ""

    @property
    def resolved(self) -> bool:
        return self.method != "provisional"


@dataclass
class Resolver:
    """name -> Substance key, built once and shared by every loader.

    Three lookup tables rather than one, so a hit records which tier found it.
    First writer wins in each table: gsrs preferred names are loaded before
    synonyms, and synonyms before chembl, so the most authoritative source
    holds the mapping.

    The stereo tier is built by finalise(), not by add(), because it needs to
    see every name before it is safe to build. See _build_stereo.
    """
    exact: dict[str, str] = field(default_factory=dict)
    salt: dict[str, str] = field(default_factory=dict)
    stereo: dict[str, str] = field(default_factory=dict)
    # Names that map to a node key directly rather than to a UNII. ChEMBL
    # synonyms belong here: they identify a molregno, and the substance that
    # molregno became may have no UNII at all. Last tier, so a gsrs name always
    # wins over a ChEMBL research code.
    alias: dict[str, str] = field(default_factory=dict)
    collisions: list[tuple[str, str, str]] = field(default_factory=list)
    blocked_stereo: set[str] = field(default_factory=set)
    _pending: list[tuple[str, str]] = field(default_factory=list)
    _final: bool = False

    def add_alias(self, name: str, key: str) -> None:
        """Register name -> an arbitrary node key. Never overrides a real
        identifier match; see `alias`."""
        if not name or not key:
            return
        e = fold(name)
        if e and usable_name(e) and e not in self.exact:
            self.alias.setdefault(e, key)

    def add(self, name: str, unii: str) -> None:
        """Register one name -> UNII mapping. Stereo is deferred to finalise()."""
        if not name or not unii:
            return
        e = fold(name)
        # Length guard: a one- or two-character "synonym" is a parsing artefact,
        # and registering it would match everything.
        if not e or not usable_name(e):
            return
        if e in self.exact and self.exact[e] != unii:
            # Two substances share a name. Real and not rare - keep the first
            # (most authoritative source) and record it rather than overwrite.
            self.collisions.append((e, self.exact[e], unii))
        else:
            self.exact.setdefault(e, unii)
        self.salt.setdefault(strip_salts(name), unii)
        self._pending.append((name, unii))
        self._final = False

    def finalise(self) -> "Resolver":
        """Build the stereo tier once every name is known.

        Two things have to be true at the same time, which is why this cannot
        happen in add():

        * The table must hold the PLAIN form, because the prefix is usually on
          the query, not the registered name. gsrs registers "Salbutamol"; a
          source writes "R-Salbutamol". So "salbutamol" -> UNII must be present.
        * That same entry must not let "Levo-cetirizine" reach cetirizine's
          UNII. Whether it would depends on whether levocetirizine is separately
          registered - which may not be known until every name is loaded.

        So: propose an entry for every name, then withdraw any whose key could
        bridge two distinct substances.
        """
        self.stereo.clear()
        self.blocked_stereo.clear()
        cand: dict[str, set[str]] = {}

        for name, unii in self._pending:
            st = strip_stereo(name)
            if st:
                cand.setdefault(st, set()).add(unii)
            # Conflict detection uses the looser pattern, so the no-separator
            # spellings ("levocetirizine") are caught even though the strict
            # matcher leaves them alone.
            lo = _loose_strip(name)
            if lo and lo != fold(name):
                other = self.exact.get(lo)
                if other is not None and other != unii:
                    self.blocked_stereo.add(lo)

        # Any key two different substances both reduce to is ambiguous.
        for st, uniis in cand.items():
            if len(uniis) > 1:
                self.blocked_stereo.add(st)

        for st, uniis in cand.items():
            if st not in self.blocked_stereo:
                self.stereo[st] = next(iter(uniis))
        self._final = True
        return self

    def resolve(self, name: str) -> Match:
        """Tiered lookup. Never returns None - an unresolved name gets a
        provisional NAME: key so the row is kept and can be merged later."""
        if not self._final:
            self.finalise()
        if not name or not fold(name):
            return Match(key="", method="provisional")
        e = fold(name)
        u = self.exact.get(e)
        if u:
            return Match(f"UNII:{u}", "unii", e)
        s = strip_salts(name)
        u = self.salt.get(s)
        if u:
            return Match(f"UNII:{u}", "salt", s)
        t = strip_stereo(name)
        u = self.stereo.get(t)
        if u:
            return Match(f"UNII:{u}", "stereo", t)
        a = self.alias.get(e)
        if a:
            return Match(a, "synonym", e)
        return Match(f"NAME:{e}", "provisional", e)

    def stats(self) -> dict:
        if not self._final:
            self.finalise()
        return {"exact": len(self.exact), "salt": len(self.salt),
                "stereo": len(self.stereo), "collisions": len(self.collisions),
                "blocked_stereo": len(self.blocked_stereo)}


# A synonym has to be at least this long to be usable. Shorter strings are
# always fragments or bare stereo descriptors, and registering one is actively
# harmful: it matches far too much.
MIN_SYNONYM = 3


def split_synonyms(raw: str) -> list[str]:
    """Split a packed synonyms cell. Pipe and semicolon only - NEVER comma.

    gsrs delimits with '|'. Commas inside these values belong to IUPAC
    systematic names:

        1H-Pyrrole-1-heptanoic acid, 2-(4-fluorophenyl)-...-, calcium salt (2:1), (BS,dR)-

    An earlier version split on commas with a digit guard, which kept "1,1-"
    intact but still shredded that name into fragments - one of which folds to
    the single letter "r" and was registered as a synonym for atorvastatin
    calcium. Any row anywhere containing a stray "r" would then have resolved
    to atorvastatin. That is the whole class of bug this module exists to
    prevent, so: no comma splitting.
    """
    if not raw:
        return []
    parts = re.split(r"[|;]", raw)
    return [p.strip() for p in parts if p and p.strip()]


def usable_name(s: str) -> bool:
    """Reject strings too short or too featureless to be a real name."""
    f = fold(s)
    return len(f) >= MIN_SYNONYM and not f.isdigit()


# Strings a source writes to mean "we do not know", which become data if
# stored: a Modality named 'Unknown', a Route named 'NIL', a dosage form of
# 'N/A'. Each one is a node or a value that answers a question it should not
# have been able to answer.
#
# Deliberately NOT here: "NA" as a bare string. It is Namibia's ISO code, and
# it is the value ClinicalTrial.phase and study_type carry on purpose. A
# caller that wants it treated as absent must say so at the call site.
_PLACEHOLDER = {
    "unknown", "n/a", "n.a.", "nil", "none", "null", "not applicable",
    "not specified", "not available", "not stated", "unspecified", "-", "--",
    "?", "tbd", "no data", "unassigned",
}


def is_placeholder(s: str) -> bool:
    """True when a value means 'we do not know' rather than naming a thing."""
    return " ".join((s or "").split()).lower() in _PLACEHOLDER


# ---------------------------------------------------------------- conditions

# Trials write a condition the way a protocol writes it; MeSH indexes it the
# way a librarian does. Measured on ct.gov's 596,490 rows: 73.8% match
# outright and 3.9% more match after one of these rewritings. Nothing here
# invents a link - every variant still has to hit the real dictionary.
#
# Stage and course qualifiers. "Metastatic Breast Cancer" is a breast
# neoplasm; the qualifier narrows it but does not change which disease it is.
_COND_QUALIFIER = re.compile(
    r"^(?:metastatic|advanced|locally advanced|recurrent|refractory|relapsed|"
    r"unresectable|newly diagnosed|previously treated|untreated|early|late|"
    r"acute|chronic|severe|mild|moderate|unspecified|adult|adults|"
    r"paediatric|pediatric|childhood|primary|secondary|"
    r"stage [0-9ivx]+)[ ]+", re.I)

# The single biggest vocabulary difference between protocols and MeSH.
_COND_CANCER = re.compile(
    r"(?<![a-z])(cancers?|tumou?rs?|malignanc(?:y|ies))(?![a-z])", re.I)

_COND_SPLIT = re.compile(r"[ ]+and[ ]+|[ ]*[/][ ]*", re.I)

# Category words that are real MeSH headings and useless as a link. Stripping
# a qualifier off "Chronic Disease" leaves "Disease", which matched and put
# 1,278 trials on a node that says nothing about any of them. Checked only on
# REWRITTEN forms: a trial whose condition is literally "Disease" still
# matches on the first tier, because that is what the registry actually said.
_COND_TOO_GENERIC = {
    "disease", "diseases", "disorder", "disorders", "syndrome", "syndromes",
    "condition", "conditions", "illness", "illnesses", "symptom", "symptoms",
    "infection", "infections", "injury", "injuries", "complication",
    "complications", "patients", "healthy", "health",
}


def condition_variants(term: str):
    """Rewritings of a trial's condition worth trying against the dictionary.

    Yields the original first, then progressively looser forms, so a caller
    taking the FIRST hit gets the most specific match available. Order is the
    whole design: taking any hit rather than the first would link a renal-cell
    trial to plain "Carcinoma".
    """
    t = " ".join((term or "").split())
    if not t:
        return
    seen = {t.lower()}
    yield t

    # Strip qualifiers repeatedly: "metastatic advanced solid tumor".
    base = t
    while True:
        nxt = _COND_QUALIFIER.sub("", base)
        if nxt == base:
            break
        base = nxt
    if base and base.lower() not in seen:
        if base.lower() not in _COND_TOO_GENERIC:
            seen.add(base.lower())
            yield base

    def _push(c):
        c = " ".join((c or "").split())
        low = c.lower()
        if len(c) >= 4 and low not in seen and low not in _COND_TOO_GENERIC:
            seen.add(low)
            return c
        return None

    # Plural and singular. MeSH heads neoplasms plural, most diseases singular.
    for cand in ((base[:-1],) if base.endswith("s") else (base + "s",)):
        c = _push(cand)
        if c:
            yield c

    if _COND_CANCER.search(base):
        for repl in ("Neoplasms", "Neoplasm"):
            c = _push(_COND_CANCER.sub(repl, base))
            if c:
                yield c

    # "Overweight and Obesity" is two headings in one cell. Last, because a
    # part is always a weaker claim than the whole.
    for part in _COND_SPLIT.split(base):
        c = _push(part)
        if c:
            yield c
