# ADR-0010: Uploader Module (Presigner, API Gateway, IAM Authentication)

## Status

Proposed (2026-05-24)

## Context

ADR-0001 fixed the entry point shape—a presigner Lambda behind API Gateway that returns a short-lived pre-signed S3 PUT URL—but listed the uploader module as planned. The rest of the pipeline (bucket, queue, table, extractor, alarms) is implemented; this ADR settles what was left.

ADR-0006 already fixed the document ID contract: the presigner mints a UUIDv7, returns it to the caller alongside the upload URL, and embeds it in the S3 object key. ADR-0009 settled the secret-management and observability patterns the presigner will inherit when it lands. What remains is the auth surface, the API Gateway flavor, the module's IAM posture, and an explicit "why a presigner exists at all"—the kind of question a future reader will ask if the architecture only ever has one caller.

The integration model is server-to-server, internal, AWS-native. The caller is another AWS service in the same account—not a browser, not a third-party SaaS, not a CLI in someone's terminal. That narrows the auth options to three:

| Option | Mechanism | Cost |
|---|---|---|
| API key | API Gateway usage plan + key | Secret in caller config; rotation story; leakage surface; no AWS-native identity in CloudTrail |
| OAuth / JWT | Cognito or external IdP + token validation | Issuer to provision; token lifecycle (issue, refresh, revoke); custom authorizer Lambda or JWT authorizer config |
| IAM SigV4 | API Gateway `AWS_IAM` authorizer | None—caller's existing IAM role signs the request; identity in CloudTrail without instrumentation |

The caller already has an IAM role. Every AWS SDK signs SigV4 by default. There is no secret to rotate, no token to refresh, no issuer to operate.

## Decision

A new module at `infra/modules/uploader/`, wired from `infra/main.tf` next to the existing modules. The module owns the HTTP API, the IAM-authorized route, the presigner Lambda, its execution role, and the log group.

### API surface

API Gateway HTTP API (`aws_apigatewayv2_api` with `protocol_type = "HTTP"`), one route:

```
POST /uploads      Authorization: AWS_IAM
```

Request body: empty (or future-extensible JSON for hints like `content_type`).
Response body: `{ "document_id": "...", "upload_url": "...", "expires_at": "..." }` per ADR-0006.

HTTP API over REST API: cheaper per request, lower latency, and the feature delta (no usage plans, no request validators) does not bite this integration. The `AWS_IAM` authorizer is a first-class authorizer type on HTTP APIs, not a custom Lambda.

### Auth

`AWS_IAM` authorizer on the route. The caller signs the request with SigV4 using its own IAM role; API Gateway validates the signature against the caller's identity and authorizes via `execute-api:Invoke` on the route ARN.

The caller's role needs:

```hcl
{
  Effect   = "Allow"
  Action   = "execute-api:Invoke"
  Resource = "arn:aws:execute-api:${region}:${account}:${api_id}/*/POST/uploads"
}
```

This grant is the caller's responsibility, not the uploader module's. The module outputs the API ARN so the caller can scope its grant precisely; nothing in this module assumes how the caller's IAM is structured.

#### Two perimeters, not one

The route is open to any in-account principal that holds `execute-api:Invoke` on the ARN. That is intentional and only safe in composition with the second perimeter the architecture provides: the extractor's document-type coupling. The full access-control story is:

1. **Account IAM** is the outer perimeter. Cross-account principals cannot sign a valid SigV4 request against this route; the boundary holds without any per-caller configuration on our side.
2. **Document-type coupling** is the inner perimeter. An in-account principal that successfully uploads a document of the wrong type produces useless output—extraction fails or returns garbage, the result object lands in the wrong shape, the downstream consumer discards it. There is nothing to gain from "sneaking in" because the pipeline only produces value for the one document type its extractor was built for.

The single-tenant deployment contract (ADR-0013)—one caller, one document type per instance—is what makes this composition coherent. If a future environment cannot rely on the account boundary (multiple unrelated teams, a regulated workload), the available hardening lever is a Lambda authorizer that checks the caller's principal ARN against an allowlist; see the alternatives section.

