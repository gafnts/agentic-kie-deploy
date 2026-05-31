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

output "errors_alarm_arn" {
  value = aws_cloudwatch_metric_alarm.errors.arn
}

output "dlq_alarm_arn" {
  value = aws_cloudwatch_metric_alarm.dlq_messages_visible.arn
}
