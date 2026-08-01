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
not just a bigger allowance:

| arm | answered | false denials | evidence | lookups | sec |
|---|---|---|---|---|---|
| graph only | 20 | 1 | 2,025 | 93 | 26 |
| documents only | 17 | 1 | 336 | 97 | 62 |
| fixed parallel (1+1) | 20 | 1 | 387 | 33 | 20 |
| split budget (4+4) | 21 | 2 | 1,317 | 77 | 31 |
| **agentic** | **21** | **0** | **2,477** | 88 | 33 |

**Fixed parallel is the clear loser** — 387 evidence against 2,477, six times
less, for two thirds of the time. Cheap and wrong.

`split` and `agentic` are the same loop differing in one rule (§4). That rule
is worth 2 false denials and roughly double the evidence.

---

## 3. The document store is the more reliable of the two and is under-used

Over 116 questions:

| | calls | returned something |
|---|---|---|
| document searches | 52 | **92%** |
| graph queries | 340 | 80% |

Yet only 20 of 116 questions touched the documents at all, and **68 graph
queries returned nothing** — 68 round trips spent discovering the graph does
not hold something.

The under-use is not because documents are unhelpful. It is a bias: the model
reaches for Cypher by default and only falls to the corpus when the graph
disappoints it repeatedly.

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
