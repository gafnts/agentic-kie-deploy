# Contributing

> [!IMPORTANT]
> This project requires:
> - [Terraform](https://developer.hashicorp.com/terraform/install) ~> 1.15.0
> - [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html) configured with credentials
> - [GitHub OIDC provider](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws) configured in your AWS account

> [!NOTE]
> Check if your AWS account already has a GitHub OIDC provider configured: `aws iam list-open-id-connect-providers`. If it's not there, create it once (`token.actions.githubusercontent.com`, audience `sts.amazonaws.com`). The IAM module references it but doesn't create it.

## DevOps strategy

### Environment model

The project has three deployment environments, all in the same AWS account:

| Environment | Who deploys | When |
|---|---|---|
| `local` | You, from your laptop | Iterating on infrastructure changes |
| `dev` | GitHub Actions | On merge to `develop` |
| `prod` | GitHub Actions | On merge to `main`, gated by manual approval |

> [!NOTE]
> Each environment has its own Terraform state file, its own IAM role, and its own set of resources tagged with `Environment=<env>`. The IAM roles are scoped so each one can only touch resources tagged for its own environment.

### Branch model

Two long-lived branches map to the two CI-managed environments: `develop` drives `dev`, `main` drives `prod`. Every change flows through a PR with a plan attached, and prod additionally waits on a manual approval before the saved plan is applied.

```mermaid
flowchart LR
    feature[Feature branch] -->|PR| develop[develop]
    develop -->|CI plans dev| planDev{{Plan dev}}
    planDev -->|merge| applyDev[CI applies dev]

    develop -->|PR| main[main]
    main -->|CI plans prod| planProd{{Plan prod}}
    planProd -->|merge| savedPlan[CI saves plan]
    savedPlan --> approval[/Manual approval/]
    approval --> applyProd[CI applies prod]
```

## First-time setup

You only do it once per AWS account.

### 1. Bootstrap the remote state backend

Creates the S3 bucket that holds Terraform state for all three environments, the four `*.backend.tfbackend` config files (one per env, plus one for the IAM bootstrap), and `infra/iam/iam.tfvars` (gitignored) pre-populated with your caller ARN and bucket name:

```bash
make bootstrap
```

The bucket is private, versioned, encrypted, and uses S3 native locking (`use_lockfile = true`). No DynamoDB table required. The bootstrap script is idempotent.

### 2. Create the IAM roles

The three deploy roles (`local`, `dev`, `prod`) live in a separate Terraform root at `infra/iam/`. They're applied once with admin credentials and rarely touched afterward.

```bash
make iam-init && make iam-apply
```

The output gives you three role ARNs. Keep them — you'll paste two into GitHub and one into your AWS config.

### 3. Configure GitHub

In the repo settings:

**Settings → Environments → New environment → `prod`**
- Add yourself as a required reviewer.
- This is what gates the prod apply step.

**Settings → Secrets and variables → Actions → Variables (Repository tab)**
- `AWS_ROLE_ARN_DEV` = `<dev_role_arn>` from the Terraform output
- `AWS_ROLE_ARN_PROD` = `<prod_role_arn>` from the Terraform output

Variables (not secrets) is correct since role ARNs aren't sensitive on their own.

### 4. Configure your local AWS profile

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

Always set `AWS_PROFILE=agentic-kie-local` (or export it once per shell session).

```bash
export AWS_PROFILE=agentic-kie-local

make init      # initialize the local backend (idempotent, safe to re-run)
make plan      # preview changes
make apply     # apply changes
make destroy   # tear down all local resources
```

`make` defaults to `ENV=local`. The Makefile refuses to apply or destroy `prod` unless `I_KNOW=1` — only CI is allowed to set that.

### Opening a PR

Branch from `develop`, push, open a PR targeting `develop`:

```bash
git checkout develop
git pull
git checkout -b feature/my-change
# ... edit ...
git push -u origin feature/my-change
```

CI runs the dev workflow. Within a minute the PR gets a sticky comment titled **"Terraform Plan · `dev`"** showing what would be applied. Review the plan as part of code review.

Merge the PR. CI applies the changes to dev automatically.

### Promoting to prod

Open a PR from `develop` to `main`. CI posts a sticky **"Terraform Plan · `prod`"** comment. Review and merge.

After the merge:

1. CI runs the `plan` job, generates a saved plan, uploads it as a workflow artifact.
2. CI queues the `apply` job, which waits at the prod environment approval gate.
3. You get notified. Open the workflow run, review the plan in the previous job's logs, click "Review deployments" → Approve.
4. CI applies the saved plan. The exact same bytes that were generated in step 1.

If the plan looks wrong at the approval gate, reject it. Nothing is applied.

### Adding new infrastructure

Most changes are app-level — new modules in `infra/modules/`, wired into `infra/main.tf`. The IAM roles already have `PowerUserAccess`, so they cover almost any AWS service you'd add. The deploy flow is unchanged.

You only need to touch `infra/iam/` when:

- Adding a new IAM-related resource pattern that needs explicit allow (rare).
- Tightening the permissions policy from `PowerUserAccess` to a service-specific allowlist.
- Adding a new environment.

## Reference

### Make targets

| Target | Description |
|---|---|
| `make bootstrap` | Create state bucket and write backend files (one-time, run once) |
| `make backend` | Regenerate backend files only, no AWS calls (used by CI and after fresh clone) |
| `make init` | Initialize Terraform backend for `ENV` |
| `make plan` | Preview infrastructure changes for `ENV` |
| `make ci-plan` | Preview changes and save plan to `tfplan.<env>` (used by CI) |
| `make apply` | Apply infrastructure changes for `ENV` (refuses prod unless `I_KNOW=1`) |
| `make ci-apply` | Apply saved plan `tfplan.<env>` (used by CI) |
| `make destroy` | Destroy all infrastructure for `ENV` (refuses prod unless `I_KNOW=1`) |
| `make format` | Format all Terraform files |

`ENV` defaults to `local`. Override with `make plan ENV=dev` etc.

### Files that are gitignored

- `infra/envs/*.backend.tfbackend` — generated by `make bootstrap` / `make backend`
- `infra/iam/backend.tfbackend` — same
- `infra/iam/iam.tfvars` — contains your principal ARN
- `infra/tfplan.*` — saved plan binaries
- `plan.txt` — captured plan output for CI comments





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
