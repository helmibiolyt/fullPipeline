"""Rebuild the graph when a CSV source publishes, and import it only if valid.

The counterpart to vector_store_sync, and the half that was missing: the
scraper DAG has always emitted a per-source Dataset on commit, and only the
vector store listened. CSV sources published to nothing.

Two things make this different from the vector store's sync:

* **The build is whole, not incremental.** There is no delta path in build.py
  and that is deliberate - the graph's correctness comes from resolving names
  against a dictionary assembled from every source in one pass, so a partial
  rebuild would resolve against a half-built dictionary. A full run is ~30-60
  minutes, which is cheap against a weekly scrape.

* **Import is gated on validation.** build -> validate -> import, and the
  import only runs if validate exits 0. Neo4j has no transaction around
  `neo4j-admin import`: it replaces the store outright. Importing an unchecked
  build means a bad scrape silently becomes the live graph, and the failure
  mode is a confident wrong answer rather than an error. The gate is the whole
  point of this DAG - remove it and it is just a cron job.
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

# The document sources vector_store_sync owns. Everything else publishes CSVs,
# which is what the graph is built from. Listed as an exclusion rather than an
# inclusion so a new CSV scraper is picked up by adding its manifest, with no
# edit here - the same property that makes the vector DAG's list maintainable.
DOC_ONLY = {
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
             if f"{s.topic}/{s.source}" not in DOC_ONLY]

GRAPH = "${GRAPH_DIR:-/home/ubuntu/graphbuild}"
PY = "${GRAPH_PYTHON:-/home/ubuntu/graphenv/bin/python}"
# Built into a run-specific directory, never over the live one. If the build
# dies halfway the previous output is still intact and still importable.
OUT = "$GRAPH_OUT"

with DAG(
    dag_id="graph_sync",
    description="Rebuild graph CSVs on CSV-source publish; import only if valid",
    schedule=_datasets or None,
    start_date=pendulum.datetime(2026, 7, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,          # a second build would fight the first for RAM
    default_args={"retries": 0},   # a failed build is a bug to read, not retry
    tags=["graph"],
) as dag:

    env = {"GRAPH_OUT": "{{ var.value.get('graph_dir', '/home/ubuntu/graphbuild') }}"
                        "/runs/{{ ts_nodash }}"}

    # --max-mem-gb is a self-imposed ceiling, not a tuning knob. Neo4j runs on
    # this same 16 GB box and stays up while the build runs, so the build has
    # to fit in what is left: 6 GB against a measured peak of ~4-5 GB. Without
    # a ceiling the kernel chooses the OOM victim, and it would choose Neo4j -
    # the bigger, older process.
    build = BashOperator(
        task_id="build",
        bash_command=f"cd {GRAPH} && {PY} build.py --all --out {OUT} "
                     f"--max-mem-gb ${{GRAPH_MAX_MEM_GB:-6}}",
        env=env, append_env=True,
        execution_timeout=pendulum.duration(hours=4),
    )

    # Exits non-zero on any FAIL-level check: dangling edge endpoints, a key
    # used by two labels, a missing fixture. That exit code is the gate.
    validate = BashOperator(
        task_id="validate",
        bash_command=f"cd {GRAPH} && {PY} validate.py --dir {OUT}",
        env=env, append_env=True,
        execution_timeout=pendulum.duration(minutes=30),
    )

    headers = BashOperator(
        task_id="generate_import",
        bash_command=f"cd {GRAPH} && {PY} neo4j_import.py --dir {OUT} "
                     f"--out {OUT}/import",
        env=env, append_env=True,
    )

    # Only reached when validate succeeded, because the default trigger rule is
    # all_success. Promotion is last: `current` is what the import reads, so
    # flipping it earlier would publish a build that had not passed yet.
    promote = BashOperator(
        task_id="promote",
        bash_command=f"cd {GRAPH} && ln -sfn {OUT} runs/current && "
                     f"ls -l runs/current",
        env=env, append_env=True,
    )

    build >> validate >> headers >> promote
