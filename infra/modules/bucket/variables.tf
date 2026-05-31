variable "bucket_name" {
  description = "Name of the ingestion S3 bucket"
  type        = string
}

variable "environment" {
  description = "Deployment environment (e.g. staging, prod)"
  type        = string
}

variable "force_destroy" {
  description = "Allow non-empty buckets to be destroyed"
  type        = bool
  default     = false
}
