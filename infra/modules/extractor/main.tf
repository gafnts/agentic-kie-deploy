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
    sid    = "IngestionReadObject"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:ListBucket",
    ]
    resources = [
      var.ingestion_bucket_arn,
      "${var.ingestion_bucket_arn}/*",
    ]
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
