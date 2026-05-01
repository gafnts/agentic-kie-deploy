output "ingestion_bucket_name" {
  value = aws_s3_bucket.ingestion_bucket.bucket
}

output "ingestion_bucket_arn" {
  value = aws_s3_bucket.ingestion_bucket.arn
}
