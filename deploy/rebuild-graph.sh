#!/usr/bin/env bash
# One rebuild, end to end: build from S3, validate, replace the store, run the
# answer gate.
#
#     NEO4J_PASSWORD='...' bash deploy/rebuild-graph.sh
#     NEO4J_PASSWORD='...' nohup setsid bash deploy/rebuild-graph.sh \
#         > ~/rebuild.log 2>&1 < /dev/null &     # ~40 min, survives logout
#
# This exists because the ad-hoc version of it lived only on the graph host,
# outside the repo, with the password written into the file. It also relied on
# NEO4J_PASSWORD being exported by whoever's interactive shell launched it, so
# running it detached - the way you would run something that takes 40 minutes
# - produced a clean build followed by an import that refused to start.
#
# Airflow does NOT use this script: graph_sync calls build-graph.sh and
# import-graph.sh as separate tasks so a failed build cannot reach the import.
# This is the manual equivalent for a one-off rebuild.
set -o pipefail

cd "$(dirname "$0")/.." || exit 1

if [ -z "${NEO4J_PASSWORD:-}" ]; then
    echo "set NEO4J_PASSWORD - the import cannot run without it, and finding" >&2
    echo "that out 30 minutes from now after a clean build is worse." >&2
    exit 1
fi

echo "=== START $(date -u +%FT%TZ)  $(git log --oneline -1)"

# On a host too small to run the build beside the database, Neo4j is stopped
# for the build as well as the import.
#
# build.py is given --max-mem-gb 6 and Neo4j holds ~5 GB on the 8 GB profile,
# so the two do not fit in 8. Left running, the build does not fail cleanly -
# it drives the box into the state where ports still accept connections and
# nothing can be scheduled, which is what happened on 2026-08-06 and needed a
# console restart to clear. A planned 50-minute outage is a much better
# outcome than an unplanned one of unknown length.
#
# The 16 GB profile is unaffected: the build runs alongside a live database and
# only the import stops it, which is ~5 minutes.
TOTAL_MB=$(free -m | awk '/^Mem:/ {print $2}')
SMALL_HOST=0
if [ "${TOTAL_MB:-0}" -lt 12000 ]; then
    SMALL_HOST=1
    echo "=== ${TOTAL_MB} MB RAM - stopping neo4j for the BUILD as well as the"
    echo "=== import. The graph is down for the whole rebuild, not just ~5 min."
    sudo systemctl stop neo4j 2>/dev/null || true
fi

bash deploy/build-graph.sh --all
build_rc=$?
echo "=== BUILD RC ${build_rc}"

# Bring it back before the import decides anything. import-graph.sh stops the
# database itself, so this is not wasted: it means a FAILED build leaves the
# graph serving the previous store rather than leaving it down.
if [ "${SMALL_HOST}" -eq 1 ]; then
    sudo systemctl start neo4j 2>/dev/null || true
fi

# Nothing is replaced if the build failed. import-graph.sh refuses a directory
# with no .validated marker anyway, but stopping here makes the reason legible
# in the log rather than leaving it to a second refusal further down.
if [ "${build_rc}" -ne 0 ]; then
    echo "=== SKIPPED IMPORT - build failed, the live graph is untouched"
    echo "=== DONE $(date -u +%FT%TZ)"
    exit "${build_rc}"
fi

bash deploy/import-graph.sh
import_rc=$?
echo "=== IMPORT RC ${import_rc}"

# The documents describe the schema, and their tables are generated from it -
# so a schema change updates them silently and the prose goes stale without
# anything saying so. Regenerated here, after the import, because the deck
# reads its counts from the live graph.
#
# Never fails the rebuild: a missing weasyprint or python-pptx on this host is
# not a reason to report a good graph as broken. It says STALE and moves on.
if [ "${import_rc}" -eq 0 ]; then
    bash deploy/refresh-docs.sh || echo "=== DOCS STALE - regenerate and commit"
fi
echo "=== DONE $(date -u +%FT%TZ)"
