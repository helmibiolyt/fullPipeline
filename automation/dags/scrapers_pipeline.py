"""Single DAG orchestrating every scraper as its own node (TaskGroup).

Collapsed, the graph shows one box per source. Expanded, each box is the
per-source pipeline:

    scrape -> validate_local -> upload_run -> verify_run -> commit -> prune
                                       \-------------------------> rollback (one_failed)

Live S3 data is only ever mutated in `commit`, after S3 verification passes.
"""
from __future__ import annotations

import pendulum
from airflow.datasets import Dataset
from airflow.models.dag import DAG
from airflow.operators.empty import EmptyOperator
from airflow.operators.python import PythonOperator
from airflow.utils.task_group import TaskGroup
from airflow.utils.trigger_rule import TriggerRule

from scrape_pipeline import load_sources
from scrape_pipeline.callbacks import on_task_failure
from scrape_pipeline.registry import Source
from scrape_pipeline.settings import S3_BUCKET
from scrape_pipeline import runner, s3_io, validation

# size_class -> Airflow pool (create these pools in the UI/CLI; default pool
# is used automatically if a named pool does not exist).
POOL_BY_SIZE = {
    "small": "scrapers_small",
    "medium": "scrapers_medium",
    "heavy": "scrapers_heavy",
}

DEFAULT_ARGS = {
    "owner": "data-eng",
    "retries": 3,
    "retry_delay": pendulum.duration(minutes=5),
    "retry_exponential_backoff": True,
    "max_retry_delay": pendulum.duration(minutes=30),
    "on_failure_callback": on_task_failure,
}


def _py(task_id, fn, src: Source, tg, dag, **kw):
    return PythonOperator(
        task_id=task_id,
        python_callable=fn,
        op_kwargs={"src": src, "run_id": "{{ run_id }}"},
        task_group=tg,
        dag=dag,
        pool=POOL_BY_SIZE.get(src.size_class, "default_pool"),
        **kw,
    )


with DAG(
    dag_id="scrapers_pipeline",
    description="Scrape 43 sources, back up locally, then atomically publish to S3.",
    default_args=DEFAULT_ARGS,
    schedule="@weekly",
    start_date=pendulum.datetime(2026, 7, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,
    max_active_tasks=8,
    tags=["scraping", "s3", "moine-data"],
) as dag:

    normal_groups, last_groups = [], []      # collect for run-last wiring
    for src in load_sources():
        if not src.enabled:            # discovered but not verified yet — skip
            continue
        with TaskGroup(group_id=src.slug) as tg:
            # Restores the previous run's index/state (manifest `hydrate:`) so the
            # scraper can skip what it already has. No-op when nothing is declared.
            hydrate = _py(
                "hydrate", lambda src, run_id: s3_io.hydrate(src, run_id),
                src, tg, dag, retries=1,
            )
            scrape = _py(
                "scrape", lambda src, run_id: runner.run_scraper(src, run_id),
                src, tg, dag,
                # timeout_min <= 0 means "no time limit" — for large, resumable crawls
                # (e.g. mhra, eu_ctr) where the scrape legitimately runs for many hours
                # and must not be killed while still making progress.
                execution_timeout=(
                    pendulum.duration(minutes=src.timeout_min)
                    if src.timeout_min and src.timeout_min > 0 else None
                ),
            )
            collect = _py(
                "collect", lambda src, run_id: runner.collect(src, run_id),
                src, tg, dag, retries=0,
            )
            validate = _py(
                "validate_local", lambda src, run_id: validation.validate_local(src, run_id),
                src, tg, dag, retries=0,
            )
            upload = _py(
                "upload_run", lambda src, run_id: s3_io.upload_run(src, run_id),
                src, tg, dag,
            )
            verify = _py(
                "verify_run", lambda src, run_id: s3_io.verify_run(src, run_id),
                src, tg, dag, retries=2,
            )
            # The pointer flip inside commit IS the publish event, so the task
            # emits a per-source Dataset. Downstream DAGs (graph load, vector
            # indexing) schedule on these and therefore wake only for the
            # sources that actually changed, instead of polling all 49.
            commit = _py(
                "commit", lambda src, run_id: s3_io.commit(src, run_id),
                src, tg, dag,
                outlets=[Dataset(f"s3://{S3_BUCKET}/{src.s3_base}")],
            )
            prune = _py(
                "prune_runs", lambda src, run_id: s3_io.prune_runs(src),
                src, tg, dag, retries=1,
                trigger_rule=TriggerRule.NONE_FAILED_MIN_ONE_SUCCESS,
            )
            cleanup = _py(
                "cleanup_local", lambda src, run_id: runner.cleanup_local(src, run_id),
                src, tg, dag, retries=1,
            )
            rollback = _py(
                "rollback_staging", lambda src, run_id: s3_io.rollback_staging(src, run_id),
                src, tg, dag, retries=1,
                trigger_rule=TriggerRule.ONE_FAILED,
            )

            # Local data is deleted only after a verified S3 commit (success path).
            hydrate >> scrape >> collect >> validate >> upload >> verify >> commit >> [prune, cleanup]
            [upload, verify, commit] >> rollback

        (last_groups if src.run_last else normal_groups).append((tg, scrape))

    # run_last sources start only after every other group has finished (any state),
    # so their long/blocking crawls run alone at the end of the DAG.
    if last_groups and normal_groups:
        gate = EmptyOperator(task_id="all_others_done",
                             trigger_rule=TriggerRule.ALL_DONE, dag=dag)
        for tg, _ in normal_groups:
            tg >> gate
        for _, scrape_task in last_groups:
            gate >> scrape_task
