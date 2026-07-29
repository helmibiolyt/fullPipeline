#!/usr/bin/env bash
# Stand up a graph build host from nothing.
#
#     scp -r graph/ ubuntu@<host>:~/          # or git clone
#     ssh ubuntu@<host> 'bash ~/graph/bootstrap.sh'
#
# The build is deliberately cheap to move: it reads the lake from S3 and writes
# CSVs to local disk, holding no state between runs and depending on nothing on
# the host but Python and boto3. Replacing the graph EC2 means running this on
# the new one - there is nothing to migrate, because the output is derived and
# the input lives in S3.
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null; then
  sudo apt-get update -qq && sudo apt-get install -y -qq python3 python3-venv
fi

python3 -m venv ~/graphenv
~/graphenv/bin/pip install -q --upgrade pip
~/graphenv/bin/pip install -q -r requirements.txt

echo
echo "credentials"
if ~/graphenv/bin/python -c "
import boto3, sys
try:
    boto3.client('sts').get_caller_identity()
except Exception as e:
    sys.exit(1)
" 2>/dev/null; then
  echo "  ok - resolved from the environment (instance role or ~/.aws)"
else
  cat <<'EOF'
  none found. Give the host read access to the bucket by ONE of:
    - an IAM instance role with s3:GetObject + s3:ListBucket   (preferred:
      nothing to rotate, nothing to leak, and it survives a box replacement)
    - aws configure
    - AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY in the environment
    - automation/.env alongside this directory
EOF
fi

cat <<EOF

ready. next:
  ~/graphenv/bin/python build.py --slice atorvastatin,erenumab,pembrolizumab --out build
  ~/graphenv/bin/python validate.py --dir build
  ~/graphenv/bin/python build.py --all --out build --max-mem-gb 12
EOF
