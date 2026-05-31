locals {
  source_file = "${path.module}/../../../src/uploader/presigner.py"
  build_path  = "${path.module}/.build/presigner.zip"
}

data "archive_file" "presigner" {
  type        = "zip"
  source_file = local.source_file
  output_path = local.build_path
}

#trivy:ignore:AVD-AWS-0017
resource "aws_cloudwatch_log_group" "presigner" {
  name              = "/aws/lambda/${var.name}"
  retention_in_days = var.log_retention_days

  tags = {
    Environment = var.environment
  }
}

#trivy:ignore:AVD-AWS-0017
resource "aws_cloudwatch_log_group" "api_access" {
  name              = "/aws/apigateway/${var.name}"
  retention_in_days = var.log_retention_days

  tags = {
    Environment = var.environment
  }
}

data "aws_iam_policy_document" "assume_role" {
  statement {
    sid     = "LambdaAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "presigner" {
  name               = "${var.name}-exec"
  assume_role_policy = data.aws_iam_policy_document.assume_role.json

  tags = {
    Environment = var.environment
    Role        = "uploader-exec"
  }
}

data "aws_iam_policy_document" "presigner" {
  # The presigner does not upload anything itself, but the URL it signs
  # inherits the signer's permissions — so the role must hold the action
  # the URL grants. Scoped to uploads/ so a misuse cannot sign URLs for
  # the analytics partition introduced by ADR-0012.
  statement {
    sid       = "IngestionPut"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${var.ingestion_bucket_arn}/uploads/*"]
  }

  statement {
    sid    = "LogsWrite"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.presigner.arn}:*"]
  }
}

resource "aws_iam_role_policy" "presigner" {
  name   = "presigner"
  role   = aws_iam_role.presigner.id
  policy = data.aws_iam_policy_document.presigner.json
}

#trivy:ignore:AVD-AWS-0066
resource "aws_lambda_function" "presigner" {
  function_name    = var.name
  role             = aws_iam_role.presigner.arn
  runtime          = var.runtime
  handler          = "presigner.handler"
  filename         = data.archive_file.presigner.output_path
  source_code_hash = data.archive_file.presigner.output_base64sha256
  architectures    = [var.architecture]
  memory_size      = var.memory_mb
  timeout          = var.timeout_seconds

  environment {
    variables = {
      INGESTION_BUCKET_NAME = var.ingestion_bucket_name
      URL_TTL_SECONDS       = tostring(var.url_ttl_seconds)
    }
  }

  tags = {
    Environment = var.environment
  }

  depends_on = [
    aws_iam_role_policy.presigner,
    aws_cloudwatch_log_group.presigner,
  ]
}

# HTTP API over REST API: cheaper per request, lower latency, and AWS_IAM
# is a first-class authorizer here — no custom Lambda authorizer required
# (ADR-0010).
resource "aws_apigatewayv2_api" "uploader" {
  name          = var.name
  protocol_type = "HTTP"

  tags = {
    Environment = var.environment
  }
}

resource "aws_apigatewayv2_integration" "presigner" {
  api_id                 = aws_apigatewayv2_api.uploader.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.presigner.invoke_arn
  integration_method     = "POST"
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "uploads" {
  api_id             = aws_apigatewayv2_api.uploader.id
  route_key          = "POST /uploads"
  authorization_type = "AWS_IAM"
  target             = "integrations/${aws_apigatewayv2_integration.presigner.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.uploader.id
  name        = "$default"
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_access.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      ip             = "$context.identity.sourceIp"
      httpMethod     = "$context.httpMethod"
      routeKey       = "$context.routeKey"
      status         = "$context.status"
      responseLength = "$context.responseLength"
      durationMs     = "$context.responseLatency"
    })
  }

  tags = {
    Environment = var.environment
  }
}

resource "aws_lambda_permission" "apigw_invoke" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.presigner.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.uploader.execution_arn}/*/POST/uploads"
}

resource "aws_cloudwatch_metric_alarm" "errors" {
  alarm_name          = "${var.name}-errors"
  alarm_description   = "Presigner invocations that ended in an unhandled exception. The function does one generate_presigned_url call against the SDK — non-zero errors mean either an IAM regression or a malformed request that slipped past API Gateway."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.presigner.function_name
  }

  alarm_actions = [var.alarm_topic_arn]
  ok_actions    = [var.alarm_topic_arn]

  tags = {
    Environment = var.environment
  }
}

resource "aws_cloudwatch_metric_alarm" "throttles" {
  alarm_name          = "${var.name}-throttles"
  alarm_description   = "Presigner invocations rejected because Lambda hit the account concurrency cap. There is no reserved or maximum concurrency on this function (ADR-0010) — throttles imply the account ceiling is being approached."
  namespace           = "AWS/Lambda"
  metric_name         = "Throttles"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.presigner.function_name
  }

  alarm_actions = [var.alarm_topic_arn]
  ok_actions    = [var.alarm_topic_arn]

  tags = {
    Environment = var.environment
  }
}
