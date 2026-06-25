#!/usr/bin/env bash
set -euo pipefail

# Out-of-band teardown: the mirror image of bootstrap.sh.
#
# Deletes the two things that live OUTSIDE Terraform's lifecycle — the six
# Secrets Manager secrets and the shared, versioned state bucket. This is the
# LAST step of a full teardown. Run it only after every Terraform stack is gone:
#
#   make destroy-env ENV=local
#   make destroy-env ENV=staging
#   make destroy-env ENV=prod I_KNOW=1   # after clearing prod's guards by hand
#   make iam-destroy I_KNOW=1
#
# Deleting the state bucket orphans anything Terraform still tracks (the
# resources keep existing in AWS but Terraform can no longer see them), which is
# why the stacks come first.
#
# Run with ADMIN/DEFAULT credentials, not the scoped deploy role: the deploy
# roles can only touch their own state prefix and cannot delete the bucket. The
# repo's .envrc sets AWS_PROFILE=agentic-kie-local, so override it, e.g.
#   AWS_PROFILE=default bash teardown.sh
#
# Secrets are scheduled for deletion with the default recovery window so a
# mistake stays recoverable. Set FORCE=1 to delete them immediately and
# irreversibly. The state bucket deletion is always irreversible.

AWS_REGION="${AWS_REGION:-us-east-1}"
PROJECT="agentic-kie"
SECRET_PREFIX="agentic-kie-deploy"
ENVS=("local" "staging" "prod")
FORCE="${FORCE:-0}"

SUFFIX=$(echo -n "${PROJECT}" | openssl dgst -sha256 | awk '{print $2}' | cut -c1-8)
BUCKET="${PROJECT}-tfstate-${SUFFIX}"

SECRETS=()
for ENV in "${ENVS[@]}"; do
  SECRETS+=("${SECRET_PREFIX}/${ENV}/llm-provider" "${SECRET_PREFIX}/${ENV}/langsmith")
done

echo ""
echo "This permanently deletes the out-of-band resources for ${PROJECT}:"
echo ""
echo "  State bucket (and ALL Terraform state it holds):"
echo "    ${BUCKET}"
echo ""
echo "  Secrets Manager secrets:"
for s in "${SECRETS[@]}"; do echo "    ${s}"; done
echo ""
if [ "${FORCE}" = "1" ]; then
  echo "  FORCE=1: secrets deleted IMMEDIATELY, no recovery window."
else
  echo "  Secrets scheduled for deletion with the default recovery window"
  echo "  (set FORCE=1 to delete immediately and irreversibly)."
fi
echo ""
echo "Run this ONLY after every Terraform stack is destroyed (destroy-env for"
echo "each env, then iam-destroy). Deleting the bucket orphans anything"
echo "Terraform still tracks."
echo ""

read -r -p "Type the bucket name to confirm: " CONFIRM
if [ "${CONFIRM}" != "${BUCKET}" ]; then
  echo "Confirmation did not match. Aborting; nothing was deleted."
  exit 1
fi

# 1. Delete the secrets
echo ""
echo "Deleting secrets"
for s in "${SECRETS[@]}"; do
  if ! aws secretsmanager describe-secret --secret-id "${s}" --region "${AWS_REGION}" >/dev/null 2>&1; then
    echo "  not found, skipping: ${s}"
    continue
  fi
  if [ "${FORCE}" = "1" ]; then
    aws secretsmanager delete-secret --secret-id "${s}" --region "${AWS_REGION}" \
      --force-delete-without-recovery >/dev/null
    echo "  deleted (immediate): ${s}"
  else
    aws secretsmanager delete-secret --secret-id "${s}" --region "${AWS_REGION}" >/dev/null
    echo "  scheduled for deletion: ${s}"
  fi
done

# 2. Empty (all versions + delete markers) and delete the state bucket
echo ""
if ! aws s3api head-bucket --bucket "${BUCKET}" 2>/dev/null; then
  echo "Bucket ${BUCKET} not found, skipping"
  echo ""
  echo "Teardown complete."
  exit 0
fi

# Versioned bucket: an object isn't gone until every version and delete marker
# is removed. Loop in case there are more than one page (1000) of them.
purge_versions() {
  local query="$1"
  while :; do
    local lines
    lines=$(aws s3api list-object-versions --bucket "${BUCKET}" --max-keys 1000 \
      --region "${AWS_REGION}" --query "${query}" --output text 2>/dev/null || true)
    [ -z "${lines}" ] && break
    printf '%s\n' "${lines}" | while IFS=$'\t' read -r KEY VERSION; do
      [ -z "${KEY}" ] && continue
      aws s3api delete-object --bucket "${BUCKET}" --region "${AWS_REGION}" \
        --key "${KEY}" --version-id "${VERSION}" >/dev/null
    done
  done
}

echo "Emptying ${BUCKET} (all object versions and delete markers)"
purge_versions 'Versions[].[Key,VersionId]'
purge_versions 'DeleteMarkers[].[Key,VersionId]'

echo "Deleting bucket ${BUCKET}"
aws s3api delete-bucket --bucket "${BUCKET}" --region "${AWS_REGION}"

echo ""
echo "Teardown complete."
