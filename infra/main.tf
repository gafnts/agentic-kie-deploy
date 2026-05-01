provider "aws" {
  region = var.aws_region
}

resource "random_id" "bucket_suffix" {
  byte_length = 4
}

module "storage" {
  source                 = "./modules/storage"
  bucket_name            = "${var.project_name}-${var.environment}-ingestion-${random_id.bucket_suffix.hex}"
  allowed_upload_origins = var.allowed_upload_origins
}
