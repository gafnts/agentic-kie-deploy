#!/usr/bin/env bash
# Smoke test: S3 ingestion -> EventBridge -> SQS extraction.
# Exits 0 if a message referencing the uploaded key arrives within 20s.
# Requires: terraform state already initialized for the target env.

set -euo pipefail

for cmd in aws jq terraform; do
  command -v "$cmd" >/dev/null || { echo "missing dependency: $cmd" >&2; exit 1; }
done

BUCKET=$(terraform -chdir=infra output -raw ingestion_bucket_name)
QURL=$(terraform -chdir=infra output -raw extraction_queue_url)

RUN_ID="smoke-$(date +%s)-$RANDOM"
KEY="smoke/${RUN_ID}.txt"
TMP=$(mktemp)
printf '%s\n' "$RUN_ID" > "$TMP"

cleanup() {
  local code=$?
  aws s3 rm "s3://$BUCKET/$KEY" >/dev/null 2>&1 || true
  rm -f "$TMP"
  exit "$code"
}
trap cleanup EXIT

echo "uploading s3://$BUCKET/$KEY"
aws s3 cp "$TMP" "s3://$BUCKET/$KEY" >/dev/null

echo "polling $QURL for a message referencing $KEY (up to 30s)"
DEADLINE=$(( $(date +%s) + 30 ))
HANDLE=""

while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  RESP=$(aws sqs receive-message \
    --queue-url "$QURL" \
    --wait-time-seconds 5 \
    --max-number-of-messages 10)

  COUNT=$(printf '%s' "$RESP" | jq -r '.Messages // [] | length')
  [ "$COUNT" -eq 0 ] && continue

  for i in $(seq 0 $((COUNT - 1))); do
    H=$(printf '%s' "$RESP" | jq -r ".Messages[$i].ReceiptHandle")
    B=$(printf '%s' "$RESP" | jq -r ".Messages[$i].Body")
    if printf '%s' "$B" | grep -q "$KEY"; then
      HANDLE="$H"
    else
      # Not ours — return to queue immediately so other consumers (or its owner) see it.
      aws sqs change-message-visibility \
        --queue-url "$QURL" \
        --receipt-handle "$H" \
        --visibility-timeout 0 >/dev/null 2>&1 || true
    fi
  done

  [ -n "$HANDLE" ] && break
done

if [ -z "$HANDLE" ]; then
  echo "FAIL: no message referencing $KEY received within 30s" >&2
  exit 1
fi

aws sqs delete-message --queue-url "$QURL" --receipt-handle "$HANDLE" >/dev/null
echo "OK: $KEY arrived in extraction queue"
