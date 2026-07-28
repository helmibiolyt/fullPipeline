#!/bin/bash
# Bring Airflow up, run the pipeline, take it back down.
#
# Airflow holds ~1.3 GB permanently (webserver 715 MB, scheduler 548 MB,
# postgres 56 MB) to execute one DAG a week. On this box that RAM is not
# spare: when memory got tight, the kernel paged out Qdrant's quantized
# vectors and dense search went from 74 ms to 1.5 s. So the services only
# exist while the run does.
#
# 'stop'/'start', never 'down'/'up' - down removes the containers and the next
# start re-runs airflow-init. The Postgres volume holds all DAG history and
# survives either way, but re-init is slow and unnecessary.
#
# NOT scheduled. Install with:  crontab -e   then:
#   17 3 * * 0  /home/ubuntu/fullPipeline/automation/weekly_run.sh
set -uo pipefail
DIR=/home/ubuntu/fullPipeline/automation
LOG=/home/ubuntu/weekly_run.log
DAG=scrapers_pipeline
SYNC_DAG=vector_store_sync
MAX_MIN=${MAX_MIN:-720}          # give up after 12 h and shut down anyway
log(){ echo "$(date -u '+%F %T') | $*" >> "$LOG"; }
cd "$DIR" || { log 'FATAL cannot cd'; exit 1; }

shutdown_airflow(){ log 'stopping airflow'; docker compose stop >/dev/null 2>&1; log "free: $(free -m|awk '/^Mem:/{print $7}') MB"; }
trap shutdown_airflow EXIT               # stop even on error or timeout

log '=== weekly run starting ==='
docker compose start >/dev/null 2>&1 || docker compose up -d >/dev/null 2>&1
for i in $(seq 1 60); do
  docker compose exec -T airflow-scheduler airflow jobs check --job-type SchedulerJob >/dev/null 2>&1 && break
  sleep 10
done
docker compose exec -T airflow-scheduler airflow jobs check --job-type SchedulerJob >/dev/null 2>&1   || { log 'FATAL scheduler never became healthy'; exit 1; }
log 'scheduler healthy'

RUN=cron__$(date -u +%Y%m%dT%H%M%S)
docker compose exec -T airflow-scheduler airflow dags unpause "$DAG" >/dev/null 2>&1
docker compose exec -T airflow-scheduler airflow dags trigger -r "$RUN" "$DAG" >/dev/null 2>&1   || { log "FATAL could not trigger $DAG"; exit 1; }
log "triggered $DAG run_id=$RUN"

deadline=$(( $(date +%s) + MAX_MIN*60 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  st=$(docker compose exec -T airflow-scheduler airflow dags list-runs -d "$DAG" --no-backfill -o plain 2>/dev/null | awk -v r="$RUN" '$2==r{print $3}')
  case "$st" in
    success) log "$DAG succeeded"; break ;;
    failed)  log "$DAG FAILED"; break ;;
  esac
  sleep 60
done
[ -z "${st:-}" ] && log "WARNING $DAG still running at ${MAX_MIN}m cap - shutting down anyway"

# vector_store_sync is Dataset-triggered by the scrapers' commit tasks, so it
# only starts after they finish. Stopping the scheduler here would strand it.
log "waiting for $SYNC_DAG"
for i in $(seq 1 120); do
  running=$(docker compose exec -T airflow-scheduler airflow dags list-runs -d "$SYNC_DAG" --state running -o plain 2>/dev/null | tail -n +2 | wc -l)
  queued=$(docker compose exec -T airflow-scheduler airflow dags list-runs -d "$SYNC_DAG" --state queued -o plain 2>/dev/null | tail -n +2 | wc -l)
  [ "$running" -eq 0 ] && [ "$queued" -eq 0 ] && { log "$SYNC_DAG idle"; break; }
  sleep 30
done
log '=== weekly run done ==='
