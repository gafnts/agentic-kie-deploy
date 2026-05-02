#!/usr/bin/env bash
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
PROJECT="agentic-kie"
ENVS=("local" "dev" "prod")

_SUFFIX=$(echo -n "${PROJECT}" | openssl dgst -sha256 | awk '{print $2}' | cut -c1-8)
BUCKET="${PROJECT}-tfstate-${_SUFFIX}"

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

mkdir -p ./infra/iam
cat > ./infra/iam/backend.tfbackend <<EOF
bucket       = "${BUCKET}"
key          = "service/iam/terraform.tfstate"
region       = "${AWS_REGION}"
use_lockfile = true
encrypt      = true
EOF
