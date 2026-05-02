# Contributing

## Prerequisites

- [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.15
- [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html) configured with credentials

## Make targets

The Makefile is the entry point for every common operation, locally and in CI. All targets accept `ENV={local,dev,prod}` (defaults to `local`).

| Target | Purpose |
|---|---|
| `make bootstrap` | Create the shared state bucket, write backend files for every environment, and generate `infra/iam/iam.tfvars` from the current AWS caller identity (one-time, admin credentials) |
| `make backend` | Regenerate `infra/envs/*.backend.tfbackend` without touching AWS (used by CI on every job) |
| `make iam-init` | `terraform init` for the IAM bootstrap module |
| `make iam-plan` | Preview changes to the IAM bootstrap module |
| `make iam-apply` | Apply the IAM bootstrap module — creates the three deploy roles (one-time, admin credentials) |
| `make init ENV=…` | `terraform init` against the env's backend config |
| `make plan ENV=…` | Preview changes for the env |
| `make apply ENV=…` | Apply changes for the env (refuses `prod` unless `I_KNOW=1`) |
| `make ci-plan ENV=…` | Plan and save to `tfplan.<env>` (used by CI to hand a saved plan to the apply job) |
| `make ci-apply ENV=…` | Apply a saved `tfplan.<env>` (used by CI; same prod guard) |
| `make destroy ENV=…` | Destroy the env (refuses `prod` unless `I_KNOW=1`) |
| `make format` | `terraform fmt -recursive` |

State locking uses S3 native locking (`use_lockfile = true`); no DynamoDB table is required. Backend files are derived from the project name (see `bootstrap-backend.sh`) and are gitignored — never commit them.

## DevOps strategy

Each environment (`local`, `dev`, `prod`) has its own least-privileged deploy role, its own state prefix, and its own apply path. All three share the same Makefile interface.

### Three environments × three concerns

|  | local | dev | prod |
|---|---|---|---|
| **Identity** | IAM user assumes `agentic-kie-local-deploy` | OIDC → `agentic-kie-dev-deploy` (trust scoped to `refs/heads/develop` + PRs) | OIDC → `agentic-kie-prod-deploy` (trust scoped to `environment:prod`) |
| **State** | `service/local/...` in the shared bucket | `service/dev/...` | `service/prod/...` |
| **Apply path** | `make apply` from your laptop | auto-apply on merge to `develop` | saved-plan apply, manual approval via the `prod` GitHub Environment |

Trust policies are defined in [`infra/iam/main.tf`](infra/iam/main.tf). Each role can only be assumed by its designated principal. The `deny_other_envs` policy combined with `Environment` resource tagging prevents a role in one environment from modifying resources tagged to another.

### Bootstrap (one-time, with admin credentials)

Two resources must exist before anything else can run. Execute these once per AWS account:

1. **State backend + IAM tfvars** — creates the private, versioned, encrypted S3 bucket, writes `infra/envs/*.backend.tfbackend` for every environment, and generates `infra/iam/iam.tfvars` populated from the current AWS caller identity (`local_principal_arn`) and the bucket name (`state_bucket_name`). Idempotent: re-running skips files that already exist.

   ```bash
   make bootstrap
   ```

2. **Deploy roles** — creates the three assumable roles (`local`, `dev`, `prod`) and their trust/permission policies:

   ```bash
   make iam-init
   make iam-apply

   terraform -chdir=infra/iam output dev_role_arn    # paste into GitHub vars.AWS_ROLE_ARN_DEV
   terraform -chdir=infra/iam output prod_role_arn   # paste into GitHub vars.AWS_ROLE_ARN_PROD
   terraform -chdir=infra/iam output local_role_arn  # configure in your ~/.aws/config
   ```

After bootstrap, all local and CI operations run through their respective assumable roles.

### Local role usage

Add a profile to `~/.aws/config` to assume the `local` role:

```ini
[profile agentic-kie-local]
role_arn       = arn:aws:iam::<account-id>:role/agentic-kie-local-deploy
source_profile = default
region         = us-east-1
```

Then prefix Make targets with the profile:

```bash
AWS_PROFILE=agentic-kie-local make init
AWS_PROFILE=agentic-kie-local make plan
AWS_PROFILE=agentic-kie-local make apply
```

### Delivery pipelines

Two workflows under [`.github/workflows/`](.github/workflows/), gated to changes in `infra/**`:

- **`deploy-dev.yml`** (`develop`): on PR, runs `fmt`/`validate`/`plan` and posts the plan as a sticky PR comment. On merge, auto-applies.
- **`deploy-prod.yml`** (`main`): on PR, posts a plan comment. On merge, generates a saved plan via `make ci-plan ENV=prod` and uploads `tfplan.prod` as an artifact. The `apply` job is gated by the `prod` GitHub Environment (manual approval) and applies the saved plan via `make ci-apply`.

Both workflows authenticate via OIDC and use a per-environment `concurrency` group to prevent concurrent deploys against the same Terraform state.

### Design notes

- **State bucket and IAM roles** are the only resources provisioned with admin credentials. All subsequent operations use the scoped deploy roles.
- **Backend files** are generated deterministically by `bootstrap-backend.sh` from the project name. CI regenerates them on every job; locally they are generated once. They are gitignored and contain no secrets.
- **`make plan` / `make apply`** behave identically locally and in CI. The only differences are the `AWS_PROFILE` value and the `I_KNOW=1` flag required for prod.
