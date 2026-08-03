# How the research agent should use the graph and the vector store

Written from measurement, not from design preference. Two harnesses produced
the numbers: `bench.py` (116 questions, one configuration) and `strategy.py`
(22 questions × 5 configurations, scored by contradiction between arms rather
than by a judge — the model grades its own work far too kindly).

Everything below that reads like a recommendation has a number attached. Where
it does not, it says so.

---

## 1. There is no store-priority rule, and looking for one is the mistake

"Graph first, documents as fallback" and its opposite are both wrong. What
predicts the right route is the **shape of the question**, and it varies per
question, not per category and not per store.

**40% of questions chain** — a lookup searched on a value an earlier lookup
returned (46 of 116). They are all the same shape: *resolve an entity, then
expand from it*.

> *which drugs developed by Novartis are approved in both the US and Europe*

The second lookup cannot be written until the first returns. A fixed parallel
plan cannot answer any of these.

**And a large minority never chain at all.** Aggregate-over-the-whole-graph
questions are one Cypher statement, and anything fired in parallel with them is
waste:

```
Graph Multi-hop                8/8   ####################
Regulatory Intelligence        3/4   ###############
Drug Discovery                5/10   ##########
...
Clinical Development           0/4
Mechanism and Target Analysis  0/4
Trial Analytics                0/3
Research                       0/5
```

So: **the agent decides per question, after seeing each result.** Not a router,
not an ordering.

---

## 2. Free choice beats every fixed plan, measured

Five configurations, same 22 questions, same total lookup budget so a win is
not just a bigger allowance.

**Scored on ANSWERED, not on evidence returned.** The first version of this
table used evidence count and it was wrong in a way worth recording: the
documents-only arm returned six chunks for *"are there recruiting clinical
trials for ALS"* and then wrote *"I don't have access to a tool that can query
the clinical trial registry database"*. It retrieved something and answered
nothing, and counting rows scored that as a store that could answer. A run
counts only if evidence came back, the answer is about the data, and it does
not claim absence.

| arm | **answered** | refused | denied | no evidence |
|---|---|---|---|---|
| graph only | 18 | 0 | 2 | 2 |
| documents only | 13 | **6** | 1 | 5 |
| fixed parallel (1+1) | 20 | 0 | 1 | 2 |
| split budget (4+4) | 19 | 0 | 2 | 1 |
| **agentic** | **21** | **0** | **0** | 1 |

The agentic loop answers 21 of 22 and is the only arm with no refusal and no
false denial. Every fixed shape loses something: the graph alone denies twice,
the documents alone refuse six times, fixed-parallel answers 20 but on a single
lookup each so it cannot chain.

Note what this corrects. On evidence count the agentic arm returned LESS than
the best single store more often than more (10 against 6) - so evidence count
was never the win condition, and any conclusion drawn from it was measuring
volume rather than usefulness.

---

## 3. The graph is the broader store; the documents are a complement, not an alternative

Which store can answer a question ALONE, re-scored on answered:

```
both stores          12 / 22
ONLY the graph        6 / 22
ONLY the documents    1 / 22
neither               3 / 22
```

