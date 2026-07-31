#!/usr/bin/env bash
# Replace the Neo4j database with a validated build.
#
#     NEO4J_PASSWORD='...' bash deploy/import-graph.sh            # newest build
#     NEO4J_PASSWORD='...' bash deploy/import-graph.sh <build-dir>
#
# This sequence took three failed imports to get right, which is why it is a
# script and not a paragraph in a README:
#
#   1. the data files carry their own header row, and with a separate header
#      file neo4j-admin parses that row as data - `enrollment` fails :int on
#      line 0
#   2. trial titles contain newlines; --multiline-fields reads them but kills
#      parallel parsing for the whole import
#   3. a column typed :int must actually hold ints - ChiCTR writes enrollment
#      as prose, and the import aborts after the node phase with a byte offset
#      for a message
#
# stage_for_neo4j.py handles all three. Skipping it fails ~40 seconds in.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

VENV="${GRAPH_VENV:-$HOME/graphenv}"
RUNS="${GRAPH_RUNS:-$HOME/graph-runs}"
DB="${NEO4J_DATABASE:-biolyt}"
IMPORT_DIR=/var/lib/neo4j/import

BUILD="${1:-$(ls -1dt "$RUNS"/*/ 2>/dev/null | head -1)}"
BUILD="${BUILD%/}"
[ -n "$BUILD" ] && [ -d "$BUILD" ] || die "no build found in $RUNS - run build-graph.sh first"
[ -f "$BUILD/.validated" ] || die "$BUILD has no passing validation. Refusing to import.
        Neo4j has no transaction around a bulk import: it replaces the store
        outright, so an unchecked build silently becomes the live graph."
[ -n "${NEO4J_PASSWORD:-}" ] || die "set NEO4J_PASSWORD"
ok "importing $BUILD"

step "generating typed headers"
"$VENV/bin/python" "$REPO/graph/neo4j_import.py" --dir "$BUILD" --out "$BUILD/import" >/dev/null

step "staging (strip header rows, collapse newlines, enforce types)"
rm -rf /tmp/neo4j-stage
"$VENV/bin/python" "$REPO/graph/stage_for_neo4j.py" --dir "$BUILD" --out /tmp/neo4j-stage

step "stopping neo4j"
sudo systemctl stop neo4j

# Back up before overwriting. neo4j-admin import replaces the store outright
# with no transaction and no undo: a duplicate column in one header once failed
# 786ms in, AFTER the previous store was gone, and the only route back was a
# 20-minute rebuild from S3. A dump costs a couple of minutes and makes that a
# restore instead.
#
# Skippable with SKIP_BACKUP=1 when disk is genuinely tight - the graph host has
# 13 GB free and a dump is ~2 GB - but skipping it is a decision, not a default.
if [ "${SKIP_BACKUP:-0}" != "1" ]; then
  # Under /var/lib/neo4j, not $HOME. neo4j-admin runs as the neo4j user, which
  # cannot traverse /home/azureuser (mode 750), so a dump there fails with
  # "is not an existing directory" even though the directory is right there.
  BACKUP_DIR="${BACKUP_DIR:-/var/lib/neo4j/backups}"
  sudo mkdir -p "$BACKUP_DIR"; sudo chown neo4j:neo4j "$BACKUP_DIR"
  step "backing up the current database first"
  if sudo -u neo4j neo4j-admin database dump "$DB" --to-path="$BACKUP_DIR"        --overwrite-destination=true >/tmp/dump.log 2>&1; then
    ok "dump: $(sudo du -sh "$BACKUP_DIR" 2>/dev/null | cut -f1) in $BACKUP_DIR"
    # Keep two. Older ones describe a graph nobody would restore.
    sudo bash -c "ls -1t '$BACKUP_DIR'/*.dump 2>/dev/null | tail -n +3 | xargs -r rm -f"
  elif grep -qi "does not exist\|no such database" /tmp/dump.log; then
    warn "no existing database yet - first import on this host"
  else
    # Anything else is a real failure and must not be mistaken for "nothing to
    # back up". The first version of this reported a permissions error as
    # "first import on this host", which would have read as success.
    die "backup FAILED - refusing to overwrite an un-backed-up store.
        $(tail -3 /tmp/dump.log)
        Re-run with SKIP_BACKUP=1 only if you accept losing the current graph."
  fi
