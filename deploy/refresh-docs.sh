#!/usr/bin/env bash
# Regenerate everything that describes the graph to a human.
#
#     bash deploy/refresh-docs.sh
#
# Run this AFTER an import finishes, never during one. The deck reads its
# counts from the live graph and falls back to a dated snapshot when Neo4j is
# unreachable - a deck built mid-import ships stale numbers that look current,
# which is worse than failing.
#
# Why a script rather than "remember to do it": the tables in these documents
# are generated from emit.py and sources.py, so a schema change updates them on
# its own and the files look fresh. The PROSE does not. After the schema work
# on 2026-08-03 every table was correct while the text still described STUDIES
# as one name-matched tier and listed a match_method set five values short.
# Nothing about the output said so.
set -o pipefail

cd "$(dirname "$0")/.." || exit 1

rc=0
run() {
    echo "==> $1"
    if python "$1"; then
        return 0
    fi
    echo "  ! $1 failed" >&2
    rc=1
}

run graph/make_tech_doc.py
run graph/make_data_doc.py

# The deck needs python-pptx, which the graph host has no reason to carry.
# Skipping is fine and saying so is the point; failing the whole script for a
# missing optional dependency would train people to ignore its exit code.
if python -c "import pptx" 2>/dev/null; then
    run presentation/make_deck.py
else
    echo "==> presentation/make_deck.py  SKIPPED (python-pptx not installed)"
fi

echo
if [ "$rc" -eq 0 ]; then
    echo "docs regenerated - commit them with the change that caused them"
else
    echo "one or more generators failed; the published docs are now STALE" >&2
fi
exit "$rc"
