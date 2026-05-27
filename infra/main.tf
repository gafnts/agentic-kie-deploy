provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}

data "aws_secretsmanager_secret" "llm_provider" {
  name = "${var.project_name}/${var.environment}/llm-provider"
}

data "aws_secretsmanager_secret" "langsmith" {
  name = "${var.project_name}/${var.environment}/langsmith"
}

data "aws_ecr_repository" "extractor" {
  name = "${var.project_name}-${var.environment}-extractor"
}

locals {
  bucket_suffix             = substr(sha256("${var.project_name}-${var.environment}-${data.aws_caller_identity.current.account_id}"), 0, 16)
  bucket_name               = "${var.project_name}-${var.environment}-ingestion-${local.bucket_suffix}"
  extractor_timeout_seconds = 120
}

module "alarms" {
  source         = "./modules/alarms"
  topic_name     = "${var.project_name}-${var.environment}-alarms"
  email_endpoint = var.alarm_email
  environment    = var.environment
}

module "bucket" {
  source                 = "./modules/bucket"
  bucket_name            = local.bucket_name
  allowed_upload_origins = var.allowed_upload_origins
  force_destroy          = var.environment != "prod"
}

module "uploader" {
  source                = "./modules/uploader"
  name                  = "${var.project_name}-${var.environment}-uploader"
  ingestion_bucket_arn  = module.bucket.bucket_arn
  ingestion_bucket_name = module.bucket.bucket_name
  url_ttl_seconds       = var.url_ttl_seconds
  log_retention_days    = var.environment == "prod" ? 30 : 14
  environment           = var.environment
  alarm_topic_arn       = module.alarms.topic_arn
}

module "queue" {
  source                 = "./modules/queue"
  name                   = "${var.project_name}-${var.environment}-extraction"
  source_bucket_name     = module.bucket.bucket_name
  lambda_timeout_seconds = local.extractor_timeout_seconds
  alarm_topic_arn        = module.alarms.topic_arn
  environment            = var.environment
}

module "table" {
  source                      = "./modules/table"
  table_name                  = "${var.project_name}-${var.environment}-results"
  deletion_protection_enabled = var.environment == "prod"
}

module "extractor" {
  source                  = "./modules/extractor"
  function_name           = "${var.project_name}-${var.environment}-extractor"
  image_uri               = "${data.aws_ecr_repository.extractor.repository_url}@${var.extractor_image_digest}"
  timeout_seconds         = local.extractor_timeout_seconds
  memory_mb               = 2048
  ephemeral_storage_mb    = 2048
  architecture            = "arm64"
  max_concurrency         = var.environment == "prod" ? 25 : 10
  queue_arn               = module.queue.queue_arn
  queue_max_receive_count = module.queue.max_receive_count
  ingestion_bucket_arn    = module.bucket.bucket_arn
  results_table_arn       = module.table.table_arn
  results_table_name      = module.table.table_name
  llm_model               = var.llm_model
  llm_provider_secret_arn = data.aws_secretsmanager_secret.llm_provider.arn
  langsmith_secret_arn    = data.aws_secretsmanager_secret.langsmith.arn
  langsmith_project       = "${var.project_name}-${var.environment}"
  log_retention_days      = var.environment == "prod" ? 30 : 14
  environment             = var.environment
  alarm_topic_arn         = module.alarms.topic_arn
}
