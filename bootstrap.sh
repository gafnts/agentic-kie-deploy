#!/usr/bin/env bash
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
PROJECT="agentic-kie"

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

# 5. Write backend files for all environments
echo ""
echo "Writing backend files"
bash bootstrap-backend.sh

# 6. Write iam.tfvars from caller identity (idempotent)
IAM_TFVARS="./infra/iam/iam.tfvars"
echo ""
if [ -f "${IAM_TFVARS}" ]; then
  echo "  ${IAM_TFVARS} exists, skipping"
else
  PRINCIPAL_ARN=$(aws sts get-caller-identity --query Arn --output text)
  echo "Writing ${IAM_TFVARS} (principal: ${PRINCIPAL_ARN})"
  cat > "${IAM_TFVARS}" <<EOF
local_principal_arn = "${PRINCIPAL_ARN}"
state_bucket_name   = "${BUCKET}"
EOF
fi

echo ""
echo "Bootstrap complete."
echo ""
echo "Next:"
echo "  make iam-init && make iam-apply   # one-time: create deploy roles"
echo "  make init                         # initialize local"
echo "  make plan && make apply           # apply local infra"
echo ""
echo "Dev and prod are initialized and applied by CI on push."
