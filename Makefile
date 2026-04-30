.DEFAULT_GOAL := help

.PHONY: help bootstrap

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'

bootstrap: ## Create S3 state bucket and DynamoDB lock table
	@bash bootstrap.sh
