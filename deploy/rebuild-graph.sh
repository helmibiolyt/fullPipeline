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

# The small-host guard (stop Neo4j for the build) lives in build-graph.sh,
# not here, because graph_sync calls that script directly and never calls
# this one. Putting it here protected a manual rebuild and left the
# unattended one to be OOM-killed.
bash deploy/build-graph.sh --all
build_rc=$?
echo "=== BUILD RC ${build_rc}"


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
