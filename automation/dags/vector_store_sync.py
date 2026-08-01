"""Keep the vector store current as scrapers publish new documents.

Triggered by Dataset, not by a clock. Every source's `commit` task in
scrapers_pipeline emits Dataset("s3://<bucket>/<s3_base>"), so this DAG wakes
when a document source actually publishes something - no polling, and no
guessing at a cron that runs before or after the scrape it depends on.

Only document-bearing sources are listened to. Of 49 scrapers, three hold 99.8%
of the documents (mhra 70,559, ema 22,150, pmda 547) and the MENA sources hold
the rest; the other 40 publish CSVs, which belong to the graph.

The ingest itself is incremental by S3 ETag, so this is cheap even when a
source commits without adding documents: unchanged files are skipped before any
download, extract or embed happens.
"""
from __future__ import annotations

import pendulum
from airflow import DAG
from airflow.datasets import Dataset, DatasetAny
from airflow.providers.ssh.operators.ssh import SSHOperator

import sys
sys.path.insert(0, "/opt/pylib")
from scrape_pipeline.registry import load_sources          # noqa: E402

# Woken by "this source produced documents", not by "this source ran".
#
# Two earlier shapes, both wrong in different directions.
#
# A hand-maintained DOC_SOURCES set of ten. Its own comment said it had to be
# maintained by hand, and it had already gone stale twice - two sources were
# found missing from it after their documents were in Qdrant, so nothing woke
# this DAG when they published. fetch_linked_docs made that worse rather than
# better: it runs for every source, so any of the 49 can start producing
# documents on any run and a curated list is guaranteed to fall behind.
#
# Then every source's commit dataset. That fixed the staleness and introduced a
# cost - commit fires on every successful run, and a wake here is not cheap. It
# lists the whole bucket and scrolls ~93k points in Qdrant before it can decide
# nothing changed, and with max_active_runs=1 those wakes queue.
#
# So the scrapers now emit documents://<slug> from a task that SKIPS when the
# run produced no documents, and a skipped task emits no dataset event. No list
# to maintain, and no wake for a CSV-only run.
_datasets = [Dataset(f"documents://{s.slug}") for s in load_sources()]

with DAG(
    dag_id="vector_store_sync",
    description="Embed newly published documents into Qdrant (incremental by ETag)",
    # DatasetAny, not a plain list. A list means AND in Airflow: the DAG waits
    # until EVERY listed dataset has updated since its last run. This one
    # listens to every source, so it would have fired only when all of them published in the
    # same window - which is to say, effectively never, with no error and no
    # failed task to notice. Just a store that quietly stopped updating.
    schedule=DatasetAny(*_datasets) if _datasets else None,
    start_date=pendulum.datetime(2026, 7, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,          # one ingest at a time; they share the model
    default_args={"retries": 1, "retry_delay": pendulum.duration(minutes=10)},
    tags=["vector-store"],
) as dag:

    # --prune removes vectors for documents that have left S3. It matters for
    # mirror:true sources, where a run legitimately deletes files: without it
    # retrieval keeps citing documents that no longer exist, which reads as a
    # perfectly well-sourced answer to something untrue.
    # SSHOperator, not BashOperator - the same correction graph_sync needed.
    # A BashOperator runs INSIDE the Airflow container, which has no
    # vector_store code, no venv, no torch and no models; it failed in two
    # seconds on `cd` to a path that does not exist there. The ingest has to
    # run on the host, where all of that lives.
    #
    # The connection points at this same machine. That looks odd and is
    # correct: Airflow is containerised and the work is not.
    SSHOperator(
        task_id="ingest_new_documents",
        ssh_conn_id="vector_host",
        # Trailing space matters: SSHOperator treats a command ending in ".sh"
        # as a path to a Jinja template file. This one ends in a flag, but the
        # habit is cheap and the failure mode is obscure.
        command=(
            "cd ${VECTOR_STORE_DIR:-$HOME/fullPipeline/vector_store} && "
            "${VECTOR_STORE_PYTHON:-$HOME/vsenv/bin/python} ingest.py --prune "
        ),
        conn_timeout=60,
        # Embedding is the slow part on CPU: ~1.7 min/document on this box.
        # Fine for a delta of tens; a large catch-up belongs on a GPU pod.
        cmd_timeout=6 * 60 * 60,
        get_pty=True,
    )
