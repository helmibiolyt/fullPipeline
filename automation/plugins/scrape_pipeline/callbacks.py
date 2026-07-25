"""Failure/alert callbacks. Logs by default; wire Slack/email via env later."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def on_task_failure(context) -> None:
    ti = context.get("task_instance")
    log.error(
        "TASK FAILED dag=%s task=%s run=%s try=%s: %s",
        getattr(ti, "dag_id", "?"),
        getattr(ti, "task_id", "?"),
        context.get("run_id"),
        getattr(ti, "try_number", "?"),
        context.get("exception"),
    )
    # Hook point: post to Slack/email/PagerDuty here.
