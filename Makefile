.DEFAULT_GOAL := help

TF := terraform -chdir=infra

.PHONY: help bootstrap init plan apply destroy

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'

bootstrap: ## Create S3 state bucket and write backend.tfbackend
	@bash bootstrap.sh

init: ## Initialize Terraform (requires bootstrap first)
	$(TF) init -backend-config=backend.tfbackend

format: ## Format Terraform code
	$(TF) fmt -recursive

plan: ## Preview infrastructure changes
	$(TF) plan

apply: ## Apply infrastructure changes
	$(TF) apply

destroy: ## Destroy all infrastructure
	$(TF) destroy
