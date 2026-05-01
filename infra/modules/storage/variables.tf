variable "bucket_name" {
  description = "Name of the ingestion S3 bucket"
  type        = string
}

variable "allowed_upload_origins" {
  description = "Origins allowed to make cross-origin PUT requests (CORS)"
  type        = list(string)
}
