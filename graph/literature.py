"""Publication, and the edges from a paper to what it is about.

Four sources, one shape. Europe PMC and PubMed share a column set exactly
(europe_pmc_metadata.csv and pubmed_metadata.csv are the same 23 columns);
bioRxiv and medRxiv share a different one, because a preprint has no PMID and
no journal.

openalex is not here. It is 7.9M works against 5,349 rows across these four,
so it is 99.6% of the literature in the lake - but all fifteen of its API keys
report insufficient budget, so it cannot be refreshed. Loading a snapshot that
can never update, and which would dominate every literature query, is worse
than a small current corpus. See EXCLUDED in sources.py.

The two edges are deliberately conservative. A paper "mentioning" a drug is
matched by exact dictionary lookup against the resolver, and a paper "about" a
disease by exact lookup against MeSH names - both on title only, not abstract.
Abstract matching sounds better and is not: an abstract naming twelve drugs in
its background section produces twelve MENTIONS edges, of which one is what the
paper is actually about. Precision matters more than recall here, because the
whole point of the edge is to answer "what has been published on X".
"""
from __future__ import annotations

import re

import lake
from normalise import fold

L = {
    "europepmc": "Literature_Evidence/europepmc.org/europe_pmc/europe_pmc_metadata.csv",
    "pubmed":    "Literature_Evidence/pubmed.ncbi.nlm.nih.gov/pubmed/pubmed_metadata.csv",
    "biorxiv":   "Literature_Evidence/biorxiv.org/biorxiv/biorxiv_metadata.csv",
    "medrxiv":   "Literature_Evidence/medrxiv.org/medrxiv/medrxiv_metadata.csv",
}

# Title text worth matching against. Two characters would match everything;
# these are the shortest real drug names in the lake ("HES", "PEG").
MIN_TERM = 3
_YEAR = re.compile(r"(19|20)\d{2}")


def _year(*candidates: str) -> str:
    for c in candidates:
        m = _YEAR.search(c or "")
        if m:
            return m.group(0)
    return ""


def _link(b, pkey: str, title: str, source: str) -> None:
    """MENTIONS a substance, ABOUT a disease - title only, exact matches only.

    Both dictionaries are already in memory: the resolver from gsrs/chembl, and
    mesh_by_name from load_mesh. Nothing new is scanned.
    """
    folded = fold(title)
    if len(folded) < MIN_TERM:
        return
    words = folded.split()

    # n-grams up to 4 words, longest first, so "non small cell lung carcinoma"
    # wins over "carcinoma". Capped at 4 because MeSH headings are rarely
    # longer and the cost is quadratic in title length.
    seen_sub, seen_dis = set(), set()
    for n in (4, 3, 2, 1):
        for i in range(len(words) - n + 1):
            term = " ".join(words[i:i + n])
            if len(term) < MIN_TERM:
                continue
            dkey = b.mesh_by_name.get(term)
            if dkey and dkey not in seen_dis:
                seen_dis.add(dkey)
                b.w.edge("ABOUT", pkey, dkey, match_method="name", source=source)
            m = b.r.resolve(term)
            if m.key and m.resolved and m.key not in seen_sub:
                seen_sub.add(m.key)
                b.w.edge("MENTIONS", pkey, m.key, match_method=m.method,
                         source=source)


def _load_indexed(b, key: str, source_name: str) -> int:
    """Europe PMC / PubMed: same 23 columns, keyed by PMID."""
    n = 0
    for row in lake.stream_csv(key, limit=b.limit):
        pmid = (row.get("pmid") or "").strip()
        doi = (row.get("doi") or "").strip()
        title = (row.get("title") or "").strip()
        if not title or not (pmid or doi):
            continue
        if not b.wanted(title):
            continue
        n += 1
        pkey = f"PMID:{pmid}" if pmid else f"DOI:{doi.lower()}"
        b.w.node("Publication", pkey, source=key, title=title[:400],
                 year=_year(row.get("publication_date", ""), row.get("first_publication_date", "")),
                 journal=row.get("journal_title", ""), doi=doi, pmid=pmid,
                 source_db=source_name, preprint="false")
        if pmid:
            b.w.identifier(pkey, "PMID", pmid, source=key)
        if doi:
            b.w.identifier(pkey, "DOI", doi.lower(), source=key)
        _link(b, pkey, title, key)
    return n


def _load_preprint(b, key: str, source_name: str) -> int:
    """bioRxiv / medRxiv: keyed by DOI, because a preprint has no PMID."""
    n = 0
    for row in lake.stream_csv(key, limit=b.limit):
        doi = (row.get("doi") or "").strip()
        title = (row.get("title") or "").strip()
        if not doi or not title:
            continue
        if not b.wanted(title):
            continue
        n += 1
        pkey = f"DOI:{doi.lower()}"
        b.w.node("Publication", pkey, source=key, title=title[:400],
                 year=_year(row.get("date", "")), journal=source_name, doi=doi,
                 pmid="", source_db=source_name, preprint="true")
        b.w.identifier(pkey, "DOI", doi.lower(), source=key)
        _link(b, pkey, title, key)
    return n


def load_publications(b):
    t0 = b._step("publications")
    n = 0
    n += _load_indexed(b, L["europepmc"], "europepmc")
    n += _load_indexed(b, L["pubmed"], "pubmed")
    n += _load_preprint(b, L["biorxiv"], "biorxiv")
    n += _load_preprint(b, L["medrxiv"], "medrxiv")
    b._done("publications", t0, n)


ALL = [load_publications]
