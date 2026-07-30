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
  BACKUP_DIR="${BACKUP_DIR:-$HOME/neo4j-backups}"
  mkdir -p "$BACKUP_DIR"; sudo chown neo4j:neo4j "$BACKUP_DIR"
  step "backing up the current database first"
  if sudo -u neo4j neo4j-admin database dump "$DB" --to-path="$BACKUP_DIR"        --overwrite-destination=true 2>&1 | tail -2; then
    ok "dump in $BACKUP_DIR ($(du -sh "$BACKUP_DIR" 2>/dev/null | cut -f1))"
    # Keep two. Older ones describe a graph nobody would restore.
    ls -1t "$BACKUP_DIR"/*.dump 2>/dev/null | tail -n +3 | xargs -r sudo rm -f
  else
    warn "no existing database to dump - first import on this host"
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
ok "imported $BUILD"
