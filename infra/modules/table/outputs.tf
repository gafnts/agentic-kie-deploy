output "table_name" {
  value = aws_dynamodb_table.results.name
}

output "table_arn" {
  value = aws_dynamodb_table.results.arn
}
