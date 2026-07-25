#!/usr/bin/env python3
"""Per-scraper PASS/FAIL matrix for the latest scrapers_pipeline run.

Run inside the container (it needs Airflow + the metadata DB):

    sudo docker compose exec airflow-scheduler python /opt/pylib/dag_report.py
    # or a specific run:
    sudo docker compose exec airflow-scheduler python /opt/pylib/dag_report.py <run_id>

Each scraper is a TaskGroup; this aggregates its sub-tasks into one verdict.
"""
import sys
from collections import defaultdict

from airflow.models import DagRun, TaskInstance
from airflow.utils.session import create_session

DAG_ID = "scrapers_pipeline"


def verdict(states: set) -> str:
    if "failed" in states or "upstream_failed" in states:
        return "FAIL"
    if "running" in states or "queued" in states or "scheduled" in states:
        return "RUNNING"
    if states and states <= {"success", "skipped"}:
        return "PASS"
    if states == {"skipped"}:
        return "SKIPPED"
    return "PENDING"


def main() -> None:
    want_run = sys.argv[1] if len(sys.argv) > 1 else None
    with create_session() as s:
        q = s.query(DagRun).filter(DagRun.dag_id == DAG_ID)
        dr = (q.filter(DagRun.run_id == want_run).first() if want_run
              else q.order_by(DagRun.execution_date.desc()).first())
        if not dr:
            sys.exit("no dag run found for " + DAG_ID)

        tis = s.query(TaskInstance).filter(
            TaskInstance.dag_id == DAG_ID,
            TaskInstance.run_id == dr.run_id,
        ).all()

        groups = defaultdict(dict)          # group -> {task_leaf: state}
        for ti in tis:
            grp, _, leaf = ti.task_id.partition(".")
            groups[grp][leaf or grp] = ti.state or "pending"

    print(f"DAG run: {dr.run_id}   overall: {dr.state}\n")
    tally = defaultdict(int)
    rows = []
    for grp in sorted(groups):
        states = set(groups[grp].values())
        v = verdict(states)
        tally[v] += 1
        # show which sub-task failed, if any
        failed = [t for t, st in groups[grp].items() if st in ("failed", "upstream_failed")]
        note = f"failed at: {', '.join(sorted(failed))}" if failed else ""
        rows.append((v, grp, note))

    for v in ("FAIL", "RUNNING", "PENDING", "SKIPPED", "PASS"):
        for rv, grp, note in rows:
            if rv == v:
                print(f"{v:8} {grp:52} {note}")
    print("\n==== " + "  ".join(f"{k}:{n}" for k, n in sorted(tally.items())) + " ====")


if __name__ == "__main__":
    main()
