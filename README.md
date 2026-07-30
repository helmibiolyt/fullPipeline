# fullPipeline

A biomedical intelligence pipeline: 49 scrapers publish to S3, and two stores
are built from what lands there — a **Neo4j knowledge graph** (13.7M nodes) and
a **Qdrant vector store** (3.24M document chunks). Airflow keeps both current.

This README is the map. Each component documents itself in more depth:
[`graph/SCHEMA.md`](graph/SCHEMA.md), [`deploy/README.md`](deploy/README.md),
[`vector_store/AGENT_QUERY_GUIDE.md`](vector_store/AGENT_QUERY_GUIDE.md).

---

## What is where

```
scrape/         49 source scrapers, one folder each, each with a manifest.yaml
automation/     Airflow: the scraper DAG and the two sync DAGs
graph/          builds the Neo4j graph from the lake's CSVs
vector_store/   embeds the lake's documents into Qdrant
deploy/         provisioning and operations scripts for both hosts
```

## The shape of it

```
                   ┌──────────────────────────────────┐
   49 scrapers ───►│  S3  moine-data                  │
   (@weekly)       │  ~436 CSVs + 93,435 documents    │
                   └───────┬──────────────────┬───────┘
                           │                  │
         35 sources / 77 declared      8 document sources
              CSV files                       │
                           │                  │
                  ┌────────▼───────┐  ┌───────▼────────┐
                  │  graph_sync    │  │ vector_store_  │
                  │  (Azure host)  │  │ sync (AWS host)│
                  └────────┬───────┘  └───────┬────────┘
                           │                  │
                  ┌────────▼───────┐  ┌───────▼────────┐
                  │ Neo4j  biolyt  │  │ Qdrant         │
                  │ 13.7M nodes    │  │ 3.24M chunks   │
                  │ 16.8M edges    │  │ bge-m3         │
                  └────────────────┘  └────────────────┘
```

**Sources vs files.** 35 *scrapers* feed the graph, but they publish **77
declared CSV files** between them — ChEMBL alone contributes nine. `graph_sync`
triggers per source; `graph/sources.py` declares per file.

Three sources — **mhra, ema, pmda** — publish *both* documents and CSVs, so
they feed both stores. Counting the overlap once, **40 of the 49 scrapers are
used**; the remaining 9 are excluded with reasons recorded in
`graph/sources.py`.

## Two hosts, and why

| host | runs | why separate |
|------|------|--------------|
| **AWS** (Ubuntu 26.04) | Qdrant, ingest, search API, **Airflow** | Qdrant wants its vectors resident |
| **Azure** (2 vCPU / 16 GB) | graph build, **Neo4j** | Neo4j wants its store resident |

They are apart because their memory profiles conflict. Qdrant holds ~8 GB,
Neo4j is configured for 4 GB heap + 4 GB page cache, and a graph build needs
~6 GB for twenty minutes. Two of those on one 16 GB box means whichever is
largest gets paged out, and a swapped page cache is worse than a small one.

Airflow lives on the AWS host and drives the Azure host **over SSH** — a
`BashOperator` would run inside the Airflow container, which has no graph code,
no Neo4j and no spare memory.

## Scheduling

Only one DAG is on a clock.

```
@weekly ──► scrapers_pipeline
              each source's commit task emits
              Dataset("s3://moine-data/<s3_base>")
                   ├──► graph_sync         (35 CSV-publishing sources)
                   └──► vector_store_sync  (8 document sources)
```

The sync DAGs have **no schedule of their own**. They wake when data actually
lands, so a sync cannot race the scrape that feeds it.

Both use `DatasetAny`. A plain list of datasets means **AND** in Airflow — the
DAG waits for *every* listed dataset — which would have meant `graph_sync`
firing only when all 35 sources published in one window, i.e. never, silently.

`graph_sync`'s trigger set is derived from `graph/sources.py`, the single
declaration of what the graph reads, so adding a file there updates the trigger
with nothing to keep in step by hand.

