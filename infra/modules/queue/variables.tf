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
}

variable "max_receive_count" {
  description = "Number of receives before a message is moved to the DLQ"
  type        = number
  default     = 3
}

variable "alarm_topic_arn" {
  description = "ARN of the SNS topic that the DLQ depth alarm publishes to. The topic lives in the alarms module so the alerting plane is one resource per env."
  type        = string
}

variable "environment" {
  description = "Deployment environment. Surfaces on the alarm tag the iam/ stack's DenyTouchingOtherEnvs guard reads."
  type        = string
}
