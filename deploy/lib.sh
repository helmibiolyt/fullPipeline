#!/usr/bin/env bash
# Shared helpers for the provisioning scripts. Sourced, not run.
#
# Everything here is idempotent: running a provision script twice must be safe,
# because the realistic use is not "build a fresh VM" but "the VM drifted, put
# it back". A script you are afraid to re-run is a script nobody runs.

set -euo pipefail

BLUE=$'\033[34m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; OFF=$'\033[0m'
step() { echo "${BLUE}==>${OFF} $*"; }
ok()   { echo "${GREEN}  ok${OFF}   $*"; }
warn() { echo "${YELLOW}  warn${OFF} $*"; }
die()  { echo "${RED}  fail${OFF} $*" >&2; exit 1; }

# Repo root, however the script was invoked.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

apt_install() {
  local missing=()
  for p in "$@"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
  if [ ${#missing[@]} -eq 0 ]; then ok "packages already present: $*"; return; fi
  step "installing: ${missing[*]}"
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
}

# Ubuntu ships python3 without the venv module, so testing for python3 passes
# on a host where `python3 -m venv` still fails. Test for what is needed.
make_venv() {
  local path="$1" reqs="$2"
  python3 -c "import ensurepip" 2>/dev/null || apt_install python3-venv
  [ -x "$path/bin/python" ] || { step "creating venv $path"; python3 -m venv "$path"; }
  "$path/bin/pip" install -q --upgrade pip
  [ -f "$reqs" ] && "$path/bin/pip" install -q -r "$reqs"
  ok "venv $path ($("$path/bin/python" -V))"
}

# Credentials are never in the repo. This only reports where they are expected
# and whether boto3 can actually resolve them, because "the build failed after
# twenty minutes" is a bad way to discover a missing key.
check_aws() {
  local py="$1"
  if "$py" -c "import boto3,sys; boto3.client('sts').get_caller_identity()" 2>/dev/null; then
    ok "AWS credentials resolve"
  else
    warn "no AWS credentials. Provide ONE of:"
    echo "        - an instance role (preferred on AWS; not available on Azure)"
    echo "        - aws configure"
    echo "        - AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY in the environment"
    echo "        - $REPO/automation/.env   (gitignored, copy it here yourself)"
  fi
}
