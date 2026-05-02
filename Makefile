.DEFAULT_GOAL := help

ENV ?= local
TF      := terraform -chdir=infra
VARS    := -var-file=envs/$(ENV).tfvars
BACKEND := -backend-config=envs/$(ENV).backend.tfbackend

IAM_TF      := terraform -chdir=infra/iam
IAM_VARS    := -var-file=iam.tfvars
IAM_BACKEND := -backend-config=backend.tfbackend

.PHONY: help bootstrap backend init plan ci-plan apply ci-apply destroy format \
        iam-init iam-plan iam-apply

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'

bootstrap: ## Create state bucket and write backend files for all environments
	@bash bootstrap.sh

backend: ## Write backend files for all environments (used by CI; no AWS calls)
	@bash bootstrap-backend.sh

iam-init: ## Initialize Terraform backend for the IAM bootstrap module
	$(IAM_TF) init -reconfigure $(IAM_BACKEND)

iam-plan: ## Preview changes to the IAM bootstrap module
	$(IAM_TF) plan $(IAM_VARS)

iam-apply: ## Apply the IAM bootstrap module (creates deploy roles)
	$(IAM_TF) apply $(IAM_VARS)

init: ## Initialize Terraform backend for ENV
	$(TF) init -reconfigure $(BACKEND)

plan: ## Preview infrastructure changes for ENV
	$(TF) plan $(VARS)

ci-plan: ## Preview changes and save plan to tfplan.ENV (used by CI)
	$(TF) plan -out=tfplan.$(ENV) $(VARS)

apply: ## Apply infrastructure changes for ENV (refuses prod unless I_KNOW=1)
	@if [ "$(ENV)" = "prod" ] && [ "$(I_KNOW)" != "1" ]; then \
		echo "Refusing to apply prod from local. CI owns prod."; exit 1; fi
	$(TF) apply $(VARS)

ci-apply: ## Apply saved plan tfplan.ENV (used by CI; refuses prod unless I_KNOW=1)
	@if [ "$(ENV)" = "prod" ] && [ "$(I_KNOW)" != "1" ]; then \
		echo "Refusing to apply prod from local. CI owns prod."; exit 1; fi
	$(TF) apply tfplan.$(ENV)

destroy: ## Destroy all infrastructure for ENV (refuses prod unless I_KNOW=1)
	@if [ "$(ENV)" = "prod" ] && [ "$(I_KNOW)" != "1" ]; then \
		echo "Refusing to destroy prod. Re-run with I_KNOW=1."; exit 1; fi
	$(TF) destroy $(VARS)

format: ## Format all Terraform files
	$(TF) fmt -recursive
