output "topic_arn" {
  description = "ARN of the alarm SNS topic. Pass to consuming modules so their CloudWatch alarms publish here."
  value       = aws_sns_topic.alarms.arn
}

output "topic_name" {
  value = aws_sns_topic.alarms.name
}
