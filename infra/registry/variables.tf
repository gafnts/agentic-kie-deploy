variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "project_name" {
  description = "Project name prefix used for resource naming. Must match across all stacks."
  type        = string
  default     = "agentic-kie-deploy"
}

variable "environment" {
  description = "Deployment environment (local, staging, prod)"
  type        = string
  validation {
    condition     = contains(["local", "staging", "prod"], var.environment)
    error_message = "environment must be one of: local, staging, prod."
  }
}