So the graph answers 18 of 22 alone and the corpus 13, and the corpus refuses
on 6 - mostly questions that want structure it does not hold ("how many
recruiting trials", "which companies sponsor most"). **Routing that treats the
two as interchangeable is wrong**: one question in 22 is documents-only, and
the corpus is there for what a document SAYS, not for counting.

That corrects an earlier claim in this file. Per CALL the document search does
return something more often than a Cypher query (92% against 80% over 116
questions), and I read that as the corpus being the more reliable store. It is
not the same thing: returning chunks is not answering, and the arm that only
had chunks refused six times.

What stands is that the documents are under-reached - 20 of 116 questions
touched them - and that 68 graph queries returned nothing. The cost of that is
not coverage, since the graph usually can answer. It is quality, and §5 has the
one case measured so far.

---

## 4. The switch rule did not earn its place — the prompt did

All 7 false denials on the first run had one shape: four graph queries in a
row, no document search, then "no data found" for something sitting in the
3.24M chunks.

```
[gggg] What biomarkers are associated with Parkinson's disease?
[gggg] Which ACE inhibitors are FDA approved?
[gggg] What oligonucleotide therapies are available?
```

I fixed it mechanically first: after three consecutive calls to one store,
refuse that store for one call. **That was patching a symptom I had written
myself.** The prompt's only advice on an empty result was:

> try a different starting node — the full-text index instead of an exact
> name, a name prefix instead of equality — before concluding the data is absent

Every option it offered was another graph query. Nothing said an empty graph
result might mean the fact is written in prose rather than tabulated. `gggg`
was the prompt working exactly as specified.

The prompt now states what each store physically holds — the graph "holds no
prose: there is no sentence anywhere in it", the documents "cannot count, and
have no notion of all" — and what a second empty result means: stop rewriting
Cypher, two misses usually mean the fact is not tabulated.

Measured on the four questions that produced `gggg` and a denial, **with the
mechanical rule turned off**:

```
biomarkers          ggggd    46 rows   no denial
ACE inhibitors      gggg     30 rows   no denial
oligonucleotides    gggg    212 rows   no denial
atorvastatin AEs    gdd      26 rows   no denial
```

Two still use only the graph, which is correct — it holds those facts. The
failure was never staying on the graph; it was staying on the graph **and then
claiming the data did not exist**.

The rule is now off by default and kept only as a backstop. Its benefit was
never reproducible: measured twice at n=22, it helped once and hurt once. A
rule that cannot be shown to work is worse than no rule, because it reads as
protection nobody has verified.

**The general lesson, which is the transferable part:** when the model behaves
badly, check what you told it before adding machinery to stop it.

---

## 5. Coverage must mean "did we get all of it", not "did we get any"

The single most dangerous failure in either store is a partial result that
looks complete.

Asked which trials share a mechanism with FDA-approved epilepsy drugs, the
agent got `LIMIT 50` rows that were 50/50 furosemide and reported that seven of
eight mechanisms had no trials at all. They had 3,671 between them.

**Warning the model does not work** — measured, it changed nothing. A warning
is something to weigh against the rows sitting in front of it. What works is
computing the missing data and handing it over:

```
TRUNCATED - you saw 50 of 3,671 matching rows.
Here is 'mech' over ALL 3,671 rows, which is what you must answer from:
    Sodium channel alpha subunit blocker: 2382
    Voltage-gated calcium channel modulator: 805
    Synaptic vesicle glycoprotein 2A modulator: 271
    ...
```

**For the research agent this means any tool returning a `coverage` field must
not set it from `bool(rows)`.** A truncated read and a complete one are
indistinguishable otherwise, and "we got rows" is what tells the caller to stop
looking.

---

## 5b. You cannot route by keyword, and the quality question is open

The obvious design - classify the question, send it to the right store - does
not survive the data. Looking for words that predict which store wins found
nothing usable: the only term favouring documents across 22 questions was
"approved", and it favoured the graph as often. A classifier trained on this
would be guessing.

Which is the argument for deciding AFTER a result rather than before one.

The open question is quality, and there is one measured case pointing at the
corpus. Asked *"is rimegepant FDA approved"*, the graph had ten rows and
answered *"listed as None (Tentative Approval) rather than full approval"* -
misleading. The documents answered *"yes, approved for acute migraine"*, which
is correct. Both "answered". Only one was right.

Nothing in this file scores correctness, so that case is an anecdote, not a
finding. Scoring it needs relevance judgements over a set of questions with
known answers, and that has not been built.

## 6. The agent must never answer from its own knowledge

On the benchmark, one question came back fluent and well-sourced in tone with
**zero lookups recorded**. `tool_choice="required"` on the first step fixes it.
Every later step stays `auto` so the loop can stop.

---

---

## 6. Entity resolution belongs in a tool, not in the caller

Three times in one session the model queried the graph correctly and got a
fraction of the answer, because nothing told it how MeSH is shaped:

```
"how many trials on eczema"          301 of 2,214
   MeSH files it under "Dermatitis, Atopic" - the full-text index HAD
   returned that node, matched on its synonym "Atopic Eczema", and the
   model narrowed back to d.name = 'Eczema'

"how many trials on Heart Diseases"  1,980 of 31,141
   a category whose 204 subtypes hold the trials; one on heart failure is
   tagged Heart Failure, never its parent
```

Both were fixed with prompt rules, and the rules work. But a rule asks the
caller to get it right every time, and `resolve_condition` makes it hard to get
wrong - it returns every candidate node with its trial count, its child count
and its synonyms, so a category is visible as `children=204` rather than
something the caller has to know.

Measured against the prompt rules alone on five disease questions: **same
answers, 11 lookups against 15**. It does not improve correctness, because the
prompt already did that. It removes the exploratory queries the model
otherwise spends discovering the shape - eczema went from five lookups to two.

**For the research agent this is the shape to copy.** A raw Cypher tool assumes
every caller knows MeSH inverts its headings and that a category node is nearly
empty. A resolve tool assumes nothing and reports what is there. The same
argument as §5: build it so the caller cannot quietly get a fraction of the
answer.


## 7. What to carry into the research agent

1. **Two tools, free order, decision after each result.** Not a router.
2. **First call is forced.** No answering from model knowledge.
3. **Per-store caps that no single store can exhaust alone** — three
   consecutive calls to one store, then it is refused for one.
4. **Bounded four ways**: total steps, per-store calls, wall clock, tokens.
5. **Coverage is completeness, never "did we get rows".** Report the true
   total, and when few values fill a limit, return the distribution.
6. **Reversed relationship directions are caught before execution.** A wrong
   arrow returns zero rows and no error, which reads as absence.

## What is NOT established

- These numbers come from MiniMax-M2. A different model may need different
  budgets — the *rules* should hold, the constants may not.
- 22 questions × 5 arms is enough to separate agentic from fixed-parallel, and
  not enough to tune the constant in §4. Three consecutive calls is a
  measured-plausible choice, not an optimum.
- Retrieval quality was never scored — only whether evidence was returned and
  whether an answer falsely claimed absence. Whether the *right* chunks come
  back is a separate question that needs relevance judgements.
