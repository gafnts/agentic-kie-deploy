output "ingestion_bucket_name" {
  value = module.storage.bucket_name
}

output "ingestion_bucket_arn" {
  value = module.storage.bucket_arn
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
