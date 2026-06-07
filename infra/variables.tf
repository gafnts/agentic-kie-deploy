variable "project_name" {
  type    = string
  default = "agentic-kie-deploy"
}

variable "environment" {
  description = "One of: local, staging, prod. Gates deletion protection, log retention, and concurrency limits."
  type        = string
  default     = "staging"
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "url_ttl_seconds" {
  description = "Lifetime of the upload pre-signed URL the uploader module hands out. Tightening or loosening is a tfvar change per env with no other coordination."
  type        = number
  default     = 600
}

variable "extractor_image_digest" {
  description = "Immutable digest of the extractor container image to deploy. Injected by CI from the build-and-push job output."
  type        = string
  validation {
    condition     = can(regex("^sha256:[a-f0-9]{64}$", var.extractor_image_digest))
    error_message = "extractor_image_digest must be a sha256 digest, e.g. sha256:abc...123."
  }
}

variable "llm_model" {
  description = "LLM identifier passed to the extractor Lambda."
  type        = string
  default     = "gemini-3-flash-preview"
}

variable "extractor_flavor" {
  description = "Which agentic-kie extraction strategy the extractor runs. 'single_pass' issues one structured LLM call; 'agentic' runs a ReAct loop over the document. Selectable per environment at deploy time; drives the whole parameter profile (max_iterations, maxReceiveCount) so re-parametrization is a one-variable flip."
  type        = string
  default     = "single_pass"
  validation {
    condition     = contains(["single_pass", "agentic"], var.extractor_flavor)
    error_message = "extractor_flavor must be 'single_pass' or 'agentic'."
  }
}

variable "alarm_email" {
  description = "Email address subscribed to the alarm SNS topic. Leave null to skip the subscription (alarms still fire in CloudWatch, they just don't notify anyone). The recipient must confirm the subscription from their inbox before delivery starts."
  type        = string
  default     = null
}
