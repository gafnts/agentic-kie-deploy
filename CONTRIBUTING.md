# Contributing

This repo contains the Terraform infrastructure for the Agentic KIE project, deployed to AWS across three environments (`local`, `staging`, `prod`). Contributing means authoring, for the most part, Terraform and Python. Infrastructure changes trigger a CI-generated plan on every PR so reviewers can see exactly what would land; production additionally gates the apply on a manual approval against a saved plan generated post-merge.

> [!IMPORTANT]
> This project requires:
> - [Terraform](https://developer.hashicorp.com/terraform/install) ~> 1.15.0
> - [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html) configured with credentials
> - [GitHub OIDC provider](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws) configured in your AWS account
> - [uv](https://docs.astral.sh/uv/) for Python tooling and pre-commit hooks

> [!NOTE]
> Check if your AWS account already has a GitHub OIDC provider configured: `aws iam list-open-id-connect-providers`. If it's not there, create it once (`token.actions.githubusercontent.com`, audience `sts.amazonaws.com`). The IAM module references it but doesn't create it.

## Contents

- [DevOps strategy](#devops-strategy)
  - [Environment model](#environment-model)
  - [Branch model](#branch-model)
- [First-time setup](#first-time-setup)
  - [Install development dependencies and hooks](#install-development-dependencies-and-hooks)
  - [Bootstrap the remote state backend](#bootstrap-the-remote-state-backend)
  - [Create the IAM roles](#create-the-iam-roles)
  - [Create the ECR repository](#create-the-ecr-repository)
  - [Create the extractor secrets](#create-the-extractor-secrets)
  - [Configure GitHub](#configure-github)
  - [Configure your local AWS profile](#configure-your-local-aws-profile)
- [Day-to-day workflow](#day-to-day-workflow)
  - [Local iteration](#local-iteration)
  - [Selecting the extractor flavor](#selecting-the-extractor-flavor)
  - [Manual smoke test](#manual-smoke-test)
  - [Load testing](#load-testing)
  - [Quality gates](#quality-gates)
  - [Shipping to staging](#shipping-to-staging)
  - [Promoting to prod](#promoting-to-prod)
  - [Adding new infrastructure](#adding-new-infrastructure)
- [Teardown](#teardown)
  - [Destroy the per-env stacks](#destroy-the-per-env-stacks)
  - [Destroy the IAM roles](#destroy-the-iam-roles)
  - [Delete the out-of-band resources](#delete-the-out-of-band-resources)
- [Reference](#reference)
  - [Make targets](#make-targets)
  - [Files that are gitignored](#files-that-are-gitignored)
  - [Design notes](#design-notes)

## DevOps strategy

### Environment model

The project has three deployment environments, all in the same AWS account:

| Environment | Who deploys | When |
|---|---|---|
| `local` | You, from your laptop | Iterating on infrastructure changes |
| `staging` | GitHub Actions | On merge to `develop` |
| `prod` | GitHub Actions | On merge to `main`, gated by manual approval |

> [!NOTE]
> Each environment has its own Terraform state file, its own IAM role, and its own set of resources tagged with `Environment=<env>`. The IAM roles are scoped so each one can only touch resources tagged for its own environment.

### Branch model

Two long-lived branches map to the two CI-managed environments: `develop` drives `staging`, `main` drives `prod`. Every change flows through a PR with a plan attached, and prod additionally waits on a manual approval before the saved plan is applied.

```mermaid
flowchart LR
    feature[Feature branch] -->|PR| develop[develop]
    develop -->|CI plans staging| planStaging{{Plan staging}}
    planStaging -->|merge| applyStaging[CI applies staging]

    develop -->|PR| main[main]
    main -->|CI plans prod| planProd{{Plan prod}}
    planProd -->|merge| savedPlan[CI saves plan]
    savedPlan --> approval[/Manual approval/]
    approval --> applyProd[CI applies prod]
```

> [!NOTE]
> The staging and prod apply jobs are not symmetric. Staging runs `terraform apply` directly against current state at merge time—the PR plan is informational, not the artifact applied. This is intentional: staging is the iteration environment, and the simplification is a reasonable trade-off. Prod is plan-bound: a new plan is generated post-merge, saved as an artifact, and that exact artifact is what gets applied after approval.

## First-time setup

### Install development dependencies and hooks

Sync Python dependencies, install pre-commit hooks for both `pre-commit` and `pre-push` stages, and install the tflint plugins declared in `.tflint.hcl`:

```bash
make install
```

This is idempotent. Re-run it after `git pull` whenever `pyproject.toml`, `.pre-commit-config.yaml`, or `.tflint.hcl` change.

Hooks fire automatically on every git operation:

| Stage | What runs | When |
|---|---|---|
| `pre-commit` | hygiene checks (whitespace, YAML, merge conflicts, large files, private keys), `terraform fmt`, `tflint`, `gitleaks`, `actionlint`, `shellcheck`, `pyproject-fmt`, `ruff check`, `ruff format` | On `git commit` |
| `pre-push` | `terraform validate`, `terraform trivy`, `mypy`, `pytest` | On `git push` |

The split keeps commits fast (sub-second feedback for the common loop) and reserves the slower scanners and validators for push time, before changes are shared.

> [!IMPORTANT]
> If not already installed in your system, install the following tools first—each links to installation instructions:
> - [trivy](https://github.com/aquasecurity/trivy)
> - [tflint](https://github.com/terraform-linters/tflint#installation)
> - [gitleaks](https://github.com/gitleaks/gitleaks#installing)
> - [actionlint](https://github.com/rhysd/actionlint#installation)

### Bootstrap the remote state backend

You only do it once per AWS account.

Creates the S3 bucket that holds Terraform state for all three environments, the four `*.backend.tfbackend` config files (one per env, plus one for the IAM bootstrap), and `infra/iam/iam.tfvars` (gitignored) pre-populated with your caller ARN and bucket name:

```bash
make bootstrap
```

The bucket is private, versioned, encrypted, and uses S3 native locking (`use_lockfile = true`). No DynamoDB table required. The bootstrap script is idempotent.

### Create the IAM roles

The three deploy roles (`local`, `staging`, `prod`) live in a separate Terraform root at `infra/iam/`. They're applied once with admin credentials and rarely touched afterward.

```bash
make iam-init && make iam-apply
```

The output gives you three role ARNs. Keep them—you'll paste two into GitHub and one into your AWS config.

### Create the ECR repository

The extractor Lambda is a container image, so the ECR repository must exist before the service stack can be applied. The registry lives in its own Terraform root at [infra/registry/](infra/registry/), one state file per environment, applied once per env and rebuilt approximately never afterwards. See [ADR-0008](docs/adr/0008-ecr-registry-stack-and-digest-pinned-images.md) for the rationale.

```bash
make registry-init ENV=local && make registry-apply ENV=local
make registry-init ENV=staging && make registry-apply ENV=staging
make registry-init ENV=prod  && make registry-apply ENV=prod
```

The repository is named `agentic-kie-deploy-<env>-extractor`, has tag immutability on, scan-on-push enabled, and a lifecycle policy that keeps the last ten tagged images and expires untagged images after a day. Each env writes to its own state file (`service/<env>/registry.tfstate`).

> [!TIP]
> For local-only setup, `make provision` chains `iam-init`/`iam-apply`, `registry-init`/`registry-apply`, and the service-stack `init` in one shot. The `staging` and `prod` registries still need their own `registry-init`/`registry-apply` runs, since `provision` only covers `ENV=local`.

> [!NOTE]
> The service stack consumes the repository via a `data "aws_ecr_repository"` lookup in the extractor module. If `make plan` later fails with `couldn't find resource`, the registry stack has not been applied for that env.

### Create the extractor secrets

The extractor Lambda depends on two long-lived API keys: the LLM provider key (used on the hot path) and the LangSmith key (used to ship traces). They are stored in AWS Secrets Manager, one secret per environment, created out-of-band so their lifecycle stays independent of `terraform apply` / `terraform destroy`. See [ADR-0009](docs/adr/0009-extractor-lambda.md) for the rationale.

Create the six secrets (two keys per env, three envs).

First, the LLM provider keys (one per env):

```bash
aws secretsmanager create-secret \
  --name agentic-kie-deploy/local/llm-provider \
  --secret-string '<your-llm-provider-key>'
aws secretsmanager create-secret \
  --name agentic-kie-deploy/staging/llm-provider \
  --secret-string '<your-llm-provider-key>'
aws secretsmanager create-secret \
  --name agentic-kie-deploy/prod/llm-provider \
  --secret-string '<your-llm-provider-key>'
```

Then, the LangSmith keys (one per env):

```bash
aws secretsmanager create-secret \
  --name agentic-kie-deploy/local/langsmith \
  --secret-string '<your-langsmith-key>'
aws secretsmanager create-secret \
  --name agentic-kie-deploy/staging/langsmith \
  --secret-string '<your-langsmith-key>'
aws secretsmanager create-secret \
  --name agentic-kie-deploy/prod/langsmith \
  --secret-string '<your-langsmith-key>'
```

Terraform discovers the secrets by name at plan time—no ARNs to copy or paste.

> [!IMPORTANT]
> Terraform manages the IAM grants on these secrets but **not** their values. Rotating a key is `aws secretsmanager update-secret` against the existing secret; the Lambda picks the new value up on the next cold start (warm invocations within a ~15-minute execution-environment lifetime continue to see the old value, by design).

### Configure GitHub

In the repo settings:

**Settings → Environments → New environment → `prod`**
- Add yourself as a required reviewer.
- This is what gates the prod apply step.

**Settings → Secrets and variables → Actions → Variables (Repository tab)**
- `AWS_ROLE_ARN_STAGING` = `<staging_role_arn>` from the Terraform output
- `AWS_ROLE_ARN_PROD_PLAN` = `<prod_plan_role_arn>` from the Terraform output (used by plan jobs on PRs and post-merge, plus the pre-apply build-and-push job; read-only except for scoped ECR push to the extractor repository)
- `AWS_ROLE_ARN_PROD` = `<prod_role_arn>` from the Terraform output (write; used by the apply job only)

Variables (not secrets) is correct since role ARNs aren't sensitive on their own.

### Configure your local AWS profile

Add to `~/.aws/config`:

```ini
[profile agentic-kie-local]
role_arn       = <local_role_arn>
source_profile = default
region         = us-east-1
```

`source_profile = default` assumes you're already authenticated as your IAM user via `~/.aws/credentials` or SSO. The `agentic-kie-local` profile assumes the local-deploy role on top of that.

Verify:

```bash
AWS_PROFILE=agentic-kie-local aws sts get-caller-identity
```

The returned ARN should end in `assumed-role/agentic-kie-local-deploy/...`.

## Day-to-day workflow

### Local iteration

The repo includes a `.envrc` that sets `AWS_PROFILE=agentic-kie-local` automatically via [direnv](https://direnv.net). Run `direnv allow` once after cloning and the profile is set whenever you enter the directory. Without direnv, export it manually:

```bash
export AWS_PROFILE=agentic-kie-local
```

```bash
make init                # Initialize the local backend (idempotent, safe to re-run)
make plan                # Preview changes
make apply               # Apply changes
make destroy ENV=local   # Tear down all local resources
```

> [!NOTE]
> The service stack requires `extractor_image_digest` (digest-pinned per ADR-0008/0009). For local applies, use `make build-extractor` to build, push, and capture the digest in one step:
>
> ```bash
> export TF_VAR_extractor_image_digest=$(make build-extractor ENV=local)
> make apply ENV=local
> ```
>
> `make build-extractor` accepts any `ENV`, but outside `local` that use case is owned by CI. Only reach for it manually for other environments when troubleshooting.

> [!IMPORTANT]
> `make` defaults to `ENV=local`. The Makefile refuses to apply or destroy `prod` unless `I_KNOW=1`—only CI is allowed to set that. `make destroy` only tears down the service stack; the ECR repository in `infra/registry/` has its own lifecycle and a separate `make registry-destroy ENV=<env>` command (same guards apply—prefer it over invoking `terraform destroy` directly inside `infra/registry/`).

### Selecting the extractor flavor

The extractor ships in two flavors: `single_pass` issues one structured LLM call, `agentic` runs a ReAct loop over the document. The flavor is a deploy-time choice *per environment*—it drives the whole parameter profile (the agent's `max_iterations` and the queue's `maxReceiveCount`), so re-parametrizing is a one-variable flip. See [ADR-0016](docs/adr/0016-agentic-flavor-deployment.md) for the rationale.

It's set in the committed `infra/envs/<env>.tfvars` file, which every `make plan`/`make apply` for that env loads automatically. Current defaults:

| Env | `extractor_flavor` |
|---|---|
| `local` | `agentic` |
| `staging` | `agentic` |
| `prod` | `single_pass` |

To change a flavor, edit the env's tfvars and ship it through the normal PR → plan → apply flow—the plan diff shows the flavor switch and the two parameter changes it pulls along (`max_iterations`, `maxReceiveCount`).

> [!TIP]
> For a throwaway local experiment, call Terraform directly with a trailing `-var`, which wins over the var-file (a CLI `-var` outranks `-var-file`; a `TF_VAR_` env var would not):
>
> ```bash
> terraform -chdir=infra apply -var-file=envs/local.tfvars -var extractor_flavor=single_pass
> ```
>
> Keep staging and prod changes as a reviewable tfvars diff, never an override.

> [!NOTE]
> Flavor drives cost: `agentic` issues multiple LLM calls per document, `single_pass` one. That's why the same load scenario costs roughly 2× more on `agentic` (see [Load testing](#load-testing)). Staging runs `agentic`, so load tests—which target staging—pay the agentic rate.

### Manual smoke test

After applying infrastructure, you can verify the pipeline end-to-end using only the AWS CLI. Two paths matter: the direct-S3 path isolates the extraction half (S3 → EventBridge → SQS → Lambda → DynamoDB), and the uploader-API path drives the whole surface front to back—the front door (API Gateway → presigner → presigned PUT), through extraction, and on to the publisher's analytics write (DynamoDB Streams → publisher → analytics S3).

**Direct-S3 path** (bypasses the uploader, useful for isolating the extraction half):

```bash
# 1. Capture terraform outputs
BUCKET=$(terraform -chdir=infra output -raw ingestion_bucket_name)
TABLE=$(terraform -chdir=infra output -raw results_table_name)
FUNCTION=$(terraform -chdir=infra output -raw extractor_function_name)

# 2. Upload a PDF
DOC_ID=$(uuidgen | tr '[:upper:]' '[:lower:]')
aws s3 cp tests/static/smoke_document.pdf \
  "s3://$BUCKET/uploads/$(date +%Y/%m/%d)/$DOC_ID"

# 3. Watch Lambda logs in real time
aws logs tail "/aws/lambda/$FUNCTION" --follow

# 4. Check DynamoDB for the result
aws dynamodb get-item \
  --table-name "$TABLE" \
  --key "{\"document_id\":{\"S\":\"$DOC_ID\"}}" \
  --consistent-read
```

**Uploader-API path** (the full surface—SigV4, presigner, extraction, and the publisher's write to the analytics bucket):

```bash
# 1. Capture the uploader endpoint and downstream outputs
API=$(terraform -chdir=infra output -raw uploader_api_endpoint)
TABLE=$(terraform -chdir=infra output -raw results_table_name)
ANALYTICS_BUCKET=$(terraform -chdir=infra output -raw analytics_bucket_name)

# 2. Sign POST /uploads with SigV4 and capture the presigned PUT URL
RESP=$(awscurl --service execute-api -X POST "$API/uploads")
DOC_ID=$(echo "$RESP" | jq -r .document_id)
UPLOAD_URL=$(echo "$RESP" | jq -r .upload_url)

# 3. PUT the document to the returned URL (no signing—the URL is already signed)
curl -X PUT --data-binary @tests/static/smoke_document.pdf "$UPLOAD_URL"

# 4. Check DynamoDB for the extractor's terminal row
aws dynamodb get-item \
  --table-name "$TABLE" \
  --key "{\"document_id\":{\"S\":\"$DOC_ID\"}}" \
  --consistent-read
```

The publisher then fans the terminal row out to the analytics bucket as `extractions/{yyyy}/{mm}/{dd}/{document_id}.json`, the same bytes Athena queries. Confirm it landed:

```bash
# 5. Confirm the publisher wrote the result object to the analytics bucket
aws s3 ls "s3://$ANALYTICS_BUCKET/extractions/" --recursive | grep "$DOC_ID"
```

> [!NOTE]
> `make smoke` runs both paths automatically via pytest (`TestExtractorSmoke` and `TestUploaderSmoke` in [tests/test_smoke.py](tests/test_smoke.py), each with a 180-second timeout). Use the manual steps above when you want to observe log output in real time or inspect the raw DynamoDB item.

### Load testing

Where smoke drives one document, the load harness drives the **real front door under arrival pressure**—presigner → presigned PUT → S3 → EventBridge → SQS → extractor → DynamoDB → Streams → publisher → analytics S3—across two arrival patterns, and answers whether the pipeline *degrades gracefully (buffers and drains) rather than fails (errors and DLQs)*, and whether the alarms tell the truth while it happens. It lives under [tests/load/](tests/load/), is excluded from the default `pytest` run (`norecursedirs`), and is invoked explicitly. See [ADR-0015](docs/adr/0015-load-testing-strategy.md) for the full design—the two scenarios (a calm `sustained` baseline and a saturating `burst`), the five SLOs, and the predictions each run tests.

> [!IMPORTANT]
> Load tests run against **staging only** and spend **real money** on live LLM calls, plus real writes to staging that the harness deletes in a `finally`. The cost depends on the deployed extractor flavor: **single-pass** is roughly **$1.40 per scenario** (~$2.80 for both), while **agentic** is closer to **$2.90 per scenario** (~$5.85 for both). The harness resolves the target environment from the deployed resource names and **refuses `prod`** at runtime, regardless of how it's invoked.

**Materialize the corpus** (once per clone). The sample is drawn from the Kleister NDA *train* partition, which is **not committed**—the PDFs would bloat the repo and trip the large-files hook. Fetch it via the pinned [`nda`](https://github.com/gafnts/kleister-nda-preparation) package into the git-ignored corpus directory:

```bash
uv run nda --output_dir tests/load/documents
```

This lays down `tests/load/documents/{train,dev-0,test-A}/documents/*.pdf`; the harness samples 200 distinct train documents with a fixed seed and reuses them, in the same order, across both scenarios (reproducible from the pinned package plus the seed).

**Run a scenario.** Point Terraform at staging, then invoke the scenario explicitly—`LOAD_SCENARIO` selects `burst` or `sustained`:

```bash
make init ENV=staging
LOAD_SCENARIO=burst     uv run pytest tests/load/test_scenarios.py -s
LOAD_SCENARIO=sustained uv run pytest tests/load/test_scenarios.py -s
```

Each run injects the documents, samples queue depth and in-flight concurrency live (CloudWatch's 1-minute series would smooth past the burst's true peak), tracks every document to its result landing, reads the latency segments from **server-side** timestamps, pulls the CloudWatch / Logs Insights / alarm telemetry, and writes a JSON artifact under `tests/load/reports/` alongside a printed summary and a pass/fail verdict against the five SLOs.

> [!TIP]
> Verify the whole path end-to-end for pennies before committing to a full 200-document run by shrinking the sample with `LOAD_N`:
>
> ```bash
> LOAD_N=3 LOAD_SCENARIO=burst uv run pytest tests/load/test_scenarios.py -s
> ```
>
> | Variable | Default | Purpose |
> |---|---|---|
> | `LOAD_SCENARIO` | `burst` | Arrival pattern: `burst` or `sustained` |
> | `LOAD_N` | `200` | Sample size—lower it for a contained dry-run |
> | `LOAD_SETTLE` | `120` | Seconds to wait after drain for CloudWatch/Logs to propagate before the metric pull |

> [!NOTE]
> The corpus sanity check needs no AWS and makes no LLM call—it parses the sampled PDFs locally and confirms the size/token distribution sits inside the run envelope (the extractor's 120s timeout and the Gemini Tier-1 TPM ceiling) before any traffic is generated. Run it standalone after materializing the corpus:
>
> ```bash
> uv run pytest tests/load/test_corpus.py -s
> ```

### Quality gates

Hooks run automatically, but you can also invoke them on demand. Useful when you want fast feedback on a single tool, or to run the full suite before pushing.

```bash
make check     # Run every hook against every file (both stages)
make format    # Apply ruff lint fixes and formatting to src
make lint      # Run ruff check on src
make type      # Run mypy on src
make test      # Run pytest with coverage
make tf-format # Format all Terraform files
```

> [!IMPORTANT]
> `make check` is what the CI mirror job runs. If it passes locally, your PR will pass the lint/format/scan stage in CI.

> [!NOTE]
> If a hook version in `.pre-commit-config.yaml` is updated, `make install` reinstalls the hook environments. If the tflint plugin version in `.tflint.hcl` changes, run `make tflint-init` (or `make install`) to refresh the plugin cache.

### Shipping to staging

Branch from `develop` and push:

```bash
git switch develop
git pull
git switch -c feature/my-change
# ... edit ...
git push -u origin feature/my-change
```

Then open a PR targeting `develop`. Within a minute the PR gets a sticky comment titled **"Terraform Plan · `staging`"** showing what would be applied. Review the plan as part of code review.

Merge the PR. CI applies the change to staging automatically, then runs `make smoke` as a post-apply gate—a smoke failure fails the workflow. Smoke exercises both entry points (see [Manual smoke test](#manual-smoke-test) above).

> [!NOTE]
> The staging workflow triggers on changes under `infra/**` (excluding the `iam/` and `registry/` roots), `src/extractor/**`, `src/uploader/**`, or `src/publisher/**`. If anything under `src/extractor/**` changed, CI runs `build-and-push` first—it builds the container image, pushes it to the staging ECR repository, and publishes the resulting digest as a job output the apply job consumes. Changes under `src/uploader/**`, `src/publisher/**`, or service-only Terraform tweaks skip the Docker work; the uploader and publisher are zip Lambdas repackaged by Terraform on every apply, and infra-only changes re-apply with the previously-deployed digest.

### Promoting to prod

Open a PR from `develop` to `main`. CI posts a sticky **"Terraform Plan · `prod`"** comment. Review and merge.

After the merge, CI runs the prod pipeline and pauses at a manual gate:

1. If the extractor changed, `build-and-push` publishes a new image and emits its digest; otherwise it resolves the currently-deployed digest.
2. The `plan` job bakes that digest into a saved plan and uploads it as a workflow artifact.
3. The `apply` job queues behind the `prod` environment approval gate, and you get notified.
4. Open the workflow run, review the plan in the `plan` job's logs, then click **Review deployments → Approve**.
5. CI applies the saved plan—the exact bytes from step 2, not a fresh plan against current state.

If the plan looks wrong at the gate, reject it instead. Nothing is applied.

> [!NOTE]
> The prod workflow triggers on the same paths as staging—`infra/**` (excluding the `iam/` and `registry/` roots), `src/extractor/**`, `src/uploader/**`, `src/publisher/**`—and shares the build/digest behavior described above. What differs is the role split: `build-and-push` and `plan` run under the prod-*plan* role (scoped ECR push, no gate), and only `apply` assumes the prod role behind the environment approval.

### Adding new infrastructure

Most changes are app-level—new modules in `infra/modules/`, wired into `infra/main.tf`. The IAM roles already have `PowerUserAccess`, so they cover almost any AWS service you'd add. The deploy flow is unchanged.

After adding a new Terraform module or bumping a provider version, regenerate the lock files so CI (linux/amd64) has the right platform hashes:

```bash
make lock
```

Then commit the updated `.terraform.lock.hcl` files alongside your change. If you skip this, the `terraform_validate` pre-push hook will fail in CI with "files were modified by this hook" (it runs `terraform init`, which rewrites the lock file to add the missing linux/amd64 hashes).

You only need to touch `infra/iam/` when:

- Adding a new IAM-related resource pattern that needs explicit allow (rare).
- Tightening the permissions policy from `PowerUserAccess` to a service-specific allowlist.
- Adding a new environment.

## Teardown

Teardown is [First-time setup](#first-time-setup) in reverse, and the order matters: every `terraform destroy` reads state from the state bucket, so the bucket goes **last**. Delete it earlier and you orphan whatever Terraform still tracks—the resources keep existing in AWS, but Terraform can no longer see them to remove them.

`make destroy ENV=<env>` only tears down the per-env **service stack** (the pipeline in `infra/main.tf`). The registry stack (ECR), the IAM roles, the state bucket, and the out-of-band secrets each have their own lifecycle—just like setup—and survive it. Tear them down in this order.

### Destroy the per-env stacks

`make destroy-env ENV=<env>` chains the service-stack and registry (ECR) destroys for one environment in the right order (service first—it reads the ECR repo via a `data` source, so the repository must outlive it). Same guards as the rest of the Makefile: it refuses a defaulted `ENV` and refuses `prod` unless `I_KNOW=1`. Run it with the same profile you deploy with (the scoped deploy role).

```bash
make destroy-env ENV=local
make destroy-env ENV=staging
```

> [!WARNING]
> Prod carries deliberate guard rails that block an automated destroy until you clear them by hand:
> - **DynamoDB** has `deletion_protection_enabled = true`—disable it first: `aws dynamodb update-table --no-deletion-protection-enabled --table-name agentic-kie-deploy-prod-results`.
> - The **ingestion and analytics S3 buckets** are `force_destroy = false`—empty them first.
> - The **prod ECR repository** is `force_delete = false`—delete its images first (`aws ecr batch-delete-image …`) or the registry destroy fails.
>
> Once cleared: `make destroy-env ENV=prod I_KNOW=1`.

### Destroy the IAM roles

The deploy roles live in a single all-env module, so one destroy removes all of them (the `prod` role included, hence `I_KNOW=1`). Run with **admin/default credentials**—the same creds you used for `make iam-apply`, not the assumed deploy role. (`PowerUserAccess` excludes IAM writes, and each role's cross-env deny stops it from deleting another env's role, so the scoped role can't do this job.)

```bash
AWS_PROFILE=default make iam-destroy I_KNOW=1
```

### Delete the out-of-band resources

The six Secrets Manager secrets and the shared, versioned state bucket live outside Terraform (created by `make bootstrap` and by hand). `teardown.sh` is the mirror of `bootstrap.sh`: it deletes the secrets and empties + deletes the state bucket. Run it **last**, after every stack above is gone, again with admin/default credentials:

```bash
AWS_PROFILE=default bash teardown.sh
```

It prints exactly what it will delete and makes you type the bucket name to confirm. Secrets are scheduled for deletion with the default recovery window (recoverable); set `FORCE=1` to delete them immediately and irreversibly. The bucket deletion is always irreversible.

> [!IMPORTANT]
> Teardown leaves the account's **GitHub OIDC provider** in place. The IAM module references it but never created it (see [First-time setup](#first-time-setup)), and other projects in the account may depend on it. Remove it manually only if you're certain nothing else uses it.

## Reference

### Make targets

Run `make help` for the full list of targets with descriptions, grouped by section. `ENV` defaults to `local`; override with `make plan ENV=staging`, etc.

### Files that are gitignored

- `.terraform/` — Terraform plugin cache and local state
- `infra/tfplan.*` — Saved plan binaries
- `infra/iam/iam.tfvars` — Contains your principal ARN
- `tests/load/documents/` — The load-test corpus, materialized via `uv run nda` (not committed)
- `tests/load/reports/` — Load-test run artifacts (JSON), except the committed `baseline/`

### Design notes

- **State bucket and IAM roles** are the only resources provisioned with admin credentials. All subsequent operations use the scoped deploy roles.
- **Backend files** (`infra/envs/*.backend.tfbackend`, `infra/iam/backend.tfbackend`) are committed to the repo and generated deterministically by `bootstrap-backend.sh` from the project name. CI regenerates them on every job; locally they are generated once.
- **`make plan` / `make apply`** behave identically locally and in CI. The only differences are the `AWS_PROFILE` value and the `I_KNOW=1` flag required for prod.
- **Every new resource must be tagged `Environment=<env>`** (by convention). Each deploy role carries an explicit IAM deny on any resource whose `Environment` tag belongs to a *different* env, so one environment's role can't touch another's. The deny is `Null`-guarded so it doesn't fire on absent tags—otherwise resource creation (which carries no resource tag yet) would break—so it enforces cross-environment isolation rather than catching untagged resources. A cross-env violation won't surface during `plan`; it denies at `apply`.
- **Prod protection is enforced at the IAM trust layer, not just CI**. The prod role's OIDC trust condition requires `environment:prod` GitHub environment context. Bypassing the approval gate in the workflow still results in a failed `AssumeRoleWithWebIdentity` call.
