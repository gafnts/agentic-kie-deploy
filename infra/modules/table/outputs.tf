output "table_name" {
  value = aws_dynamodb_table.results.name
}

output "table_arn" {
  value = aws_dynamodb_table.results.arn
}

output "stream_arn" {
  description = "ARN of the table's DynamoDB Stream (NEW_IMAGE). The results module subscribes its publisher event source mapping to this."
  value       = aws_dynamodb_table.results.stream_arn
}
