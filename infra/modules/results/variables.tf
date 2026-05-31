variable "function_name" {
  description = "Name of the publisher Lambda function. Also used as the prefix for the execution role, log group, DLQ, and function-level alarms."
  type        = string
}

variable "bucket_name" {
  description = "Name of the analytics (extractions) S3 bucket the publisher writes result objects to and Athena queries. The Athena query-results bucket derives from this name with an athena-results suffix."
  type        = string
}

variable "project_name" {
  description = "Project name. Composed with environment into the Glue database name (project_environment_results) and Athena workgroup name (project-environment-results)."
  type        = string
}

variable "source_table_stream_arn" {
  description = "ARN of the source DynamoDB table's NEW_IMAGE stream. The publisher's event source mapping subscribes to it; the execution role's StreamRead statement is scoped to it."
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

variable "athena_bytes_scanned_cutoff" {
  description = "Per-query data-scan cap on the workgroup, in bytes. The workgroup is a budget boundary: Athena bills per TB scanned. Defaults to 1 GiB; raising it is an opt-in."
  type        = number
  default     = 1073741824 # 1 GiB
}

variable "athena_results_expiration_days" {
  description = "Lifecycle expiration for Athena query results. Query results are debugging artifacts, not durable data, so they expire quickly."
  type        = number
  default     = 7
}

variable "noncurrent_version_expiration_days" {
  description = "Days after which noncurrent object versions expire on the analytics bucket. Bounds versioning storage cost without losing the recovery window."
  type        = number
  default     = 30
}

variable "access_log_expiration_days" {
  description = "Days after which access log objects expire on the results-logs bucket. Logs are operational artifacts, not durable records."
  type        = number
  default     = 90
}

variable "force_destroy" {
  description = "Allow non-empty buckets (and the workgroup's query history) to be destroyed. Set true in non-prod so `make destroy` does not strand state."
  type        = bool
  default     = false
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
