#!/usr/bin/env bash
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
PROJECT="agentic-kie"
_SUFFIX=$(echo -n "${PROJECT}" | openssl dgst -sha256 | awk '{print $2}' | cut -c1-8)
BUCKET="${PROJECT}-tfstate-${_SUFFIX}"

# 1. Create the state bucket (idempotent)
echo "Creating S3 bucket: ${BUCKET}"
if aws s3api head-bucket --bucket "${BUCKET}" 2>/dev/null; then
  echo "  bucket already exists, skipping"
else
  if [ "${AWS_REGION}" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "${BUCKET}" --region "${AWS_REGION}"
  else
    aws s3api create-bucket \
      --bucket "${BUCKET}" \
      --region "${AWS_REGION}" \
      --create-bucket-configuration LocationConstraint="${AWS_REGION}"
  fi
fi

# 2. Enable versioning (recovery from corrupted state)
echo "Enabling versioning"
aws s3api put-bucket-versioning \
  --bucket "${BUCKET}" \
  --versioning-configuration Status=Enabled

# 3. Block all public access
echo "Blocking public access"
aws s3api put-public-access-block \
  --bucket "${BUCKET}" \
  --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

# 4. Enable default encryption
echo "Enabling encryption"
aws s3api put-bucket-encryption \
  --bucket "${BUCKET}" \
  --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

echo ""
echo "Bootstrap complete."
echo "Writing infra/backend.tfbackend."
cat > ./infra/backend.tfbackend <<EOF
bucket       = "${BUCKET}"
key          = "service/terraform.tfstate"
region       = "${AWS_REGION}"
use_lockfile = true
encrypt      = true
EOF

echo ""
echo "Done."
echo "You now may run: terraform -chdir=infra init -backend-config=backend.tfbackend"
