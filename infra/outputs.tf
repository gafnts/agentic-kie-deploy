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
