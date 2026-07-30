#!/usr/bin/env bash
# Airflow, which schedules both stores off the scrapers.
#
#     bash deploy/airflow.sh up | down | status | logs
#
# Dataset-driven, not cron: every source's `commit` task emits
# Dataset("s3://<bucket>/<s3_base>"), and the two sync DAGs schedule on those.
# They wake only for sources that actually published, instead of polling 49.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

cd "$REPO/automation"
[ -f .env ] || warn "automation/.env missing - Airflow needs AIRFLOW_UID and AWS keys"

case "${1:-status}" in
  up)     step "starting airflow"; docker compose up -d
          ok "webserver on :8080 once healthy" ;;
  down)   step "stopping airflow"; docker compose down ;;
  logs)   docker compose logs -f --tail=100 ;;
  status) docker compose ps ;;
  *)      die "usage: $0 up|down|status|logs" ;;
esac
