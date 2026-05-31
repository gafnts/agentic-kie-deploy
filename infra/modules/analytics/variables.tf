variable "bucket_name" {
  description = "Name of the analytics (extractions) S3 bucket the publisher writes result objects to and Athena queries. The Athena query-results bucket derives from this name with an athena-results suffix."
  type        = string
}

variable "project_name" {
  description = "Project name. Composed with environment into the Glue database name (project_environment_analytics) and Athena workgroup name (project-environment-analytics)."
  type        = string
}

variable "results_prefix" {
  description = "Top-level S3 prefix (the partition root) under which result objects live, e.g. extractions. Single-sourced at the root and used here in the Glue table's storage.location and partition-projection template so the read path cannot drift from the publisher's write path."
  type        = string
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

variable "environment" {
  description = "Deployment environment (local, staging, prod). Used for the Environment tag the iam/ stack's DenyTouchingOtherEnvs guard reads."
  type        = string
}
