variable "name" {
  description = "Base name for the extraction queue and its DLQ"
  type        = string
}

variable "source_bucket_name" {
  description = "Name of the S3 bucket whose Object Created events feed the queue"
  type        = string
}

variable "lambda_timeout_seconds" {
  description = "Timeout of the consumer Lambda. The queue's visibility timeout is derived as 6x this value, per AWS guidance, so the two cannot drift."
  type        = number
  default     = 60
}

variable "max_receive_count" {
  description = "Number of receives before a message is moved to the DLQ"
  type        = number
  default     = 3
}
