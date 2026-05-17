output "queue_arn" {
  value = aws_sqs_queue.extraction.arn
}

output "queue_url" {
  value = aws_sqs_queue.extraction.url
}

output "queue_name" {
  value = aws_sqs_queue.extraction.name
}

output "dlq_arn" {
  value = aws_sqs_queue.extraction_dlq.arn
}

output "dlq_url" {
  value = aws_sqs_queue.extraction_dlq.url
}

output "dlq_name" {
  value = aws_sqs_queue.extraction_dlq.name
}

output "dlq_alarm_arn" {
  value = aws_cloudwatch_metric_alarm.dlq_messages_visible.arn
}
