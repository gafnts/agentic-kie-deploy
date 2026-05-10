# ADR-0008: ECR Registry as a Separate Stack, Digest-Pinned Extractor Images

## Status

Accepted (2026-05-09)

## Context

ADR-0001 fixed the extractor as a container Lambda packaged from ECR, on the grounds that `agentic-kie-deploy`'s ML and LLM dependencies do not fit comfortably under Lambda's zipped layer limits. That decision delegated the registry to a future module; this ADR settles its shape and — equally important — the deployment choreography that hangs off it.

Five concerns drive the design and they are coupled enough that one ADR is clearer than five:

### Stack layout and the bootstrap order problem

The extractor Lambda needs an `image_uri` to be created. The image needs a repository to exist before it can be pushed. Terraform plans the Lambda and the repository in the same graph, but the image push happens *between* them and is not Terraform's job. Three resolutions exist:

1. **Same stack, targeted bootstrap.** `module.registry` lives inside the main service stack; the first apply per environment runs `terraform apply -target=module.registry`, then CI builds and pushes, then a full `terraform apply` wires the Lambda. One-time targeted apply per env, no extra state.
2. **Separate stack, like `iam/`.** `infra/registry/` owns its own state, applied once per env. The main stack consumes the repository URL via a `data` source. Two extra `make` targets and a state file per env, but no targeted applies and a clean dependency direction (registry → service, never the reverse).
3. **Terraform-driven Docker push of a placeholder.** A `null_resource` runs `docker push` of a stub image so a single end-to-end `terraform apply` works. Removes the bootstrap quirk at the cost of putting Docker on every Terraform runner and turning `plan` output into an opaque local-exec.

Option 3 trades a small one-time inconvenience for a much larger steady-state one and is rejected. Between options 1 and 2, the deciding factor is *what kind of object the registry is*. The repository is a **build-time durable**: it outlives any individual deploy, is rebuilt approximately never, and is shared across every push of every commit on a given branch. The service stack is a **deploy-time** artifact: every PR plans against it, every merge applies. Mixing those lifecycles in one stack means every service plan re-evaluates a repository nobody intends to change, and the targeted-apply bootstrap becomes a recurring papercut whenever the bootstrap order is forgotten by a new contributor or a fresh environment. The IAM stack already establishes the precedent: things that bootstrap once and live longer than the service belong in their own state.

### Image pinning

The Lambda needs a stable handle to a specific build. Three handles are available:

- **Tag** (e.g. `git-sha`). Readable in plans, requires `IMMUTABLE` repository policy to prevent silent rolls.
- **Digest** (`sha256:…`). Cryptographically pins the bytes; immune to tag mutability bugs by construction.
- **Mutable convenience tag** (e.g. `dev-latest`). Forces redeployment via Lambda update each push; loses replay/rollback determinism and is incompatible with `IMMUTABLE`.

Digest pinning is strictly stronger than tag pinning: it produces deterministic plans (the digest changes iff the bytes changed), it survives accidental tag-mutability misconfigurations, and it makes Terraform-level rollback (revert the tfvar, re-apply) a one-line operation. The ergonomic loss — the digest is a 71-character opaque string in the plan — is mitigated by sourcing it from CI rather than typing it.

### Lifecycle policy

ECR storage is per-GB-month. Without a lifecycle policy, every CI build accumulates indefinitely; even at portfolio scale this is wasteful and complicates audit. Two pressures:

- **Tagged images** are rollback targets. Recent tags carry value (the last few good builds), older tags carry approximately none.
- **Untagged images** are orphans, almost always from failed pushes or images displaced by a `:latest`-style retag. They have no rollback value at all.

A "keep last 10 tagged + expire untagged after 1 day" policy bounds steady-state storage to roughly ten image sizes per env while preserving a useful rollback window. The 1-day floor on untagged images leaves enough time to investigate a half-completed push before the artifact disappears.

### Scanning

ECR offers two scanning modes. Basic scan-on-push runs an AWS-native CVE scan once per push, free, with findings exposed on the ECR console and as EventBridge events. Enhanced scanning via Inspector rescans continuously and covers OS plus language packages, but it is configured at the account/region level (one config governs the entire registry) and bills per image.

Scan-on-push is per-repository, fits cleanly inside the registry module, and gives a CVE signal at the moment a build lands. Enhanced is the right answer at production scale and the upgrade path is non-breaking — turn it on later via `aws_ecr_registry_scanning_configuration` with a `repository_filter` of `agentic-kie-deploy-*`.

