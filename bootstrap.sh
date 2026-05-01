#!/usr/bin/env bash
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
PROJECT="agentic-kie"
ENVS=("local" "dev" "prod")

_SUFFIX=$(echo -n "${PROJECT}" | openssl dgst -sha256 | awk '{print $2}' | cut -c1-8)
BUCKET="${PROJECT}-tfstate-${_SUFFIX}"

# 1. Create the state bucket (idempotent, shared across envs)
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

# 2. Versioning
echo "Enabling versioning"
aws s3api put-bucket-versioning \
  --bucket "${BUCKET}" \
  --versioning-configuration Status=Enabled

# 3. Block public access
echo "Blocking public access"
aws s3api put-public-access-block \
  --bucket "${BUCKET}" \
  --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

# 4. Default encryption
echo "Enabling encryption"
aws s3api put-bucket-encryption \
  --bucket "${BUCKET}" \
  --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

# 5. Write one backend file per environment
mkdir -p ./infra/envs
for ENV in "${ENVS[@]}"; do
  BACKEND_FILE="./infra/envs/${ENV}.backend.tfbackend"
  echo "Writing ${BACKEND_FILE}"
  cat > "${BACKEND_FILE}" <<EOF
bucket       = "${BUCKET}"
key          = "service/${ENV}/terraform.tfstate"
region       = "${AWS_REGION}"
use_lockfile = true
encrypt      = true
EOF
done

echo ""
echo "Bootstrap complete."
echo "Backend files written for: ${ENVS[*]}"
echo ""
echo "Next:"
echo "  make init ENV=local   # then dev, then prod"
