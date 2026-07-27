"""Structure-aware chunking with a documented fallback cascade.

Regulatory documents mostly carry their own structure, and where they do it is
better than anything a splitter could infer:

    1. EU SPC template   -> numbered sections (4.1 Therapeutic indications,
                            4.8 Undesirable effects, ...) fixed by Directive
                            2001/83/EC Annex I. Confirmed present in 4/4 sampled
                            MHRA SPCs.
    2. Patient leaflet   -> named headings ("Possible side effects", ...)
    3. Generic headings  -> anything that looks like a heading
    4. Semantic          -> paragraph groups (PAR, PMDA: no mandated template)
    5. Fixed             -> last resort

Every chunk records which branch produced it in `chunk_path`, so the
distribution can be checked after a run. If template detection silently breaks,
the collection still fills, retrieval still "works", and only the section
filters go quiet - the kind of failure that hides for months unless measured.
"""
from __future__ import annotations

import re
from functools import lru_cache

from config import CHUNK_TOKENS, CHUNK_OVERLAP, EMBED_MODEL, SEMANTIC_SPLIT
from schema import Chunk

# --- EU SPC template ---------------------------------------------------------
# Whitelisted, not pattern-matched. A generic "number + capitalised words" rule
# reads table contents as headings: on a real SPC it produced "597 Placebo",
# "0.0033 Hazard ratio**" and a Berlin postcode as section starts.
SPC_SECTIONS = {
    "1": "name", "2": "composition", "3": "pharmaceutical_form",
    "4": "clinical_particulars",
    "4.1": "indications", "4.2": "posology", "4.3": "contraindications",
    "4.4": "warnings", "4.5": "interactions", "4.6": "pregnancy",
    "4.7": "driving", "4.8": "adverse_effects", "4.9": "overdose",
    "5": "pharmacological_properties",
    "5.1": "pharmacodynamics", "5.2": "pharmacokinetics", "5.3": "preclinical_safety",
    "6": "pharmaceutical_particulars",
    "6.1": "excipients", "6.2": "incompatibilities", "6.3": "shelf_life",
    "6.4": "storage", "6.5": "container", "6.6": "disposal",
    "7": "marketing_authorisation_holder", "8": "marketing_authorisation_number",
    "9": "date_of_authorisation", "10": "date_of_revision",
}
_SPC_HEAD = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2})?)[.)]?\s+(\S.{2,80})$")
# These SPCs are laid out in two columns, so PyMuPDF emits the section number
# and its title as separate lines: "4.1" then "Therapeutic indications". Every
# heading was missed until this was handled - all six sampled SPCs reported
# zero section numbers while plainly containing 4.1 through 4.9.
_SPC_NUM_ONLY = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2})?)[.)]?\s*$")


