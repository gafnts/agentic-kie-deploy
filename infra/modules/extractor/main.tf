#trivy:ignore:AVD-AWS-0017
resource "aws_cloudwatch_log_group" "extractor" {
  name              = "/aws/lambda/${var.function_name}"
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

resource "aws_iam_role" "extractor" {
  name               = "${var.function_name}-exec"
  assume_role_policy = data.aws_iam_policy_document.assume_role.json

  tags = {
    Environment = var.environment
    Role        = "extractor-exec"
  }
}

data "aws_iam_policy_document" "extractor" {
  statement {
    sid    = "SqsConsume"
    effect = "Allow"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
    ]
    resources = [var.queue_arn]
  }

  statement {
    sid       = "IngestionReadObject"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${var.ingestion_bucket_arn}/*"]
  }

  statement {
    sid    = "ResultsWrite"
    effect = "Allow"
    actions = [
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:GetItem",
    ]
    resources = [var.results_table_arn]
  }

  statement {
    sid       = "SecretsRead"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.llm_provider_secret_arn, var.langsmith_secret_arn]
  }

  statement {
    sid    = "LogsWrite"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.extractor.arn}:*"]
  }
}

resource "aws_iam_role_policy" "extractor" {
  name   = "extractor"
  role   = aws_iam_role.extractor.id
  policy = data.aws_iam_policy_document.extractor.json
}

#trivy:ignore:AVD-AWS-0066
resource "aws_lambda_function" "extractor" {
  function_name = var.function_name
  role          = aws_iam_role.extractor.arn
  package_type  = "Image"
  image_uri     = var.image_uri
  architectures = [var.architecture]
  memory_size   = var.memory_mb
  timeout       = var.timeout_seconds

  ephemeral_storage {
    size = var.ephemeral_storage_mb
  }

  environment {
    variables = {
      LLM_MODEL               = var.llm_model
      LLM_PROVIDER_SECRET_ARN = var.llm_provider_secret_arn
      LANGSMITH_SECRET_ARN    = var.langsmith_secret_arn
      LANGSMITH_PROJECT       = var.langsmith_project
      RESULTS_TABLE_NAME      = var.results_table_name
      SQS_MAX_RECEIVE_COUNT   = tostring(var.queue_max_receive_count)
    }
  }

  tags = {
    Environment = var.environment
  }

  depends_on = [
    aws_iam_role_policy.extractor,
    aws_cloudwatch_log_group.extractor,
  ]
}

resource "aws_lambda_event_source_mapping" "extraction" {
  event_source_arn                   = var.queue_arn
  function_name                      = aws_lambda_function.extractor.arn
  batch_size                         = 1
  maximum_batching_window_in_seconds = 0
  function_response_types            = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = var.max_concurrency
  }
}

resource "aws_cloudwatch_metric_alarm" "errors" {
  alarm_name          = "${var.function_name}-errors"
  alarm_description   = "Lambda invocations that ended in an unhandled exception. With maxReceiveCount=3 on the queue, a single bad document fires this up to three times before it lands in the DLQ — the alarm is the early-warning signal that the DLQ alarm is the confirmation of."
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
    FunctionName = aws_lambda_function.extractor.function_name
  }

  alarm_actions = [var.alarm_topic_arn]
  ok_actions    = [var.alarm_topic_arn]

  tags = {
    Environment = var.environment
  }
}

resource "aws_cloudwatch_metric_alarm" "throttles" {
  alarm_name          = "${var.function_name}-throttles"
  alarm_description   = "Invocations rejected because the function hit its concurrency cap. With maximum_concurrency set on the event source mapping, throttles mean ingestion is exceeding the planned LLM fan-out budget; either the cap is wrong or there is a burst worth investigating."
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
    FunctionName = aws_lambda_function.extractor.function_name
  }

  alarm_actions = [var.alarm_topic_arn]
  ok_actions    = [var.alarm_topic_arn]

  tags = {
    Environment = var.environment
  }
}
