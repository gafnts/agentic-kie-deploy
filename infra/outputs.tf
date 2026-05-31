output "alarm_topic_arn" {
  value = module.alarms.topic_arn
}

output "ingestion_bucket_name" {
  value = module.ingestion.bucket_name
}

output "ingestion_bucket_arn" {
  value = module.ingestion.bucket_arn
}

output "uploader_api_endpoint" {
  value = module.uploader.api_endpoint
}

output "uploader_route_arn" {
  description = "Execution ARN of POST /uploads. The caller's IAM role attaches execute-api:Invoke on this ARN."
  value       = module.uploader.route_arn
}

output "uploader_function_name" {
  value = module.uploader.function_name
}

output "extraction_queue_url" {
  value = module.queue.queue_url
}

output "extraction_queue_arn" {
  value = module.queue.queue_arn
}

output "extraction_dlq_arn" {
  value = module.queue.dlq_arn
}

output "results_table_name" {
  value = module.table.table_name
}

output "results_table_arn" {
  value = module.table.table_arn
}

output "extractor_function_name" {
  value = module.extractor.function_name
}

output "extractor_function_arn" {
  value = module.extractor.function_arn
}

output "extractor_log_group_name" {
  value = module.extractor.log_group_name
}

output "analytics_bucket_name" {
  value = module.results.bucket_name
}

output "analytics_bucket_arn" {
  description = "ARN of the analytics bucket. The caller scopes its s3:GetObject grant to the bucket's extractions/ prefix."
  value       = module.results.bucket_arn
}

output "publisher_function_name" {
  value = module.results.function_name
}

output "publisher_dlq_arn" {
  value = module.results.dlq_arn
}

output "results_glue_database_name" {
  value = module.results.glue_database_name
}

output "results_athena_workgroup_name" {
  value = module.results.athena_workgroup_name
}