def join_split_headings(lines: list[str]) -> list[str]:
    """Merge a bare section-number line with the title line that follows it."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        m = _SPC_NUM_ONLY.match(lines[i])
        if m and m.group(1) in SPC_SECTIONS:
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and len(lines[j].strip()) <= 80:
                out.append(f"{m.group(1)} {lines[j].strip()}")
                i = j + 1
                continue
        out.append(lines[i])
        i += 1
    return out

# --- Patient information leaflet --------------------------------------------
PIL_HEADINGS = [
    (re.compile(r"what .{1,40} is and what it is used for", re.I), "indications"),
    (re.compile(r"(before you take|before you use|what you need to know before)", re.I), "warnings"),
    (re.compile(r"how to (take|use)\b", re.I), "posology"),
    (re.compile(r"possible side effects", re.I), "adverse_effects"),
    (re.compile(r"how to store", re.I), "storage"),
    (re.compile(r"contents of the pack|further information", re.I), "composition"),
]

# --- Generic heading fallback ------------------------------------------------
SECTION_KEYWORDS = {
    "indication": "indications", "contraindication": "contraindications",
    "posology": "posology", "dosage": "posology", "administration": "posology",
    "adverse": "adverse_effects", "undesirable effect": "adverse_effects",
    "side effect": "adverse_effects", "warning": "warnings",
    "precaution": "warnings", "interaction": "interactions",
    "pharmacodynamic": "pharmacodynamics", "mechanism of action": "pharmacodynamics",
    "pharmacokinetic": "pharmacokinetics", "overdose": "overdose",
    "pregnancy": "pregnancy", "composition": "composition",
    "clinical trial": "clinical_trials", "efficacy": "efficacy",
}
_KEYWORDS_ORDERED = sorted(SECTION_KEYWORDS.items(), key=lambda kv: -len(kv[0]))
_HEADING = re.compile(r"^\s*([A-Z0-9][\w .()/-]{2,60})\s*$")


@lru_cache(maxsize=1)
def _tokenizer():
    """The embedding model's own tokenizer.

    Counting whitespace-separated words instead returns ~1 for a page of
    Japanese, which has no spaces - so every PMDA page (they average 62 pages
    and 178k characters) became a single chunk. Arabic is affected too. Falls
    back to a character heuristic only if transformers is unavailable.
    """
    try:
        from transformers import AutoTokenizer
        return AutoTokenizer.from_pretrained(EMBED_MODEL)
    except Exception:  # noqa: BLE001 - counting must never break ingestion
        return None


def _ntok(s: str) -> int:
    tk = _tokenizer()
    if tk is not None:
        return len(tk.encode(s, add_special_tokens=False))
    # ~3 chars/token is deliberately conservative: it over-counts Latin script
    # slightly, which keeps chunks under budget rather than over.
    return max(1, len(s) // 3)


def _language(text: str) -> str:
    for ch in text[:400]:
        o = ord(ch)
        if 0x0600 <= o <= 0x06FF:
            return "ar"
        if 0x4E00 <= o <= 0x9FFF:
            return "zh"
        if 0x3040 <= o <= 0x30FF:
            return "ja"
    return "en"


def _spc_section(line: str):
    """Return (code, tag) when the line is a whitelisted SPC heading."""
    m = _SPC_HEAD.match(line)
    if not m:
        return None
    code = m.group(1)
    tag = SPC_SECTIONS.get(code)
    if not tag:
        return None
    # A heading is short and mostly words; a table row that happens to begin
    # with "4.1" is not.
    title = m.group(2).strip()
    if len(title) > 80 or sum(c.isdigit() for c in title) > len(title) / 3:
        return None
    return code, tag


def _pil_section(line: str):
    s = line.strip()
    if not s or len(s) > 120:
        return None
    for rx, tag in PIL_HEADINGS:
        if rx.search(s):
            return tag
    return None


def _keyword_section(line: str):
    if not _HEADING.match(line):
        return None
    low = line.lower()
    for kw, tag in _KEYWORDS_ORDERED:
        if kw in low:
            return tag
    return None


def detect_layout(pages: list[str]) -> str:
    """Which branch of the cascade this document should take.

    Order and evidence both matter, and getting either wrong mislabels whole
    corpora:

    * PIL is tested first. Patient leaflets are *also* numbered 1-6, so bare
      integers are not evidence of an SPC - testing SPC first labelled 109 of
      145 leaflet chunks as "composition" and "pharmaceutical_form".
    * SPC evidence is restricted to *dotted* codes (4.1, 4.8, 5.1). Those exist
      only in the EU template; a bare "2." heads a section in almost any
      document.
    * Headings are matched per line, not against the whole text. Searching the
      blob made any PAR that merely discussed side effects look like a leaflet
      - all 132 sampled PAR chunks took the PIL branch.
    """
    lines = join_split_headings(
        [l.strip() for p in pages[:6] for l in p.split("\n")])

    pil_hits = {tag for l in lines if len(l) <= 120 for rx, tag in PIL_HEADINGS
                if rx.search(l)}
    if len(pil_hits) >= 2:
        return "pil"

    dotted = {r[0] for l in lines if (r := _spc_section(l)) and "." in r[0]}
    if len(dotted) >= 2:
        return "spc"

    if any(_keyword_section(l) for l in lines):
        return "heading"
    return "semantic" if SEMANTIC_SPLIT else "fixed"


# MHRA stores documents under pdfs/<type>/, so the type is known rather than
# inferred. Detection put every sampled PAR on the leaflet branch because an
# assessment report discusses the same topics a leaflet does - inferring a fact
# the path already states is how that happens.
DOC_TYPE_LAYOUT = {
    "spc": "spc",
    "pil": "pil",
    "par": None,      # assessment narrative: no template, use the fallbacks
    "epar": None,
}


def layout_for(pages: list[str], doc_type: str | None) -> str:
    hinted = DOC_TYPE_LAYOUT.get((doc_type or "").lower(), "__none__")
    if hinted == "__none__":            # unknown type -> detect
        return detect_layout(pages)
    if hinted is None:                  # known to be untemplated -> skip PIL/SPC
        lines = join_split_headings(
            [l.strip() for p in pages[:6] for l in p.split("\n")])
        if any(_keyword_section(l) for l in lines):
            return "heading"
        return "semantic" if SEMANTIC_SPLIT else "fixed"
    return hinted


def chunk_document(blocks, source, doc_id, s3_key, doc_type=None) -> list[Chunk]:
    """blocks: list[(page, text)] from extract.extract_blocks -> list[Chunk].

    The buffer runs across the whole document rather than resetting per page.
    Resetting meant a section spanning pages was cut at the page break whatever
    the section logic said - and SPCs average 16.5 pages, with sections that
    routinely span them.
    """
    blocks = [(p, t) for p, t in blocks if t and t.strip()]
    if not blocks:
        return []
    pages = [t for _, t in blocks]
    layout = layout_for(pages, doc_type)
    lang = _language("\n".join(pages[:3]))

    chunks: list[Chunk] = []
    buf: list[str] = []
    buf_tok = 0
    offset = 0
    state = {"section": None, "code": None,
             "page_from": blocks[0][0], "page_to": blocks[0][0]}

    def flush():
        nonlocal buf, buf_tok, offset
        text = "\n".join(buf).strip()
        if text:
            chunks.append(Chunk.new(
                text, source, doc_id, s3_key, offset,
                page=state["page_from"], page_to=state["page_to"],
                section=state["section"], section_code=state["code"],
                language=lang, chunk_path=layout))
            offset += 1
        buf, buf_tok = [], 0
        state["page_from"] = state["page_to"]

    for page, text in blocks:
        state["page_to"] = page
        for line in join_split_headings(text.split("\n")):
            hit = None
            if layout == "spc":
                hit = _spc_section(line)
            elif layout == "pil":
                t = _pil_section(line)
                hit = (None, t) if t else None
            elif layout == "heading":
                t = _keyword_section(line)
                hit = (None, t) if t else None

            # A section boundary closes the current chunk, so one chunk never
            # straddles two sections.
            if hit and hit[1] != state["section"] and buf:
                flush()
            if hit:
                state["code"], state["section"] = hit[0], hit[1]

            buf.append(line)
            buf_tok += _ntok(line)

            if buf_tok >= CHUNK_TOKENS:
                carry = list(buf)
                flush()
                # Overlap tail, so a fact split across the boundary stays
                # retrievable from either side.
                tail, ttok = [], 0
                for l in reversed(carry):
                    ttok += _ntok(l)
                    tail.insert(0, l)
                    if ttok >= CHUNK_OVERLAP:
                        break
                buf, buf_tok = tail, ttok

    if buf:
        flush()
    return chunks