fi

sudo rm -rf "$IMPORT_DIR"/nodes "$IMPORT_DIR"/edges "$IMPORT_DIR"/headers
sudo mv /tmp/neo4j-stage/nodes /tmp/neo4j-stage/edges "$IMPORT_DIR"/
sudo cp -r "$BUILD/import/headers" "$IMPORT_DIR"/
sudo cp "$BUILD/import/schema.cypher" "$IMPORT_DIR"/
sudo chown -R neo4j:neo4j "$IMPORT_DIR"

step "importing"
# --bad-tolerance=0 is deliberate. Validation already proved every endpoint
# resolves, so a bad row here means the files changed in between - and a
# partial import that silently drops edges is worse than a failed one.
cd "$IMPORT_DIR"
sudo -u neo4j bash -c "
neo4j-admin database import full $DB \
\$(for f in nodes/*.csv; do b=\$(basename \$f .csv); echo --nodes=\$b=$IMPORT_DIR/headers/\$b.header.csv,$IMPORT_DIR/\$f; done) \
\$(for f in edges/*.csv; do b=\$(basename \$f .csv); echo --relationships=\$b=$IMPORT_DIR/headers/\$b.header.csv,$IMPORT_DIR/\$f; done) \
--id-type=string --skip-bad-relationships=false --skip-duplicate-nodes=false \
--bad-tolerance=0 --high-parallel-io=on --overwrite-destination=true"

step "starting neo4j"
sudo systemctl start neo4j
for _ in $(seq 1 60); do curl -s -o /dev/null -m 5 http://localhost:7474/ && break; sleep 5; done

step "constraints and indexes"
# After the import, never before: neo4j-admin does not enforce constraints
# during load, and creating them first costs an index build on every row.
cypher-shell -a bolt://localhost:7687 -u neo4j -p "$NEO4J_PASSWORD" -d "$DB" \
  --non-interactive -f "$IMPORT_DIR/schema.cypher"

cypher-shell -a bolt://localhost:7687 -u neo4j -p "$NEO4J_PASSWORD" -d "$DB" \
  --non-interactive --format plain \
  'MATCH (n) RETURN count(n) AS nodes;'

# Answer-level tests, against the database that is now live.
#
# validate.py proves the build is well-formed, and it passed on a graph where
# "what are the side effects of metformin" returned nothing - the reports hang
# off the salt form, and an empty result is not a structural fault. So this
# asks the questions instead: every label reachable the way a caller would
# reach it, every relationship traversable, twelve real drugs answered across
# nine question types, and each known trap asserted.
#
# It runs after the import because it needs a live database. A failure does
# not roll back automatically - it tells you the graph is serving wrong or
# empty answers, and the dump taken at the start of this script is the way
# back.
step "answer tests"
if NEO4J_URI="bolt://localhost:7687" NEO4J_USER="neo4j" \
   NEO4J_PASSWORD="$NEO4J_PASSWORD" NEO4J_DATABASE="$DB" \
   "$VENV/bin/python" "$REPO/graph/test_answers.py"; then
  ok "imported $BUILD - structure and answers both check out"
else
  echo
  echo "  The import completed, but the graph gives wrong or empty answers."
  echo "  Restore with:"
  echo "    sudo systemctl stop neo4j"
  # Defaulted inline: BACKUP_DIR is assigned inside the backup block, which
  # is skipped on a first import, and an unset path in a recovery
  # instruction is worse than no instruction.
  echo "    sudo -u neo4j neo4j-admin database load $DB --from-path=${BACKUP_DIR:-/var/lib/neo4j/backups} --overwrite-destination=true"
  echo "    sudo systemctl start neo4j"
  exit 1
fi
