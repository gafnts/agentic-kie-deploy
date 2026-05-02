<h1 align="center">Agentic KIE Deployment</h1>
<p align="center">
  <strong>Serverless, event-driven AWS infrastructure for asynchronous document key-information extraction.</strong>
</p>
<p align="center">
<a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License"></a>
</p>

---

<p align="center">A client uploads a document to S3 and receives structured fields back — names, dates, amounts, line items — without waiting on the LLM call or managing extraction infrastructure. The entire pipeline is serverless, event-driven, and provisioned with Terraform on AWS.</p>

## Contents

- [Architecture](#architecture)
- [Modules](#modules)
  - [Storage](#storage)
- [Infrastructure](#infrastructure)
- [Environments & delivery](#environments--delivery)
- [Getting started](#getting-started)
- [Contributing](#contributing)
- [Architecture decisions](docs/adr/README.md)

---

## Architecture

The pipeline is fully asynchronous. A client calls a small presigner Lambda behind an API Gateway HTTP endpoint, which returns a short-lived pre-signed S3 PUT URL. The client uploads the document directly to S3, bypassing API Gateway payload limits entirely. The bucket emits an `Object Created` event to EventBridge, which routes it to an SQS queue with a dead-letter queue and redrive policy for resilience. SQS then triggers the extractor Lambda, packaged as a container image from ECR to accommodate heavier ML and LLM dependencies. The extractor runs the [`agentic-kie`](https://github.com/gafnts/agentic-kie) library against the document and writes the resulting structured record to a DynamoDB table keyed by document ID.

![architecture](./docs/architecture.png)

| Component | Service | Role |
|---|---|---|
| Presigner | Lambda + API Gateway | Issues short-lived pre-signed PUT URLs to clients |
| Ingestion bucket | S3 | Receives uploads directly from clients, emits Object Created events |
| Event router | EventBridge | Routes bucket events to the extraction queue |
| Queue | SQS + DLQ | Buffers events, retries on failure, isolates bad messages |
| Extractor | Lambda (container image) | Runs the agentic LLM extraction loop |
| Store | DynamoDB | Holds structured results, keyed by document ID |

---

## Modules

The infrastructure is organized as small, per-concern Terraform modules wired together at the root in [infra/main.tf](infra/main.tf).

| Module | Path | Status |
|---|---|---|
| `storage` | [infra/modules/storage/](infra/modules/storage/) | Implemented |
| `queue` | [infra/modules/queue/](infra/modules/queue/) | Planned |
| `table` | [infra/modules/table/](infra/modules/table/) | Planned |
| `registry` | [infra/modules/registry/](infra/modules/registry/) | Planned |
| `extractor` | [infra/modules/extractor/](infra/modules/extractor/) | Planned |
| `uploader` | [infra/modules/uploader/](infra/modules/uploader/) | Planned |

### Storage

The ingestion bucket is the entry point of the pipeline. Clients upload documents directly via pre-signed PUT URLs, and the bucket forwards `Object Created` events to EventBridge for downstream routing. The bucket is locked down through four orthogonal hardening layers:

| Layer | Mechanism | What it closes |
|---|---|---|
| Public Access Block | All four block flags enabled | Prevents ACLs or policies from ever making objects public |
| Ownership controls | `BucketOwnerEnforced` | Disables ACLs entirely; every object is owned by the bucket account regardless of uploader |
| TLS-only policy | Deny on `aws:SecureTransport = false` | Enforces HTTPS at the policy layer; old SDKs and misconfigured clients cannot fall back to HTTP |
| Default encryption | SSE-S3 (AES256) | Protects data at rest; AWS manages the key transparently |

EventBridge notifications are enabled on the bucket so object-creation events flow into the rest of the system. The routing rule lives with the queue module.

CORS is configured to allow `PUT` requests from the origins listed in `allowed_upload_origins`, which is the only method clients need to deposit documents.

> [!NOTE]
> The bucket currently uses SSE-S3 (AES256). For workloads ingesting PII or regulated documents, SSE-KMS with a customer-managed key and S3 Bucket Keys enabled provides a second permission gate (`kms:Decrypt` in addition to `s3:GetObject`) and full CloudTrail auditability on every decrypt.

---

## Infrastructure

Terraform state is stored remotely in an S3 bucket created by [bootstrap.sh](bootstrap.sh). The bucket is private, versioned, encrypted at rest, and uses S3 native locking (`use_lockfile = true`), so no DynamoDB table is required. A single bucket is shared across environments, with state isolated by prefix (`service/local/`, `service/dev/`, `service/prod/`).

The [Makefile](Makefile) wraps all common Terraform commands. Every target accepts `ENV={local,dev,prod}` (defaults to `local`):

```bash
make bootstrap         # Create state bucket and write backend files for every env (once per AWS account)
make init  ENV=dev     # terraform init against the env's backend config
make plan  ENV=dev     # Preview infrastructure changes
make apply ENV=dev     # Apply infrastructure changes (refuses prod unless I_KNOW=1)
make format            # Format all Terraform files
make destroy ENV=dev   # Destroy infrastructure for the env (refuses prod unless I_KNOW=1)
```

CI uses two additional targets — `make ci-plan` saves a plan to `tfplan.<env>`, and `make ci-apply` consumes it — so the apply job runs the exact bytes that were reviewed.

> [!IMPORTANT]
> `infra/envs/*.backend.tfbackend` is gitignored and must never be committed. Run `make bootstrap` (or just `make backend` if the bucket already exists) to regenerate them after a fresh clone.

---

## Environments & delivery

The project treats `local`, `dev`, and `prod` as three peers of the same model. Each has its own least-privileged deploy role (defined in [infra/iam/](infra/iam/)), its own state prefix, and its own apply path:

| Env | Identity | Apply path |
|---|---|---|
| `local` | IAM user assumes `agentic-kie-local-deploy` | `make apply` from your laptop |
| `dev` | OIDC → `agentic-kie-dev-deploy` (trust scoped to `develop` + PRs) | Auto-apply on merge to `develop` |
| `prod` | OIDC → `agentic-kie-prod-deploy` (trust scoped to `environment:prod`) | Saved-plan apply, manual approval via the `prod` GitHub Environment |

Two workflows under [.github/workflows/](.github/workflows/) drive CI: `deploy-dev.yml` posts a plan on PR and auto-applies on merge; `deploy-prod.yml` posts a plan on PR and, on merge, generates a saved plan that is applied only after manual approval.

A `deny_other_envs` IAM policy combined with `Environment` resource tagging prevents a role in one environment from modifying resources tagged to another. See [CONTRIBUTING.md](CONTRIBUTING.md#devops-strategy) for the full strategy and the bootstrap procedure.

---

## Getting started

> [!IMPORTANT]
> Requires [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.15 and the [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html) configured with credentials.

1. Bootstrap the remote state backend and IAM deploy roles (once per AWS account, with admin credentials). See [CONTRIBUTING.md](CONTRIBUTING.md#bootstrap-one-time-with-admin-credentials) for the full procedure:

```bash
make bootstrap
```

2. Configure your laptop to assume the `local` deploy role (see [CONTRIBUTING.md](CONTRIBUTING.md#local-role-usage)), then initialize Terraform:

```bash
AWS_PROFILE=agentic-kie-local make init
```

3. Preview and apply:

```bash
AWS_PROFILE=agentic-kie-local make plan
AWS_PROFILE=agentic-kie-local make apply
```

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for prerequisites, available `make` targets, the IAM bootstrap procedure, and the full DevOps strategy.
