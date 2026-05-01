.DEFAULT_GOAL := help

ENV ?= local
TF  := terraform -chdir=infra
VARS    := -var-file=envs/$(ENV).tfvars
BACKEND := -backend-config=envs/$(ENV).backend.tfbackend

.PHONY: help bootstrap init plan apply destroy format

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'

bootstrap: ## Create state bucket and write backend files for all envs
	@bash bootstrap.sh

init: ## terraform init for ENV
	$(TF) init -reconfigure $(BACKEND)

plan: ## terraform plan for ENV
	$(TF) plan $(VARS)

apply: ## terraform apply for ENV (refuses prod by default)
	@if [ "$(ENV)" = "prod" ] && [ "$(I_KNOW)" != "1" ]; then \
		echo "Refusing to apply prod from local. CI owns prod."; exit 1; fi
	$(TF) apply $(VARS)

destroy: ## terraform destroy for ENV (refuses prod by default)
	@if [ "$(ENV)" = "prod" ] && [ "$(I_KNOW)" != "1" ]; then \
		echo "Refusing to destroy prod. Re-run with I_KNOW=1."; exit 1; fi
	$(TF) destroy $(VARS)

format: ## Format Terraform code
	$(TF) fmt -recursive
