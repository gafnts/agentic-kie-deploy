# Contributing

## Prerequisites

- [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.10
- [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html) configured with credentials

## Getting started

1. **Bootstrap the remote backend** (one-time per environment):

   ```bash
   make bootstrap
   ```

   This creates a private, encrypted, versioned S3 bucket for Terraform state and writes a local `backend.tfbackend` file with the connection details.

2. **Initialize Terraform:**

   ```bash
   terraform init -backend-config=backend.tfbackend
   ```

3. **Plan and apply:**

   ```bash
   terraform plan
   terraform apply
   ```

## Notes

- `backend.tfbackend` is gitignored (never commit it). Re-run `make bootstrap` to regenerate it.
- State locking uses S3 native locking (`use_lockfile = true`). No DynamoDB table is required.
