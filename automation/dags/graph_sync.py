"""Rebuild the graph when a CSV source publishes, and import it only if valid.

The counterpart to vector_store_sync, and the half that was missing: the
scraper DAG has always emitted a per-source Dataset on commit, and only the
vector store listened. CSV sources published to nothing.

**This DAG runs over SSH, not locally.** Airflow lives on the vector host; the
graph is built on a separate machine, because Qdrant wanting its vectors
resident and Neo4j wanting its store resident do not fit in one 16 GB box. A
BashOperator here would run inside the Airflow container, which has no graph
code, no Neo4j and no 6 GB to spare - the first version of this DAG did exactly
that and could never have worked.

It delegates to `deploy/build-graph.sh` and `deploy/import-graph.sh` rather
than re-implementing them. Those scripts are also what a human runs by hand, so
there is one sequence to get right instead of two that drift apart - and the
earlier inline version had already drifted: it stopped after generating headers
and never imported anything, so the graph would have been rebuilt weekly and
never reached the database.

The validation gate lives inside build-graph.sh: it exits non-zero when
validate.py fails, so with Airflow's default all_success rule the import task
simply never starts. Neo4j has no transaction around a bulk import - it
replaces the store outright - so an unchecked build silently becomes the live
graph, and the failure mode is a confident wrong answer rather than an error.

Setup this DAG needs, once:

    Airflow connection  `graph_host`   SSH, host + user + private key
    Airflow variable    `neo4j_password`

The variable name ends in `password`, which is what makes Airflow mask it in
task logs. Renaming it to something without that word puts the password in
plaintext in every log line.
"""
from __future__ import annotations

import pendulum
from airflow import DAG
from airflow.datasets import Dataset, DatasetAny
from airflow.providers.ssh.operators.ssh import SSHOperator

import sys
sys.path.insert(0, "/opt/pylib")
from scrape_pipeline.registry import load_sources          # noqa: E402
from scrape_pipeline.settings import S3_BUCKET             # noqa: E402

# Which sources should wake this DAG comes from graph/sources.py - the single
# declaration of what the graph actually reads - not from a list maintained
# here. The list that used to live here was wrong in both directions.
#
# It excluded mhra, ema and pmda as "document sources", but those publish
# documents AND CSVs: mhra_data/raw_metadata.csv is 78,215 UK products,
# ema_medicines.csv is every centrally authorised EU medicine, and five EMA
# tables are the entire RegulatoryEvent population. Their CSV updates would
# never have rebuilt the graph, silently.
#
# It also included ~14 sources the graph never reads, each of which would have
# triggered a 30-minute rebuild and a Neo4j restart to produce a byte-identical
# graph.
#
# Deriving it means adding a file to sources.py updates the trigger too, with
# nothing to keep in step by hand.
sys.path.insert(0, "/opt/graph")            # mounted read-only by compose
import sources as graph_sources             # noqa: E402

_GRAPH_SOURCES = {"/".join(d["file"].split("/")[:2])
                  for d in graph_sources.INCLUDED}

_datasets = [Dataset(f"s3://{S3_BUCKET}/{s.s3_base}")
             for s in load_sources()
             if f"{s.topic}/{s.source}" in _GRAPH_SOURCES]

SSH_CONN = "graph_host"
DEPLOY = "~/fullPipeline/deploy"

with DAG(
    dag_id="graph_sync",
    description="Rebuild the graph on CSV-source publish; import only if valid",
    # DatasetAny, not a plain list. A list means AND in Airflow: the DAG waits
    # until EVERY listed dataset has updated since its last run. This one
    # listens to the 27 sources that feed the graph, so it would have fired
    # only when all 27 published in the same window - effectively never, with
    # no error and no failed task. Just a graph that quietly stopped updating.
    schedule=DatasetAny(*_datasets) if _datasets else None,
    start_date=pendulum.datetime(2026, 7, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,          # a second build would fight the first for RAM
    default_args={"retries": 0},   # a failed build is a bug to read, not retry
    tags=["graph"],
) as dag:

    # Deliberately no `git pull` task. Deploying code and processing data are
    # different decisions, and a DAG that pulls before every run turns any
    # pushed commit into an unreviewed production deploy on the next scrape.
    # Updating the graph host is `git pull` by hand, when intended.

    build = SSHOperator(
        task_id="build_and_validate",
        ssh_conn_id=SSH_CONN,
        # Writes to ~/graph-runs/<timestamp>/, validates it, and marks it
        # importable only on success. Keeps the two most recent runs.
        command=f"bash {DEPLOY}/build-graph.sh",
        conn_timeout=60,
        cmd_timeout=4 * 60 * 60,     # a full build is ~30 min; 4h is the guard
        get_pty=True,                # so sudo inside the script has a terminal
    )

    # Only runs when build_and_validate exited 0, which it does only when
    # validate.py passed. import-graph.sh independently refuses a build
    # directory with no .validated marker, so the gate holds even if this DAG
    # is edited to reorder the tasks.
    import_neo4j = SSHOperator(
        task_id="import_to_neo4j",
        ssh_conn_id=SSH_CONN,
        command=(
            "NEO4J_PASSWORD='{{ var.value.get('neo4j_password') }}' "
            f"bash {DEPLOY}/import-graph.sh"
        ),
        conn_timeout=60,
        cmd_timeout=60 * 60,
        get_pty=True,
    )

    build >> import_neo4j
