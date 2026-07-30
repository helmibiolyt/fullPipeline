#!/usr/bin/env bash
# Build the graph from S3, then validate it. Does not touch Neo4j.
#
#     bash deploy/build-graph.sh              # full build
#     bash deploy/build-graph.sh --slice atorvastatin,erenumab,pembrolizumab
#
# Validation is not optional here: import-graph.sh refuses to run against a
# build that has no passing validation next to it. That gate is the whole
# reason the graph is built to files first.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

VENV="${GRAPH_VENV:-$HOME/graphenv}"
RUNS="${GRAPH_RUNS:-$HOME/graph-runs}"
# Ceiling, not a tuning knob: Neo4j holds 8G on this box and without a limit
# the kernel picks the OOM victim - it picked badly enough to reboot the host
# once already.
MEM="${GRAPH_MAX_MEM_GB:-6}"
OUT="$RUNS/$(date -u +%Y%m%dT%H%M%SZ)"

MODE=(--all)
[ $# -gt 0 ] && MODE=("$@")

step "building ${MODE[*]} -> $OUT"
cd "$REPO/graph"
"$VENV/bin/python" -u build.py "${MODE[@]}" --out "$OUT" --max-mem-gb "$MEM"

step "validating"
if "$VENV/bin/python" validate.py --dir "$OUT" --max-mem-gb 4; then
  touch "$OUT/.validated"
  ok "validation passed - $OUT is importable"
else
  die "validation failed. $OUT is NOT importable; read the failures above."
fi

step "retention"
# Two kept, not one: the previous build is what you re-import from when a new
# one validates but turns out wrong for a reason validate.py cannot see.
cd "$RUNS"
ls -1dt */ 2>/dev/null | sed 's#/##' | tail -n +3 | xargs -r rm -rf
ok "kept: $(ls -1dt */ 2>/dev/null | tr -d '/' | tr '\n' ' ')"
df -h "$RUNS" | tail -1
