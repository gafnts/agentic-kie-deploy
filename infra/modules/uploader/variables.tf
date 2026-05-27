variable "name" {
  description = "Base name for the module's resources (Lambda function, HTTP API, execution role, log groups, alarms). All sub-resources are derived from this name with stable suffixes."
  type        = string
}

variable "ingestion_bucket_arn" {
  description = "ARN of the S3 ingestion bucket. The signed URL inherits the role's s3:PutObject grant on this bucket's uploads/ prefix."
  type        = string
}

variable "ingestion_bucket_name" {
  description = "Name of the S3 ingestion bucket. Passed to the Lambda as INGESTION_BUCKET_NAME for the generate_presigned_url call."
  type        = string
}

variable "url_ttl_seconds" {
  description = "Lifetime of the pre-signed PUT URL. Defaults to 600s (long enough for retries and slow networks, short enough that a leaked URL is useless within an hour)."
  type        = number
  default     = 600
  validation {
    condition     = var.url_ttl_seconds >= 60 && var.url_ttl_seconds <= 3600
    error_message = "url_ttl_seconds must be between 60 and 3600 (1 minute to 1 hour)."
  }
}

variable "timeout_seconds" {
  description = "Function timeout. Defaults to 5s — the function does one generate_presigned_url call."
  type        = number
  default     = 5
}

variable "memory_mb" {
  description = "Function memory allocation in MB. Defaults to 256MB — boto3 import plus one signing call."
  type        = number
  default     = 256
}

variable "architecture" {
  description = "Function CPU architecture."
  type        = string
  default     = "arm64"
  validation {
    condition     = contains(["arm64", "x86_64"], var.architecture)
    error_message = "architecture must be arm64 or x86_64."
  }
}

variable "runtime" {
  description = "Lambda managed runtime for the zip-packaged presigner."
  type        = string
  default     = "python3.13"
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention in days for the function's log group."
  type        = number
  default     = 14
}

variable "environment" {
  description = "Deployment environment (local, staging, prod). Used for the Environment tag the iam/ stack's DenyTouchingOtherEnvs guard reads."
  type        = string
}

variable "alarm_topic_arn" {
  description = "ARN of the SNS topic that function-level CloudWatch alarms publish to. The topic lives in the alarms module so the alerting plane is one resource per env."
  type        = string
}
