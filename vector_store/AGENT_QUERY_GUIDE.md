# How the Research Agent Should Query the Vector Store

Everything below is measured against the live collection (3,240,756 chunks),
not inferred. Numbers in tables are reproducible with the probes described at
the end.

---

## 1. The request contract

```
POST http://35.153.204.103/search
Content-Type: application/json

{ "query": "<natural-language question>", "final_k": 15 }
```

| Field | Default | Agent should set it? |
|---|---|---|
| `query` | — | **Always** |
| `final_k` | 15 | Yes — 15 normally, 30 for survey questions |
| `section` | null | Rarely (see §5) |
| `section_code` | null | Only for an explicit SPC reference |
| `doc_type` | null | **Never** (see §5) |
| `top_k` | 250 | Never — tuned |
| `min_score` | — | Not settable; a 0.6 relevance floor is enforced server-side |

Response: `count` plus results carrying `text`, `cosine`, `section`,
`section_code`, `page`, `s3_key`, `doc_type`, `duplicates`.

---

## 2. What the corpus contains

| Source | Chunks | Content |
|---|---|---|
| `products.mhra.gov.uk` | 2,168,590 | UK SPCs, PILs, Public Assessment Reports |
| `ema.europa.eu` | 988,041 | EPARs, assessment reports, risk management plans |
| `pmda.go.jp` | 79,471 | Japanese review reports (in English) |
| MENA/GCC authorities | ~4,600 | UAE, Oman, Qatar, Bahrain filings |
| `loinc.org` | 51 | Terminology |

Language: 3,236,136 English · 3,869 Arabic · 751 Chinese.

**Document types.** `spc` and `pil` and `par` dominate; EMA documents carry a
product name in `doc_type` instead (`keytruda`, `comirnaty`, `opdivo`…), which
is why `doc_type` must not be used as a filter.

**Sections available** (top): `adverse_effects` 351k · `warnings` 282k ·
`composition` 229k · `posology` 225k · `indications` 120k ·
`pharmacodynamics` 116k · `pharmacokinetics` 108k · `storage` 106k ·
`efficacy` 104k · `interactions` 74k · `pregnancy` 46k ·
`contraindications` 37k · `overdose` 32k.

---

## 3. What the corpus answers well — and what it does not

Measured as top cosine over a standard probe set:

| Question type | Top cosine | Verdict |
|---|---|---|
| Dosing / posology | 0.770 | Excellent |
| Renal & hepatic dose adjustment | 0.758 | Excellent |
| Drug interactions | 0.747 | Excellent |
| Class-level ("which drugs cause X") | 0.740 | Excellent |
| Adverse effects | 0.735 | Excellent |
| Pregnancy & lactation | 0.720 | Excellent |
| Paediatric use | 0.713 | Excellent |
| EU marketing authorisation dates | 0.707 | Good |
| Manufacturing & quality | 0.704 | Good |
| Mechanism / molecular target | 0.702 | Good |
| Indications | 0.695 | Good |
| Post-marketing commitments | 0.676 | Good |
| Clinical efficacy / endpoints | 0.666 | Adequate |
| Contraindications | 0.652 | Adequate |
| Comparative (drug A vs B) | 0.643 | Weak — decompose instead |
| Pharmacokinetics | 0.621 | Weak — name the parameter |
| **US / FDA approval status** | 0.609 | **Borderline — see §7** |

**Returns nothing at all** (below the 0.6 floor — the corpus genuinely lacks
these):

- biomarker selection, companion diagnostics
- health technology assessment, reimbursement
- patent expiry, exclusivity
- pricing, cost-effectiveness
- real-world evidence registries

For these the agent should state the corpus does not cover the topic, rather
than reaching for adjacent material.

---

## 4. How to phrase the query

### Use the generic (INN) name, not the brand

Measured on `"<name> mechanism of action"`:

| Brand | | INN | |
|---|---|---|---|
| Wegovy | 0.629 | **semaglutide** | **0.718** |
| Keytruda | 0.681 | **pembrolizumab** | **0.719** |
| Humira | 0.644 | **adalimumab** | **0.692** |
| Xeljanz | 0.625 | **tofacitinib** | **0.660** |
| Cosentyx | 0.645 | **secukinumab** | **0.663** |
| Aimovig | 0.653 | erenumab | 0.649 |