### Presigner Lambda

| Lever | Value | Reasoning |
|---|---|---|
| Runtime | Python 3.13, zip (not container image) | The presigner has no heavy dependencies—`boto3` and `uuid7`. The container-image cold-start tax (ADR-0009) is unjustified here |
| Timeout | 5 s | The function does one `generate_presigned_url` call; 5 s is generous |
| Memory | 256 MB | `boto3` import plus one signing call. Larger memory does not reduce wall-clock here |
| Architecture | `arm64` | Same cost reasoning as ADR-0009; the build is mechanical (a zip on the right architecture) |
| Concurrency | No reserved or maximum | The presign call is cheap and the upstream is API Gateway, which already rate-limits. No fan-out to bound |

The function:

1. Mints a UUIDv7.
2. Composes the key: `uploads/{yyyy}/{mm}/{dd}/{document_id}` per ADR-0006.
3. Calls `s3.generate_presigned_url("put_object", Params={Bucket, Key}, ExpiresIn=600)`.
4. Returns `{document_id, upload_url, expires_at}`.

Ten-minute URL lifetime—long enough for retries and slow networks; short enough that a leaked URL is useless within an hour.

### IAM execution role

Inline policy, two statements:

| Statement | Action | Resource |
|---|---|---|
| `IngestionPut` | `s3:PutObject` | `${var.ingestion_bucket_arn}/uploads/*` |
| `LogsWrite` | `logs:CreateLogStream`, `logs:PutLogEvents` | `${aws_cloudwatch_log_group.presigner.arn}:*` |

The `s3:PutObject` grant is what makes the *signed URL* valid—the presigner does not upload anything itself, but the URL inherits the signer's permissions, so the role must have the action the URL grants. Scoped to the `uploads/` prefix so a misuse cannot sign URLs for the analytics partition that ADR-0012 introduces.

The role name is `agentic-kie-deploy-${env}-uploader-exec`, tagged `Environment = ${env}` so the `iam/` stack's `DenyTouchingOtherEnvs` guard holds (same pattern as ADR-0009).

### Why a presigner exists at all

The caller is an AWS service with its own IAM role; in principle it could PUT directly to S3 with no presigner in the loop. The presigner exists because three pieces of the pipeline depend on it:

1. **The document ID is server-minted (ADR-0006).** The caller cannot synthesize a UUIDv7 with the project's collision and observability guarantees. The presigner is where the ID is created and where the caller first learns it.
2. **The object key is signature-pinned.** The presigned URL fixes the exact `uploads/{yyyy}/{mm}/{dd}/{document_id}` key. The caller cannot upload to a different key under the same URL. This is the property ADR-0006 leans on to keep the ID server-controlled rather than caller-asserted.
3. **The caller needs the result address before the upload (ADR-0011).** Because results land at `extractions/{yyyy}/{mm}/{dd}/{document_id}.json`, the caller needs `document_id` *before* the result exists to install an S3 event subscription on the right key. The presign response is the only place that ID is exposed.

Removing the presigner means giving up server-controlled IDs, signature-pinned keys, and the address-before-existence property.

### Module wiring

```hcl
module "uploader" {
  source               = "./modules/uploader"
  function_name        = "${var.project_name}-${var.environment}-presigner"
  api_name             = "${var.project_name}-${var.environment}-uploader"
  ingestion_bucket_arn = module.bucket.bucket_arn
  ingestion_bucket_name = module.bucket.bucket_name
  url_ttl_seconds      = 600
  log_retention_days   = var.environment == "prod" ? 30 : 14
  environment          = var.environment
  alarm_topic_arn      = module.alarms.topic_arn
}
```

Outputs: `api_endpoint`, `api_arn`, `route_arn`, `function_name`. The caller's IAM grant is constructed from `route_arn`.

### Observability

