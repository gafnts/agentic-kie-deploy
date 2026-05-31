variable "function_name" {
  description = "Name of the publisher Lambda function. Also used as the prefix for the execution role, log group, DLQ, and function-level alarms."
  type        = string
}

variable "source_table_stream_arn" {
  description = "ARN of the source DynamoDB table's NEW_IMAGE stream. The publisher's event source mapping subscribes to it; the execution role's StreamRead statement is scoped to it."
  type        = string
}

variable "analytics_bucket_name" {
  description = "Name of the analytics (extractions) bucket the publisher writes result objects to. Passed to the Lambda as the ANALYTICS_BUCKET_NAME env var."
  type        = string
}

variable "analytics_bucket_arn" {
  description = "ARN of the analytics bucket. Scopes the publisher's s3:PutObject grant to the bucket's results_prefix partition; the bucket itself is owned by the analytics module."
  type        = string
}

variable "results_prefix" {
  description = "Top-level S3 prefix (the partition root) under which result objects live, e.g. extractions. Single-sourced at the root: threaded to the Lambda as RESULTS_PREFIX and into the s3:PutObject IAM scope so the write path cannot drift from the analytics read path."
  type        = string
}

variable "timeout_seconds" {
  description = "Function timeout. Defaults to 30s. Generous for one S3 PutObject per record across a 100-record batch."
  type        = number
  default     = 30
}

variable "memory_mb" {
  description = "Function memory allocation in MB."
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
  description = "Lambda managed runtime for the zip-packaged publisher."
  type        = string
  default     = "python3.13"
}

variable "stream_batch_size" {
  description = "Maximum stream records per publisher invocation. Stream records are tiny and the consumer does one S3 PUT per record, so batching amortizes invocation overhead."
  type        = number
  default     = 100
}

variable "stream_batching_window_seconds" {
  description = "Maximum seconds to wait filling a batch. Dominates result-delivery tail latency; 5s balances delivery latency against invocation frequency."
  type        = number
  default     = 5
}

variable "stream_retry_attempts" {
  description = "Retries before a failed batch lands in the DLQ. Mirrors the extractor's maxReceiveCount=3 for retry-budget symmetry across the pipeline."
  type        = number
  default     = 3
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention in days for the publisher's log group."
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