---

## Operating

### Graph

```bash
bash deploy/build-graph.sh                              # ~21 min, builds + validates
NEO4J_PASSWORD='...' bash deploy/import-graph.sh        # ~3 min, backs up + replaces
```

`import-graph.sh` **refuses a build with no passing validation**. Neo4j has no
transaction around a bulk import — it replaces the store outright — so an
unchecked build silently becomes the live graph, and the failure mode is a
confident wrong answer rather than an error. It also dumps the current database
first; that dump is the only route back from a failed import.

Builds land in `~/graph-runs/<timestamp>/`, outside the repo. Two are kept.

### Vector store

```bash
bash deploy/airflow.sh up
~/vsenv/bin/python vector_store/ingest.py --prune
```

Ingest is incremental by S3 ETag, so re-running is cheap: a recent run skipped
92,397 unchanged documents and looked at 1,038. `--prune` removes vectors for
documents that have left S3 — without it, retrieval keeps citing documents that
no longer exist, which reads as a well-sourced answer to something untrue.

**Embedding is ~7 minutes per document on CPU.** A delta of tens is fine; a
backfill belongs on a GPU host.

---

## Replacing a host

The two hosts are not equally replaceable, and this is the thing to get right.

| | graph host | vector host |
|---|---|---|
| Irreplaceable state | **none** | **~27 GB of Qdrant vectors** |
| Rebuild cost | ~25 min, automatic | **hours on GPU, days on CPU** |
| Strategy | **throw away and rebuild** | **migrate the data** |

Everything on the graph host derives from S3. The vector store's embeddings do
not — regenerating them means re-embedding 93k documents.

**Never destroy the vector host before copying `~/qdrant` off it.**

### Replacing the graph host

1. Ubuntu 24.04, 2 vCPU / 16 GB, **100 GB disk**.
2. **Edit `graph/neo4j.conf.snippet`** — it contains the host's public IP in
   `server.default_advertised_address` and the two `advertised_address` lines.
   Neo4j Browser builds its `bolt://` URL from these; pointed at a dead host it
   loads and then fails to connect, and the error looks like a Neo4j problem
   rather than a config one. Commit and push before provisioning.
3. On the new box:
   ```bash
   git clone https://github.com/helmibiolyt/fullPipeline.git
   cp /safe/place/.env fullPipeline/automation/.env && chmod 600 fullPipeline/automation/.env
   NEO4J_PASSWORD='<choose>' bash fullPipeline/deploy/graph-host.sh
   bash fullPipeline/deploy/build-graph.sh
   NEO4J_PASSWORD='...' bash fullPipeline/deploy/import-graph.sh
   ```
4. Add the firewall rule for 7474/7687, **scoped to your address**.
5. Update Airflow's `graph_host` connection to the new IP.

**Faster:** if the old box still lives, `scp` its newest `~/graph-runs/<ts>/`
across and skip to `import-graph.sh` — five minutes instead of twenty-five. Or
restore the dump: `neo4j-admin database load biolyt --from-path=...`.

### Replacing the vector host

1. **Back up first, with Qdrant stopped** — copying a live storage directory
   can produce a corrupt snapshot, and you would only discover it when search
   returned nothing:
   ```bash
   docker stop qdrant
   tar czf ~/qdrant-backup.tar.gz -C ~ qdrant     # ~27 GB
   # copy it off the box, or push to S3
   ```
2. New host: match or exceed the current one. **Disk ≥ 60 GB.**
3. ```bash
   git clone https://github.com/helmibiolyt/fullPipeline.git
   cp /safe/place/.env fullPipeline/automation/.env
   cp /safe/place/vector.env fullPipeline/vector_store/.env
   bash fullPipeline/deploy/vector-host.sh
   ```
4. Restore before starting anything:
   ```bash
   docker stop qdrant && rm -rf ~/qdrant
   tar xzf qdrant-backup.tar.gz -C ~ && docker start qdrant
   ```