INN wins in five of six. Best of all is **both together** —
`"Keytruda pembrolizumab mechanism of action"` scored 0.725, above either alone.
Regulatory documents use both names, so supplying both matches more of them.

### Longer and more specific is better

| Query | Words | Top cosine |
|---|---|---|
| `pembrolizumab` | 1 | 0.698 |
| `pembrolizumab adverse effects` | 3 | 0.729 |
| `what are the most common adverse effects of pembrolizumab` | 9 | 0.716 |
| `which adverse reactions were most frequently reported in patients treated with pembrolizumab in the pivotal clinical trials and how often they occurred` | 26 | **0.755** |

Verbose clinical phrasing outperforms keywords. The agent should pass the
user's question in full rather than reducing it to terms.

### Decompose multi-part questions — this is the highest-value rule

A three-part question retrieves for whichever part dominates the embedding:

| Query | Top cosine | Results mentioning US/FDA |
|---|---|---|
| `erenumab mechanism of action, molecular target, and is it FDA approved` | 0.624 | 0 of 15 |
| `erenumab mechanism of action` | 0.649 | — |
| `erenumab molecular target CGRP receptor` | **0.711** | — |
| `erenumab approved by the FDA in the United States` | 0.615 | **2 of 15** |

The combined query returned **zero** passages mentioning the FDA. Split out,
the same question surfaced two. **Always issue one search per information
need, then synthesise.**

---

## 5. Filters

### `section` — scoping only, not quality

| Query | No filter | With `section` |
|---|---|---|
| contraindications of atorvastatin | 0.733 | 0.733 |
| what dose in renal impairment | 0.782 | 0.776 |
| is it safe in pregnancy | 0.696 | 0.696 |
| serious adverse reactions | 0.700 | 0.700 |
| mechanism of action of pembrolizumab | 0.713 | 0.713 |
| how should it be stored | 0.681 | 0.681 |

Identical in five of six, marginally *worse* in one. A well-phrased question
already lands in the right section, so **do not filter by default.** Use
`section` only when the user explicitly wants one section and nothing else —
and if the filtered call returns few results, retry without it, because section
labels are imperfect.

### `section_code` — for explicit references

Use when the user cites an EU SPC number: `"4.8"`, `"4.3"`, `"5.1"`.

### `doc_type` — do not use

EMA documents carry a product name here rather than a type, so filtering on
`spc`/`pil`/`par` silently drops the entire EMA corpus.

---

## 6. The agent's own verification duty

**Retrieval does not guarantee the results are about the drug you named.**
Measured — how many of 15 results actually mention the named drug:

| Query | On target |
|---|---|
| contraindications of **adalimumab** | 15/15 (100%) |
| adverse effects of **pembrolizumab** | 15/15 (100%) |
| dose of **semaglutide** | 15/15 (100%) |
| warnings for **tofacitinib** | 15/15 (100%) |
| mechanism of action of **erenumab** | **6/15 (40%)** |
| contraindications of **atorvastatin** | **8/15 (53%)** |

Two failure patterns:

- **Sparsely documented drugs** (erenumab) — results drift to other molecules
  in the same class. A search for erenumab returned passages about
  eptinezumab, cetuximab and ustekinumab, all monoclonal antibodies with
  similarly structured mechanism-of-action text.
- **Generics in a crowded class** (atorvastatin) — rosuvastatin and
  simvastatin passages score within ~0.08, close enough to interleave.

Repeating the name (`"erenumab Aimovig mechanism of action"`) improved it from
40% to 60% — helpful, not sufficient.

> **Rule: before using a passage, confirm the drug name appears in its `text`
> or `s3_key`. Discard passages about a different molecule.** The embedding
> cannot make this distinction reliably; the agent can, trivially.

---

## 7. Jurisdiction

There is no FDA source in the corpus. **But FDA content exists inside non-FDA
documents** — EMA and PMDA reviews routinely cite US decisions:

