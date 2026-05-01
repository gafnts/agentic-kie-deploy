output "bucket_name" {
  value = aws_s3_bucket.ingestion.bucket
}

output "bucket_arn" {
  value = aws_s3_bucket.ingestion.arn
}