5. `bash deploy/airflow.sh up`, then recreate the Airflow connections and
   variables (below).
6. Anything pointing at the old address — the research agent, any client of the
   search API — needs the new one.

Airflow's own state (run history, connections) lives in its Postgres volume.
Losing it is survivable: the DAGs are dataset-triggered and fire on the next
publish regardless. Back up the volume if you want the history.

---

## Configuration that is not in the repo

Secrets are gitignored and must be placed by hand on each host.

| file | holds | used by |
|------|-------|---------|
| `automation/.env` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AIRFLOW_UID` | scrapers, graph build |
| `vector_store/.env` | `QDRANT_URL`, `QDRANT_API_KEY`, AWS keys, `HF_HOME` | API, ingest, sync DAG |
| `automation/config/*.pem` | SSH keys Airflow uses to reach both hosts | `graph_sync`, `vector_store_sync` |

On AWS prefer an **IAM instance role** over keys — nothing to rotate, nothing
to leak, and it survives the box being replaced. Azure cannot do this, so that
host needs a key file.

### Airflow needs, once per install

```
connection  graph_host    SSH → Azure host,  key at /opt/automation/config/graph.pem
connection  vector_host   SSH → the docker host IP (172.17.0.1), ubuntu
variable    neo4j_password
```

The variable **must** be named `neo4j_password`. Airflow masks values whose key
contains `password`; rename it and the password appears in plaintext in every
task log.

### Two host-level settings that are easy to miss

**`HF_HOME` must point at real disk.** `/tmp` on the AWS host is a 7.7 GB
*tmpfs* — RAM, not disk. The HuggingFace cache defaulted there, took 6.5 GB of
it, and ingest died with "No space left on device" while the actual disk had
40 GB free.

**`tesseract` is not installed**, so 1,038 image-only PDFs produce zero chunks.
They extract cleanly and contain no text layer. Install tesseract only if you
want them.

---

## Verifying a deployment

```bash
# graph
bash deploy/build-graph.sh --slice atorvastatin,erenumab,pembrolizumab
# expect: 0 failures, and the three fixtures passing

# graph, live
MATCH (n) RETURN count(n)                       # ~13.7M
CALL db.index.fulltext.queryNodes('entity_names','lung cancer') YIELD node
  WHERE node:Disease RETURN node.name LIMIT 3   # expect real diseases

# vector store
curl -H "api-key: $QDRANT_API_KEY" localhost:6333/collections/biolyt_docs
# expect: points_count ~3.24M
```

The graph validator checks referential integrity, key uniqueness across labels,
substance-resolution quality, fan-out outliers, source coverage, and three
biology fixtures (atorvastatin → HMG-CoA reductase, pembrolizumab → PD-1,
erenumab → CGRP receptor). It exits non-zero on any failure, which is what
gates the import.

---

## Known limitations

**Three sources cannot be refreshed.** `accessdata.fda.gov-orangebook`,
`chictr.org.cn` and `jrct.mhlw.go.jp` are blocked at IP level (Akamai and WAF
rules against datacenter addresses). Their data is already in the graph and now
frozen. Orange Book is the costly one: it supplies essentially all Patent and
Exclusivity nodes, and patent expiry is the most time-sensitive data here. A
residential proxy or different egress IP fixes it; no code change will.

**93% of Substances have no name** — 2.87M ChEMBL research compounds carrying
only an identifier. Reachable by ID, never by name or embedding.

**Six sources are permanently excluded**: pubchem (85% of the lake's rows,
redundant with ChEMBL's InChIKey), openalex (API budget exhausted), evs.nci,
loinc, cdisc, nupco. Each reason is recorded in `graph/sources.py`.

**Dates are strings.** The sources write at least six formats and one is the
literal text "Approved Prior to Jan 1, 1982", so date-range comparison does not
work in Cypher.

**The Qdrant API key was committed** in `vector_store/serve.sh` (`e059138`) and
is in the pushed history. Removed from HEAD; **it still needs rotating**.
