variable "table_name" {
  description = "Name of the DynamoDB results table"
  type        = string
}

variable "deletion_protection_enabled" {
  description = "Whether the table is protected from accidental deletion."
  type        = bool
  default     = false
}