```
[ema]  "Respreeza is approved by the FDA since 2003. The pivotal studies for the
        current EU application was also submitted to the FDA..."
[pmda] "...the drug was approved in the U.S. on October 15, 2013."
[pmda] "Wegovy was approved ... in the US in June 2021 and in the EU in January 2022."
```

Between 13% and 30% of results for explicitly US-phrased queries contain US or
FDA text, carried by EMA (20 occurrences), PMDA (4) and MHRA (1) in a standard
probe.

> **Rule: always search, whatever the jurisdiction. If a retrieved passage
> explicitly states US/FDA status, cite it. If none does, say the corpus does
> not confirm it. Never infer US approval from EU or UK approval** — they are
> independent decisions.

---

## 8. Reading the response

| Signal | Meaning | Agent action |
|---|---|---|
| `count: 0` | Nothing cleared the 0.6 relevance floor | State the corpus has no relevant information. Do not soften, do not substitute adjacent material. |
| `cosine` ≥ 0.70 | Strong topical match | Use with confidence |
| `cosine` 0.60–0.70 | Usable but verify on-topic | Check the drug name appears (§6) |
| `duplicates: [...]` | The same text in other products | **Evidence strength.** `+37` means every UK licence says this — report it as consistent across authorisations |
| `section` / `section_code` | Regulatory location | Cite as "SPC section 4.3" |
| `s3_key` + `page` | Exact provenance | Every claim should carry one |

`section` is occasionally wrong (observed: angioedema warning text labelled
`storage`, contraindication text labelled `composition`). Trust the passage
text over its label when they disagree.

---

## 9. Worked pattern

User asks: *"What is erenumab's mechanism of action, molecular target, and is
it FDA approved?"*

```python
a = search("erenumab Aimovig mechanism of action", final_k=15)
b = search("erenumab molecular target CGRP receptor binding", final_k=15)
c = search("erenumab approved by the FDA in the United States", final_k=15)

for r in a + b + c:                       # §6 verification
    if "erenumab" not in (r.text + r.s3_key).lower(): discard(r)

# synthesise: MoA and target from a + b, with citations
# for FDA: only if a passage in c explicitly says so; otherwise state that the
# corpus does not confirm US approval status
```

---

## 10. Ready-to-use system-prompt block

> You query a vector store of regulatory documents from the MHRA (UK), EMA
> (EU), PMDA (Japan) and several MENA/GCC authorities — SPCs, patient leaflets
> and assessment reports.
>
> **Querying**
> - Issue one search per information need. Never combine several questions in
>   one query.
> - Pass the user's question in full, in clinical language. Longer and more
>   specific retrieves better than keywords.
> - Prefer the generic (INN) name; include the brand name as well when known.
> - Request `final_k: 15`, or `30` for survey questions spanning many products.
> - Do not set `doc_type`. Set `section` only when the user wants one section
>   exclusively, and retry without it if few results return.
>
> **Using results**
> - Before using a passage, confirm the drug you asked about appears in its
>   text or source path. Discard passages about other molecules — retrieval
>   sometimes returns same-class drugs.
> - Cite every claim with the document and page. Prefer passages whose
>   `section` matches the claim.
> - When `duplicates` is non-empty, the same wording appears across that many
>   other authorised products; report it as consistent across authorisations.
> - An empty result means the corpus has nothing relevant. Say so plainly.
>   Never substitute related material for the answer.
> - Never infer US/FDA approval from EU or UK approval. Report US status only
>   when a retrieved passage states it explicitly; assessment reports from EMA
>   and PMDA often do.
> - The corpus does not cover pricing, reimbursement, health technology
>   assessment, patents, companion diagnostics or real-world evidence
>   registries.

---

## 11. Reproducing these measurements

Every table above comes from calling `retrieve.retrieve()` directly on the
host, with the service environment loaded:

```bash
cd ~/fullPipeline/vector_store
QDRANT_URL=http://localhost:6333 QDRANT_API_KEY=… USE_FP16=0 \
  ~/vsenv/bin/python -c "import retrieve; print(retrieve.retrieve('<query>', final_k=15))"
```

Re-run them when the corpus changes materially — the cosine bands in §3 and the
entity-scoping rates in §6 are properties of the current content, not of the
software.
