.DEFAULT_GOAL := help

ENV ?= local
TF      := terraform -chdir=infra
VARS    := -var-file=envs/$(ENV).tfvars
BACKEND := -backend-config=envs/$(ENV).backend.tfbackend

IAM_TF      := terraform -chdir=infra/iam
IAM_VARS    := -var-file=iam.tfvars
IAM_BACKEND := -backend-config=backend.tfbackend

.PHONY: help install tflint-init check lint format type test smoke \
        bootstrap backend \
        iam-init iam-plan iam-apply \
        init plan ci-plan apply ci-apply destroy tf-format lock \
        _check-backend

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'


# LOCAL DEVELOPMENT SETUP

install: ## Sync deps, install pre-commit hooks (both stages), install tflint plugins
	uv sync --all-groups
	uv run pre-commit install
	uv run pre-commit install --hook-type pre-push
	tflint --init

tflint-init: ## Refresh tflint plugins after a .tflint.hcl version bump
	tflint --init


# QUALITY GATES

check: ## Run every pre-commit hook against every file (both stages)
	uv run pre-commit run --all-files --hook-stage pre-commit
	uv run pre-commit run --all-files --hook-stage pre-push

lint: ## Run ruff check on src
	uv run ruff check src

format: ## Apply ruff lint fixes and formatting to src
	uv run ruff check src --fix
	uv run ruff format src

type: ## Run mypy on src
	uv run mypy src

test: ## Run pytest with coverage
	uv run pytest --cov --cov-report=term-missing

smoke: ## Run end-to-end smoke tests against the deployed ENV (requires terraform outputs)
	@bash tests/queue.sh


# STATE BACKEND BOOTSTRAP

bootstrap: ## Create state bucket and write backend files for all environments
	@bash bootstrap.sh

backend: ## Write backend files for all environments (used by CI; no AWS calls)
	@bash bootstrap-backend.sh


# IAM BOOTSTRAP MODULE

iam-init: ## Initialize Terraform backend for the IAM bootstrap module
	$(IAM_TF) init -reconfigure $(IAM_BACKEND)

iam-plan: ## Preview changes to the IAM bootstrap module
	$(IAM_TF) plan $(IAM_VARS)

iam-apply: ## Apply the IAM bootstrap module (creates deploy roles)
	$(IAM_TF) apply $(IAM_VARS)


# TERRAFORM LIFECYCLE

init: ## Initialize Terraform backend for ENV
	$(TF) init -reconfigure $(BACKEND)

plan: ## Preview infrastructure changes for ENV
	$(TF) plan $(VARS)

ci-plan: ## Preview changes and save plan to tfplan.ENV (used by CI)
	$(TF) plan -out=tfplan.$(ENV) $(VARS)

apply: _check-backend ## Apply infrastructure changes for ENV (refuses prod unless I_KNOW=1)
	@if [ "$(ENV)" = "prod" ] && [ "$(I_KNOW)" != "1" ]; then \
		echo "Refusing to apply prod from local. CI owns prod."; exit 1; fi
	$(TF) apply $(VARS)

ci-apply: _check-backend ## Apply saved plan tfplan.ENV (used by CI; refuses prod unless I_KNOW=1)
	@if [ "$(ENV)" = "prod" ] && [ "$(I_KNOW)" != "1" ]; then \
		echo "Refusing to apply prod from local. CI owns prod."; exit 1; fi
	$(TF) apply tfplan.$(ENV)

destroy: _check-backend ## Destroy all infrastructure for ENV (requires explicit ENV; refuses prod unless I_KNOW=1)
	@if [ "$(origin ENV)" != "command line" ] && [ "$(origin ENV)" != "environment" ]; then \
		echo "destroy requires explicit ENV (e.g. make destroy ENV=local). Refusing default."; exit 1; fi
	@if [ "$(ENV)" = "prod" ] && [ "$(I_KNOW)" != "1" ]; then \
		echo "Refusing to destroy prod. Re-run with I_KNOW=1."; exit 1; fi
	$(TF) destroy $(VARS)

# Verify the configured backend key matches ENV. Prevents the footgun where
# `make init ENV=prod` followed by `make destroy` (defaulting to local)
# operates on the prod state because the backend pointer persists in
# infra/.terraform/terraform.tfstate across runs.
_check-backend:
	@if [ ! -f infra/.terraform/terraform.tfstate ]; then \
		echo "Terraform not initialized. Run 'make init ENV=$(ENV)' first."; exit 1; fi
	@current=$$(grep -o '"key": *"[^"]*"' infra/.terraform/terraform.tfstate | head -1 | sed 's/.*"\([^"]*\)"$$/\1/'); \
	expected="service/$(ENV)/terraform.tfstate"; \
	if [ "$$current" != "$$expected" ]; then \
		echo "Backend mismatch: configured key is '$$current' but ENV=$(ENV) expects '$$expected'."; \
		echo "Run 'make init ENV=<env>' to reconfigure the backend before continuing."; \
		exit 1; \
	fi

tf-format: ## Format all Terraform files
	$(TF) fmt -recursive

lock: ## Regenerate .terraform.lock.hcl for linux_amd64 + darwin (arm64/amd64) in all modules
	@find infra -name ".terraform.lock.hcl" -exec dirname {} \; | \
		xargs -I{} terraform -chdir={} providers lock \
		-platform=linux_amd64 -platform=darwin_amd64 -platform=darwin_arm64
