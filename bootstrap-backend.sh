#!/usr/bin/env bash
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
PROJECT="agentic-kie"
ENVS=("local" "dev" "prod")

SUFFIX=$(echo -n "${PROJECT}" | openssl dgst -sha256 | awk '{print $2}' | cut -c1-8)
BUCKET="${PROJECT}-tfstate-${SUFFIX}"

write_backend() {
  local file="$1" key="$2"
  mkdir -p "$(dirname "$file")"
  echo "Writing ${file}"
  cat > "$file" <<EOF
bucket       = "${BUCKET}"
key          = "${key}"
region       = "${AWS_REGION}"
use_lockfile = true
encrypt      = true
EOF
}

for ENV in "${ENVS[@]}"; do
  write_backend "./infra/envs/${ENV}.backend.tfbackend"          "service/${ENV}/terraform.tfstate"
  write_backend "./infra/registry/envs/${ENV}.backend.tfbackend" "service/${ENV}/registry.tfstate"
done

write_backend "./infra/iam/backend.tfbackend" "service/iam/terraform.tfstate"
