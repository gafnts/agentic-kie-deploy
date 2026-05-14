output "function_name" {
  value = aws_lambda_function.extractor.function_name
}

output "function_arn" {
  value = aws_lambda_function.extractor.arn
}

output "execution_role_arn" {
  value = aws_iam_role.extractor.arn
}

output "log_group_name" {
  value = aws_cloudwatch_log_group.extractor.name
}

output "timeout_seconds" {
  description = "Echoed back so the queue module can derive its visibility timeout from the same value (ADR-0005)."
  value       = aws_lambda_function.extractor.timeout
}
