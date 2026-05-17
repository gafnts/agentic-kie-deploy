variable "topic_name" {
  description = "Name of the SNS topic that CloudWatch alarms publish to. One topic per environment."
  type        = string
}

variable "email_endpoint" {
  description = "Email address subscribed to the alarm topic. Null disables the subscription; the topic still exists and alarms still fire, they just route nowhere. Subscriptions require manual confirmation from the recipient's inbox before delivery starts."
  type        = string
  default     = null
}

variable "environment" {
  description = "Deployment environment. Surfaces on the resource tag the iam/ stack's DenyTouchingOtherEnvs guard reads."
  type        = string
}
