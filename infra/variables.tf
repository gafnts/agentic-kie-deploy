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
  description = "Deployment environment (e.g., dev, prod)"
  type        = string
  default     = "dev"
}

variable "allowed_upload_origins" {
  description = "Origins allowed to make cross-origin PUT requests to the ingestion bucket"
  type        = list(string)
  default     = ["https://gabriel.com.gt"]
}
