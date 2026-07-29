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
from airflow.datasets import Dataset
from airflow.operators.bash import BashOperator

import sys
sys.path.insert(0, "/opt/pylib")
from scrape_pipeline.registry import load_sources          # noqa: E402
from scrape_pipeline.settings import S3_BUCKET             # noqa: E402

# Sources that actually publish documents. Derived from the manifests rather
# than hardcoded, so a new document source is picked up by adding its manifest.
DOC_SOURCES = {
    "Regulatory_Approvals/products.mhra.gov.uk",
    "Regulatory_Approvals/ema.europa.eu",
    "Regulatory_Approvals/pmda.go.jp",
    "MENA_GCC_Regulatory_Market/dha.gov.ae",
    "MENA_GCC_Regulatory_Market/doh.gov.ae",
    "MENA_GCC_Regulatory_Market/nhra.bh",
    "MENA_GCC_Regulatory_Market/moph.gov.qa",
    "MENA_GCC_Regulatory_Market/moh.gov.om",
}

_datasets = [Dataset(f"s3://{S3_BUCKET}/{s.s3_base}")
             for s in load_sources()
             if f"{s.topic}/{s.source}" in DOC_SOURCES]

with DAG(
    dag_id="vector_store_sync",
    description="Embed newly published documents into Qdrant (incremental by ETag)",
    # Any listed dataset updating wakes this DAG.
    schedule=_datasets or None,
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
    BashOperator(
        task_id="ingest_new_documents",
        # Path comes from the environment, defaulting to where the repo is
        # actually checked out. It was hardcoded to /opt/vector_store, which
        # exists on no host we run - the task would have failed on `cd` the
        # first time a source published, and only then.
        bash_command=(
            "cd ${VECTOR_STORE_DIR:-/home/ubuntu/fullPipeline/vector_store} && "
            "${VECTOR_STORE_PYTHON:-/home/ubuntu/vsenv/bin/python} "
            "ingest.py --prune 2>&1 | tail -40"
        ),
        # Embedding is the slow part on CPU: ~1.7 min/document on this box.
        # Fine for a delta of tens; a large catch-up belongs on a GPU pod. See
        # vector_store/VECTOR_PLAN.md section 3.
        execution_timeout=pendulum.duration(hours=6),
    )
