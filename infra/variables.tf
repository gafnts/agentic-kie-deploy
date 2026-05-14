variable "aws_region" {
  description = "AWS region where resources will be deployed"
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project name used for resource naming"
  type        = string
  default     = "agentic-kie-deploy"
}

variable "environment" {
  description = "Deployment environment"
  type        = string
  default     = "dev"
}

variable "allowed_upload_origins" {
  description = "Origins allowed to make cross-origin PUT requests to the ingestion bucket"
  type        = list(string)
  default     = ["https://gabriel.com.gt"]
}

variable "extractor_image_digest" {
  description = "Immutable digest of the extractor container image to deploy. Injected by CI from the build-and-push job output."
  type        = string
  validation {
    condition     = can(regex("^sha256:[a-f0-9]{64}$", var.extractor_image_digest))
    error_message = "extractor_image_digest must be a sha256 digest, e.g. sha256:abc...123."
  }
}
