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

## 4. The one rule that earns its place: a store cannot monopolise the budget

All 7 false denials on the 116-question run had one shape — four graph queries
in a row, no document search, then "no data found":

```
[gggg  ] What biomarkers are associated with Parkinson's disease?
[gggg  ] Which ACE inhibitors are FDA approved?
[gggg  ] What oligonucleotide therapies are available?
[gggg  ] Which mechanisms are unique to approved vs investigational?
```

None of that is missing. It is in the 3.24M chunks the model never asked.

**After three consecutive calls to one store, that store is refused for one
call and the reply names the other one.** Not a ban, not an ordering — the
model still chooses, it just cannot spend the whole budget in one place before
the other has been tried once.

Three attempts were needed and the two failures are the useful part:

- keyed on consecutive **empty** results — never fired, because the model
  interleaves hits and misses and two zeros rarely land in a row
- **raised both caps** to 6 so a shared ceiling could let the question decide —
  measurably worse: it spent all six on the graph and never reached the
  documents, while the 4+4 split reached them at step five
- **withheld the tool between steps** — never fired either, and invisibly:
  withholding cleared the run counter, but the model calls a withheld tool
  anyway often enough, so by dispatch the counter read zero

The rule has to live where the call is dispatched, and a hint must not clear
the counter it is a hint about.

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
