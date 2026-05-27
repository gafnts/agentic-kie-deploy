output "api_endpoint" {
  description = "Base URL of the HTTP API. The presigner route lives at POST {api_endpoint}/uploads."
  value       = aws_apigatewayv2_api.uploader.api_endpoint
}

output "api_arn" {
  value = aws_apigatewayv2_api.uploader.arn
}

output "route_arn" {
  description = "Execution ARN of the POST /uploads route. Compose into the caller's execute-api:Invoke grant."
  value       = "${aws_apigatewayv2_api.uploader.execution_arn}/*/POST/uploads"
}

output "function_name" {
  value = aws_lambda_function.presigner.function_name
}

output "function_arn" {
  value = aws_lambda_function.presigner.arn
}

output "execution_role_arn" {
  value = aws_iam_role.presigner.arn
}

output "log_group_name" {
  value = aws_cloudwatch_log_group.presigner.name
}

output "errors_alarm_arn" {
  value = aws_cloudwatch_metric_alarm.errors.arn
}

output "throttles_alarm_arn" {
  value = aws_cloudwatch_metric_alarm.throttles.arn
}
