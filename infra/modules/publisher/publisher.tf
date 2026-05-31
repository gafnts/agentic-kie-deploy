data "archive_file" "publisher" {
  type        = "zip"
  source_file = local.source_file
  output_path = local.build_path
}

#trivy:ignore:AVD-AWS-0017
resource "aws_cloudwatch_log_group" "publisher" {
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

resource "aws_iam_role" "publisher" {
  name               = "${var.function_name}-exec"
  assume_role_policy = data.aws_iam_policy_document.assume_role.json

  tags = {
    Environment = var.environment
    Role        = "publisher-exec"
  }
}

data "aws_iam_policy_document" "publisher" {
  statement {
    sid    = "StreamRead"
    effect = "Allow"
    actions = [
      "dynamodb:DescribeStream",
      "dynamodb:GetRecords",
      "dynamodb:GetShardIterator",
    ]
    resources = [var.source_table_stream_arn]
  }

  # dynamodb:ListStreams does not support resource-level scoping, so it must be
  # granted on "*". The event source mapping requires it to enumerate the table's
  # streams; the resource-scoped statement above covers the actual record reads.
  statement {
    sid       = "StreamList"
    effect    = "Allow"
    actions   = ["dynamodb:ListStreams"]
    resources = ["*"]
  }

  statement {
    sid       = "ResultsWrite"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${var.analytics_bucket_arn}/${var.results_prefix}/*"]
  }

  # The event source mapping's on_failure destination delivers exhausted batches
  # to the DLQ using this role's credentials, so the role needs SendMessage on it.
  statement {
    sid       = "DlqWrite"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.publisher_dlq.arn]
  }

  statement {
    sid    = "LogsWrite"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.publisher.arn}:*"]
  }
}

resource "aws_iam_role_policy" "publisher" {
  name   = "publisher"
  role   = aws_iam_role.publisher.id
  policy = data.aws_iam_policy_document.publisher.json
}

#trivy:ignore:AVD-AWS-0066
resource "aws_lambda_function" "publisher" {
  function_name    = var.function_name
  role             = aws_iam_role.publisher.arn
  runtime          = var.runtime
  handler          = "publisher.handler"
  filename         = data.archive_file.publisher.output_path
  source_code_hash = data.archive_file.publisher.output_base64sha256
  architectures    = [var.architecture]
  memory_size      = var.memory_mb
  timeout          = var.timeout_seconds

  environment {
    variables = {
      ANALYTICS_BUCKET_NAME = var.analytics_bucket_name
      RESULTS_PREFIX        = var.results_prefix
    }
  }

  tags = {
    Environment = var.environment
  }

  depends_on = [
    aws_iam_role_policy.publisher,
    aws_cloudwatch_log_group.publisher,
  ]
}

resource "aws_lambda_event_source_mapping" "publisher" {
  event_source_arn  = var.source_table_stream_arn
  function_name     = aws_lambda_function.publisher.arn
  starting_position = "LATEST"

  batch_size                         = var.stream_batch_size
  maximum_batching_window_in_seconds = var.stream_batching_window_seconds
  maximum_retry_attempts             = var.stream_retry_attempts
  bisect_batch_on_function_error     = true
  function_response_types            = ["ReportBatchItemFailures"]

  filter_criteria {
    filter {
      pattern = jsonencode({
        eventName = ["INSERT", "MODIFY"]
        dynamodb = {
          NewImage = {
            status = { S = ["succeeded", "failed"] }
          }
        }
      })
    }
  }

  destination_config {
    on_failure {
      destination_arn = aws_sqs_queue.publisher_dlq.arn
    }
  }

  depends_on = [aws_iam_role_policy.publisher]
}

resource "aws_cloudwatch_metric_alarm" "errors" {
  alarm_name          = "${var.function_name}-errors"
  alarm_description   = "Publisher invocations that ended in an unhandled exception. Without this, result objects silently stop reaching S3 while the extractor keeps writing terminal rows to DynamoDB."
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
    FunctionName = aws_lambda_function.publisher.function_name
  }

  alarm_actions = [var.alarm_topic_arn]
  ok_actions    = [var.alarm_topic_arn]

  tags = {
    Environment = var.environment
  }
}

resource "aws_cloudwatch_metric_alarm" "throttles" {
  alarm_name          = "${var.function_name}-throttles"
  alarm_description   = "Publisher invocations rejected because Lambda hit the account concurrency cap. There is no reserved or maximum concurrency on this function — throttles would stall result publishing and leave succeeded/failed DynamoDB rows without corresponding S3 objects."
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
    FunctionName = aws_lambda_function.publisher.function_name
  }

  alarm_actions = [var.alarm_topic_arn]
  ok_actions    = [var.alarm_topic_arn]

  tags = {
    Environment = var.environment
  }
}
