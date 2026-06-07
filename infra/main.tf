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
  ingestion_bucket_suffix = substr(sha256("${var.project_name}-${var.environment}-${data.aws_caller_identity.current.account_id}"), 0, 16)
  ingestion_bucket_name   = "${var.project_name}-${var.environment}-ingestion-${local.ingestion_bucket_suffix}"

  results_bucket_suffix = substr(sha256("${var.project_name}-${var.environment}-extractions-${data.aws_caller_identity.current.account_id}"), 0, 16)
  results_bucket_name   = "${var.project_name}-${var.environment}-extractions-${local.results_bucket_suffix}"

  extractor_timeout_seconds = 120

  # The parameter profile follows extractor_flavor so the whole envelope moves with
  # the flavor rather than being hand-edited. Only two knobs differ between flavors:
  # the agent's max_iterations (n/a for single-pass, which has no loop) and the queue's
  # maxReceiveCount (agentic failures are mostly logic, not transient, so retrying an
  # expensive doomed run buys nothing). The timeout, visibility timeout, modality, and
  # concurrency cap hold across both.
  flavor_profiles = {
    single_pass = {
      max_iterations    = null
      max_receive_count = 3
    }
    agentic = {
      max_iterations    = 30
      max_receive_count = 2
    }
  }
  flavor_profile = local.flavor_profiles[var.extractor_flavor]

  # Partition root for result objects, single-sourced here and threaded into both
  # the publisher (write path) and analytics (Glue/Athena read path) modules.
  results_prefix = "extractions"
}

module "alarms" {
  source         = "./modules/alarms"
  topic_name     = "${var.project_name}-${var.environment}-alarms"
  email_endpoint = var.alarm_email
  environment    = var.environment
}

module "ingestion" {
  source        = "./modules/bucket"
  bucket_name   = local.ingestion_bucket_name
  force_destroy = var.environment != "prod"
  environment   = var.environment
}

module "uploader" {
  source                = "./modules/uploader"
  name                  = "${var.project_name}-${var.environment}-uploader"
  ingestion_bucket_arn  = module.ingestion.bucket_arn
  ingestion_bucket_name = module.ingestion.bucket_name
  url_ttl_seconds       = var.url_ttl_seconds
  log_retention_days    = var.environment == "prod" ? 30 : 14
  environment           = var.environment
  alarm_topic_arn       = module.alarms.topic_arn
}

module "queue" {
  source                 = "./modules/queue"
  name                   = "${var.project_name}-${var.environment}-extraction"
  source_bucket_name     = module.ingestion.bucket_name
  lambda_timeout_seconds = local.extractor_timeout_seconds
  max_receive_count      = local.flavor_profile.max_receive_count
  alarm_topic_arn        = module.alarms.topic_arn
  environment            = var.environment
}

module "table" {
  source                      = "./modules/table"
  table_name                  = "${var.project_name}-${var.environment}-results"
  deletion_protection_enabled = var.environment == "prod"
  environment                 = var.environment
}

module "extractor" {
  source                  = "./modules/extractor"
  function_name           = "${var.project_name}-${var.environment}-extractor"
  image_uri               = "${data.aws_ecr_repository.extractor.repository_url}@${var.extractor_image_digest}"
  extractor_flavor        = var.extractor_flavor
  max_iterations          = local.flavor_profile.max_iterations
  timeout_seconds         = local.extractor_timeout_seconds
  memory_mb               = 2048
  ephemeral_storage_mb    = 2048
  architecture            = "arm64"
  max_concurrency         = var.environment == "prod" ? 25 : 10
  queue_arn               = module.queue.queue_arn
  queue_max_receive_count = module.queue.max_receive_count
  ingestion_bucket_arn    = module.ingestion.bucket_arn
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

module "publisher" {
  source                  = "./modules/publisher"
  function_name           = "${var.project_name}-${var.environment}-publisher"
  source_table_stream_arn = module.table.stream_arn
  analytics_bucket_name   = module.analytics.bucket_name
  analytics_bucket_arn    = module.analytics.bucket_arn
  results_prefix          = local.results_prefix
  log_retention_days      = var.environment == "prod" ? 30 : 14
  environment             = var.environment
  alarm_topic_arn         = module.alarms.topic_arn
}

module "analytics" {
  source         = "./modules/analytics"
  bucket_name    = local.results_bucket_name
  project_name   = var.project_name
  results_prefix = local.results_prefix
  force_destroy  = var.environment != "prod"
  environment    = var.environment
}
