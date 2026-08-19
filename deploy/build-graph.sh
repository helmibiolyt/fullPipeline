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

# On a host too small to run the build beside the database, stop Neo4j first.
#
# This lives HERE, not in rebuild-graph.sh, and the distinction cost a failed
# production run to learn. graph_sync calls build-graph.sh and import-graph.sh
# as separate tasks - it never calls rebuild-graph.sh - so a guard placed there
# protects a manual rebuild and nothing else. The first unattended rebuild on
# the 8 GB host was killed exactly this way:
#
#     11:51:51  Killed process 7800 (java)    5.6 GB   <- Neo4j
#     11:56:58  Killed process 8395 (python)  4.0 GB   <- the build
#     SSH operator error: exit status = 137
#
# Both died. The kernel does not choose the victim you would.
#
# Restarted by a trap rather than at the end, so a build that fails or is
# interrupted still leaves the database serving the previous store instead of
# leaving the host with no graph at all.
TOTAL_MB=$(free -m | awk '/^Mem:/ {print $2}')
if [ "${TOTAL_MB:-0}" -lt 12000 ] && systemctl is-active --quiet neo4j 2>/dev/null; then
  warn "${TOTAL_MB} MB RAM - stopping neo4j for the build (it does not fit"
  warn "alongside a ${MEM} GB build at this size). The graph is DOWN until the"
  warn "build finishes, not just for the import."
  sudo systemctl stop neo4j 2>/dev/null || true
  trap 'sudo systemctl start neo4j 2>/dev/null || true' EXIT
fi

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