### Encryption and repository policy

ADR-0004 deferred CMKs for the ingestion bucket on portfolio-project grounds. ADR-0007 mirrored that for the table. The same logic applies to the repository: no real PII lives in container images, and the migration to a CMK is symmetric to the storage and table migrations (rotate the key, grant `kms:Decrypt`/`kms:GenerateDataKey` to readers and writers). AES256 keeps posture parity across all three data stores.

The repository policy needs one statement: allow the Lambda service principal (`lambda.amazonaws.com`) to pull this repository's images, scoped by `aws:SourceArn` to the extractor function so an unrelated Lambda in the account cannot pull. Pushes are governed by the deploy role's `PowerUserAccess` and are not narrowed further at this layer.

## Decision

### Layout

A new top-level Terraform stack at `infra/registry/`, mirroring the shape of `infra/iam/`:

```
infra/registry/
  main.tf
  variables.tf
  outputs.tf
  terraform.tf
  envs/
    local.backend.tfbackend
    dev.backend.tfbackend
    prod.backend.tfbackend
    local.tfvars
    dev.tfvars
    prod.tfvars
```

State key per environment: `service/${env}/registry.tfstate`, sibling to `service/${env}/terraform.tfstate`. The stack is per-environment (unlike `iam/`, which is account-global) because ECR repositories are regional and the `Environment` tag-deny IAM guard requires per-env tagging.

The main service stack consumes the repository by name via a `data "aws_ecr_repository"` lookup, not via remote state. Coupling on the repository name (`agentic-kie-deploy-${env}-extractor`) keeps the stacks loosely connected: deleting and recreating the registry stack does not require a `terraform_remote_state` plumbing change in the service stack.

### Repository

```hcl
resource "aws_ecr_repository" "extractor" {
  name                 = "agentic-kie-deploy-${var.environment}-extractor"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = var.environment != "prod"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}
```

`force_delete` follows the same rule as the storage and table modules: prod must not be wiped accidentally, but `make destroy ENV=local` and `ENV=dev` should work.

### Lifecycle policy

```hcl
resource "aws_ecr_lifecycle_policy" "extractor" {
  repository = aws_ecr_repository.extractor.name
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after 1 day"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep last 10 tagged images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 10
        }
        action = { type = "expire" }
      }
    ]
  })
}
```

### Repository policy

```hcl
data "aws_iam_policy_document" "extractor" {
  statement {
    sid    = "AllowExtractorLambdaPull"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
    actions = [
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values = [
        "arn:aws:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:function:agentic-kie-deploy-${var.environment}-extractor",
      ]
    }
  }
}

resource "aws_ecr_repository_policy" "extractor" {
  repository = aws_ecr_repository.extractor.name
  policy     = data.aws_iam_policy_document.extractor.json
}
```

The `aws:SourceArn` condition closes the confused-deputy class: any Lambda outside the extractor's ARN cannot pull, even if its execution role would otherwise permit it.

### Image pinning

The extractor module (future ADR) takes a single tfvar:

```hcl
variable "extractor_image_digest" {
  description = "Immutable digest (sha256:...) of the extractor container image to deploy"
  type        = string
  validation {
    condition     = can(regex("^sha256:[a-f0-9]{64}$", var.extractor_image_digest))
    error_message = "extractor_image_digest must be a sha256 digest, e.g. sha256:abc...123."
  }
}
```

The Lambda resource references it as `image_uri = "${data.aws_ecr_repository.extractor.repository_url}@${var.extractor_image_digest}"`. Tag presence is incidental; the digest is the contract.

### Build-and-push pipeline

CI gains a `build-and-push` job that runs before `apply` on the service stack, gated on changes under `src/extractor/**` (and the workflow file itself). Service-only changes — Terraform tweaks, IAM tightening — re-apply with the previously-deployed digest and skip the Docker work entirely.

The job builds the image from `src/extractor`, tags it with the short commit SHA, pushes to the env repository, then resolves the resulting digest via `aws ecr describe-images` and publishes it as a job output. The downstream `apply` job consumes that output as `-var="extractor_image_digest=…"`. The digest, not the tag, is the contract between CI and Terraform; the tag exists only to give the push a human-readable handle.

The dev role's `PowerUserAccess` already covers `ecr:*`, so no IAM change is needed for CI to push. Local development uses the same role via the `local` profile, so a developer can run `make registry-apply ENV=local && docker push …` to iterate.

