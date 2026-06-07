variable "function_name" {
  description = "Name of the extractor Lambda function. Also used as the prefix for the execution role and log group."
  type        = string
}

variable "image_uri" {
  description = "Fully qualified container image URI including digest (e.g. <repo_url>@sha256:...)"
  type        = string
  validation {
    condition     = can(regex("@sha256:[a-f0-9]{64}$", var.image_uri))
    error_message = "image_uri must be digest-pinned (end in @sha256:<64 hex>); tag-only references are not allowed."
  }
}

variable "timeout_seconds" {
  description = "Function timeout. The queue's visibility timeout is derived as 6x this value."
  type        = number
  default     = 120
}

variable "memory_mb" {
  description = "Function memory allocation in MB. vCPU is allocated proportionally."
  type        = number
  default     = 2048
}

variable "ephemeral_storage_mb" {
  description = "Function /tmp storage in MB (512 - 10240)."
  type        = number
  default     = 2048
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

variable "max_concurrency" {
  description = "Maximum concurrent Lambda invocations driven by the SQS event source mapping. Caps parallel LLM fan-out under an ingestion burst."
  type        = number
  default     = 10
  validation {
    condition     = var.max_concurrency >= 2 && var.max_concurrency <= 1000
    error_message = "max_concurrency must be between 2 and 1000 (SQS event source mapping limits)."
  }
}

variable "queue_arn" {
  description = "ARN of the SQS extraction queue that triggers the Lambda."
  type        = string
}

variable "queue_max_receive_count" {
  description = "maxReceiveCount of the SQS extraction queue. Passed to the Lambda as SQS_MAX_RECEIVE_COUNT so it can gate the terminal failed write to the final delivery attempt."
  type        = number
  default     = 3
}

variable "ingestion_bucket_arn" {
  description = "ARN of the S3 ingestion bucket the Lambda reads source objects from."
  type        = string
}

variable "results_table_arn" {
  description = "ARN of the DynamoDB results table the Lambda writes structured answers to."
  type        = string
}

variable "results_table_name" {
  description = "Name of the DynamoDB results table. Passed to the Lambda as RESULTS_TABLE_NAME for boto3 calls (non-secret operational metadata)."
  type        = string
}

variable "llm_model" {
  description = "LLM identifier passed to the extractor Lambda."
  type        = string
  default     = "gemini-3-flash-preview"
}

variable "llm_provider_secret_arn" {
  description = "ARN of the Secrets Manager secret holding the LLM provider API key."
  type        = string
}

variable "langsmith_secret_arn" {
  description = "ARN of the Secrets Manager secret holding the LangSmith API key."
  type        = string
}

variable "langsmith_project" {
  description = "LangSmith project name. Composed at the root from project_name + environment."
  type        = string
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