Same posture as the extractor (ADR-0009): a module-owned log group at `/aws/lambda/${function_name}`, structured JSON logs, retention 14d in `local`/`staging`, 30d in `prod`. Function-level `Errors` and `Throttles` alarms on the same SNS topic as the rest of the pipeline (the `alarms` module). No LangSmith—there is no LLM call.

CloudWatch logs include the resolved `document_id`, `client_principal` (from the API Gateway request context, which surfaces the caller's IAM principal), and `request_id` on every log line. The `client_principal` is the auditable record of who asked for which upload URL.

## Consequences

Positive:

- No secrets to rotate, no token lifecycle to operate. The caller signs with the role it already has.
- Caller identity lands in CloudTrail by default—`execute-api:Invoke` is logged with the principal ARN.
- The presigner's IAM role is narrow (one `s3:PutObject` on one prefix). The blast radius of a Lambda compromise is bounded to signing URLs for the ingestion bucket.
- HTTP API is the cheaper API Gateway flavor; cost is a non-line-item at portfolio scale.
- Caller-side IAM is the outer audit boundary for "who can upload." Adding or removing a caller is one IAM policy change on their side, nothing in our infrastructure. The inner boundary—document-type coupling in the extractor (ADR-0009, ADR-0013)—means an in-account principal that grants itself the action still cannot produce useful pipeline output without matching the deployed instance's document type.

Negative:

- Casual `curl` testing requires SigV4 signing (`awscurl` or the AWS CLI's `--invoke-api`). Ad-hoc probes are slightly less convenient than against a public endpoint.
- HTTP API has no usage plans, so rate limiting is global per-route (the default 10k req/s account limit), not per-caller. Acceptable at portfolio scale; if rate-limiting per caller becomes a need, the answer is a small Lambda authorizer that reads a per-principal quota from DynamoDB, not REST API usage plans.

Neutral:

- The presigner is a zip Lambda while the extractor is a container image. The asymmetry is intentional—packaging follows dependency weight, not consistency for its own sake.
- The URL TTL (600 s) is a single variable; tightening it later is a tfvar change with no other coordination.

## Alternatives considered

- **API keys via API Gateway usage plans.** Rejected—introduces a secret-rotation story and a parallel auth surface that the caller does not need (it already has an IAM role).
- **OAuth / JWT via Cognito.** Rejected—needs an issuer, a token-validation step on every request, and a refresh flow. Heavy for one internal AWS-native caller.
- **No presigner; caller PUTs directly to S3 with its own credentials.** Rejected—gives up server-minted IDs (ADR-0006), signature-pinned keys, and the "caller learns the result address before the result exists" property that ADR-0011 depends on. The presigner is load-bearing for those reasons, not for auth.
- **REST API instead of HTTP API.** Rejected—REST API is more expensive per request and the features it adds (usage plans, request validators, edge-optimized endpoints) are not needed here.
- **Container image for the presigner.** Rejected—the function imports `boto3` and signs one URL. Container cold starts (3–10 s per ADR-0009) are unjustified for that workload; zip cold starts are sub-second.
- **Generate UUIDv4 instead of UUIDv7.** Rejected—settled in ADR-0006. UUIDv7's time-ordering is the reason.
- **Lambda authorizer with per-principal quotas.** Deferred—rate-limiting per caller is not a requirement today and adding it before it is needed would mean carrying an authorizer Lambda, a quota table, and a cache for zero current value. The shape of the change when needed is recorded above.
- **Lambda authorizer for principal allowlisting.** Deferred—the account boundary plus document-type coupling (ADR-0013) is the assumed perimeter at this scale; tightening to a specific principal ARN is a Lambda authorizer that reads `request_context.identity.userArn` and matches it against an env-provided allowlist. Same mechanism as the per-principal-quotas authorizer above; the two would collapse into one authorizer if both became needed. Out of scope until the account boundary stops being sufficient (multiple unrelated teams in one account, a regulated workload).
- **Synchronous extraction endpoint (return results from the same call).** Rejected—settled in ADR-0001 on payload-size and timeout grounds; revisiting here would undo that decision.