### `make` targets

The Makefile gains a parallel set to the existing IAM targets:

```
registry-init     ## Initialize Terraform backend for the registry stack
registry-plan     ## Preview changes to the registry stack
registry-apply    ## Apply the registry stack for ENV
registry-destroy  ## Destroy the registry stack for ENV (requires explicit ENV; refuses prod unless I_KNOW=1)
```

`make destroy` for the service stack does not touch the registry. `registry-destroy` is a separate, explicit target — carrying the same guards as `destroy` (explicit `ENV` required, prod blocked unless `I_KNOW=1`, backend-mismatch check) so the raw `terraform -chdir=infra/registry destroy` command, which bypasses those guards, is never the intended path.

### Module responsibilities

| Module / Stack       | Responsibility for the image lifecycle                                                                            |
| -------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `infra/registry/`    | Provision the ECR repository, lifecycle policy, repository policy, scan-on-push. Output `repository_url`.         |
| CI `build-and-push`  | Build the image from `src/extractor/`, push to the env repository, publish the resulting digest as a job output. |
| `infra/` (extractor) | Read `extractor_image_digest` tfvar, look up the repository URL via `data` source, wire the Lambda.               |
| `iam/`               | Continues to grant `PowerUserAccess` to the deploy roles; no ECR-specific policy needed.                          |

## Consequences

Positive:

- The registry stack and the service stack have separate lifecycles, matching their actual operational cadence (the repository is rebuilt approximately never; the service is re-applied per PR).
- Digest pinning makes plans deterministic and rollbacks a one-line tfvar revert.
- Tag immutability + digest pinning makes silent image swap a non-class of bug.
- Lifecycle policy bounds storage cost without a manual cleanup job.
- Repository policy with `aws:SourceArn` closes the confused-deputy class on the pull side, mirroring the queue's posture on the SQS-send side.
- Build-and-push is gated by path filter, so service-only PRs do not pay for a Docker rebuild.

Negative:

- One additional Terraform stack per env (three new state files), with the operational overhead of `init`/`plan`/`apply` cycles a contributor has to learn.
- A first-time setup per env is now `iam-apply` → `registry-apply` → `apply`, three steps instead of two. Documented in `CONTRIBUTING.md`.
- The service stack now depends on a `data "aws_ecr_repository"` lookup, which fails loudly if the registry stack has not been applied. The error message is clear (`couldn't find resource`) but the failure mode is new.

Neutral:

- Scan-on-push catches CVEs known at push time but not those disclosed afterward. Acceptable at portfolio scale; the upgrade path to Inspector enhanced scanning is account-wide configuration plus a `repository_filter` of `agentic-kie-deploy-*` and is non-breaking.
- AES256 over a CMK is the same posture as ADR-0004 and ADR-0007 and migrates the same way.

## Alternatives considered

- **Module in the main service stack with targeted bootstrap apply.** Rejected — coupling a build-time durable to deploy-time state means every service plan re-evaluates the repository, and the targeted-apply step is a recurring papercut for new environments and new contributors.
- **Terraform-driven Docker push of a placeholder image.** Rejected — places Docker on every Terraform runner, makes plans opaque, and trades a one-time setup quirk for permanent operational complexity.
- **Tag-based image pinning instead of digest.** Rejected — strictly weaker than digest pinning and requires `IMMUTABLE` repository policy as a load-bearing safety check rather than a defense-in-depth one.
- **Mutable `${env}-latest` tag with Lambda update on every push.** Rejected — incompatible with `IMMUTABLE`, loses replay determinism, and conflates "the image we pushed" with "the image deployed."
- **Enhanced scanning via Inspector.** Deferred — the right answer at production scale, but its account-wide configuration model does not fit a per-env registry stack and the per-image cost is disproportionate for portfolio scale. Re-evaluate when this project moves beyond portfolio status.
- **CMK encryption on the repository.** Deferred — same reasoning as ADR-0004 and ADR-0007. Re-evaluate before real PII enters the build pipeline (e.g. fixtures, baked-in test data).
- **One shared registry across environments, scoped by tag.** Rejected — breaks the `Environment` tag-deny IAM guard and conflates blast radii. A bad image in dev should be unable to land in prod by construction, not by tag hygiene.
- **No lifecycle policy.** Rejected for symmetry with the storage module's expiration rule and the queue's bounded retention; unbounded ECR growth has no upside.
