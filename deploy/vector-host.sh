#!/usr/bin/env bash
# Provision a vector-store host: Qdrant, the embedding/ingest code, the search
# API, and the Airflow that schedules them.
#
#     git clone https://github.com/helmibiolyt/fullPipeline.git
#     bash fullPipeline/deploy/vector-host.sh
#
# Idempotent. Does NOT ingest documents - that is hours of embedding, see
# vector_store/ingest.py, and on CPU it belongs on a GPU host for a backfill.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

VENV="${VS_VENV:-$HOME/vsenv}"
QDRANT_DATA="${QDRANT_DATA:-$HOME/qdrant}"

step "system packages"
apt_install curl ca-certificates docker.io docker-compose-v2
sudo systemctl enable --now docker
# So docker works without sudo after the next login.
groups | grep -q docker || { sudo usermod -aG docker "$USER"; warn "log out and back in for docker group membership"; }

step "python environment"
make_venv "$VENV" "$REPO/vector_store/requirements.txt"
check_aws "$VENV/bin/python"

step "qdrant"
mkdir -p "$QDRANT_DATA"
if ! docker ps --format '{{.Names}}' | grep -qx qdrant; then
  docker rm -f qdrant 2>/dev/null || true
  docker run -d --name qdrant --restart unless-stopped \
    -p 6333:6333 -p 6334:6334 \
    -v "$QDRANT_DATA:/qdrant/storage" \
    qdrant/qdrant:latest
fi
ok "qdrant up, storage $QDRANT_DATA ($(du -sh "$QDRANT_DATA" 2>/dev/null | cut -f1))"

step "search API"
if [ -f "$REPO/vector_store/biolyt-api.service" ]; then
  sudo cp "$REPO/vector_store/biolyt-api.service" /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now biolyt-api
  ok "biolyt-api $(systemctl is-active biolyt-api)"
fi

cat <<TXT

$(step "done")
  code    $REPO             (git pull to update)
  venv    $VENV
  qdrant  $QDRANT_DATA -> localhost:6333
  api     systemctl status biolyt-api

  next:  bash $REPO/deploy/airflow.sh up      # scheduler + webserver
         $VENV/bin/python $REPO/vector_store/ingest.py --prune

  NOT done here, deliberately:
    - document ingest. Embedding ~93k documents is many hours on CPU; a
      backfill belongs on a GPU host, an incremental delta runs from Airflow.
    - the cloud firewall. Qdrant on 6333 must not be reachable from the
      internet; that is a security-group rule, not a box-local setting.
TXT
