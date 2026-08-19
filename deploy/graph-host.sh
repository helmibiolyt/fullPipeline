#!/usr/bin/env bash
# Provision a graph host: builds the graph from S3 and serves it as Neo4j.
#
#     git clone https://github.com/helmibiolyt/fullPipeline.git
#     bash fullPipeline/deploy/graph-host.sh
#
# Idempotent. Installs Python + Neo4j, applies the tuned config, and creates
# the directory layout. It does NOT build or import the graph - those are
# separate operations with their own runtimes, see build-graph.sh and
# import-graph.sh.
#
# Sizing this encodes: 2 vCPU / 16 GB, with Neo4j and the build coexisting.
# Neo4j gets 4G heap + 4G page cache rather than the usual "everything
# available", because the weekly rebuild needs ~6G on the same box and a
# swapped page cache is worse than a small one.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

VENV="${GRAPH_VENV:-$HOME/graphenv}"
RUNS="${GRAPH_RUNS:-$HOME/graph-runs}"

step "system packages"
apt_install curl gnupg ca-certificates

step "python environment"
make_venv "$VENV" "$REPO/graph/requirements.txt"
check_aws "$VENV/bin/python"

step "build output directory (outside the repo, never committed)"
mkdir -p "$RUNS"; ok "$RUNS"

step "neo4j"
if ! command -v neo4j >/dev/null; then
  sudo install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://debian.neo4j.com/neotechnology.gpg.key \
    | sudo gpg --dearmor --yes -o /etc/apt/keyrings/neotechnology.gpg
  echo 'deb [signed-by=/etc/apt/keyrings/neotechnology.gpg] https://debian.neo4j.com stable 5' \
    | sudo tee /etc/apt/sources.list.d/neo4j.list >/dev/null
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq neo4j cypher-shell
fi
ok "neo4j $(neo4j --version)"

step "neo4j configuration"
CONF=/etc/neo4j/neo4j.conf
[ -f "$CONF.orig" ] || sudo cp "$CONF" "$CONF.orig"
# Re-applied from the pristine copy each run, so editing the snippet in git and
# re-provisioning actually takes effect instead of appending a second block.
sudo cp "$CONF.orig" "$CONF"
# Which memory profile fits. Handing Neo4j the 16 GB numbers on an 8 GB box
# asks for 8 GB on a machine that has 8, which leaves the OS nothing and the
# build nothing - so the size of the host picks the profile rather than the
# person running this remembering to.
TOTAL_MB=$(free -m | awk '/^Mem:/ {print $2}')
if [ "${TOTAL_MB:-0}" -lt 12000 ]; then
  SNIPPET="$REPO/graph/neo4j.conf.8gb.snippet"
  warn "${TOTAL_MB} MB RAM - using the 8 GB profile (2G heap / 3G page cache)."
  warn "The build does NOT fit alongside Neo4j at this size: rebuild-graph.sh"
  warn "will stop the database for the build, so a rebuild is ~50 minutes of"
  warn "downtime rather than ~5. See the snippet for the alternative."
else
  SNIPPET="$REPO/graph/neo4j.conf.snippet"
fi
sudo bash -c "cat '$SNIPPET' >> '$CONF'"
# Community Edition allows exactly one user database, so the imported one has
# to become the default rather than be created alongside `neo4j`.
grep -q '^initial.dbms.default_database' "$CONF" \
  || echo 'initial.dbms.default_database=biolyt' | sudo tee -a "$CONF" >/dev/null
ok "config applied from $(basename "$SNIPPET")"

step "neo4j password"
if [ -n "${NEO4J_PASSWORD:-}" ]; then
  sudo systemctl stop neo4j 2>/dev/null || true
  sudo -u neo4j neo4j-admin dbms set-initial-password "$NEO4J_PASSWORD" 2>/dev/null \
    && ok "password set from NEO4J_PASSWORD" \
    || warn "password already set; set-initial-password only works before first start"
else
  warn "NEO4J_PASSWORD not set - leaving any existing password alone."
  echo "        To set one on a fresh host:  NEO4J_PASSWORD='...' bash $0"
fi

cat <<TXT

$(step "done")
  code    $REPO            (git pull to update)
  venv    $VENV
  builds  $RUNS            (outside the repo)
  neo4j   http://<host>:7474   bolt://<host>:7687   database 'biolyt'

  next:  bash $REPO/deploy/build-graph.sh      # ~30 min, writes to $RUNS
         bash $REPO/deploy/import-graph.sh     # ~5 min, replaces the database

  NOT done here, deliberately:
    - the cloud firewall. 7474/7687 listen on 0.0.0.0 because the database has
      to be reachable; the NSG/security-group rule that limits WHO can reach it
      is not something a script on the box can set, and defaulting it open
      would publish the graph to the internet.
TXT
