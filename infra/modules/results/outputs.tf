output "bucket_name" {
  description = "Name of the analytics (extractions) bucket. The caller scopes its s3:GetObject grant to the bucket's extractions/ prefix."
  value       = aws_s3_bucket.results.bucket
}

output "bucket_arn" {
  description = "ARN of the analytics bucket. Compose into the caller's s3:GetObject grant on extractions/* (the grant lives on the caller's side, not in this module)."
  value       = aws_s3_bucket.results.arn
}

output "function_name" {
  value = aws_lambda_function.publisher.function_name
}

output "function_arn" {
  value = aws_lambda_function.publisher.arn
}

output "execution_role_arn" {
  value = aws_iam_role.publisher.arn
}

output "log_group_name" {
  value = aws_cloudwatch_log_group.publisher.name
}

output "dlq_arn" {
  value = aws_sqs_queue.publisher_dlq.arn
}

output "dlq_name" {
  value = aws_sqs_queue.publisher_dlq.name
}

output "athena_results_bucket_name" {
  value = aws_s3_bucket.athena_results.bucket
}

output "glue_database_name" {
  value = aws_glue_catalog_database.results.name
}

output "glue_table_name" {
  value = aws_glue_catalog_table.extractions.name
}

output "athena_workgroup_name" {
  description = "Name of the dedicated Athena workgroup. Run extraction queries with this workgroup to inherit the per-query scan cap and the pinned result location."
  value       = aws_athena_workgroup.results.name
}

output "errors_alarm_arn" {
  value = aws_cloudwatch_metric_alarm.errors.arn
}

output "dlq_alarm_arn" {
  value = aws_cloudwatch_metric_alarm.dlq_messages_visible.arn
}
