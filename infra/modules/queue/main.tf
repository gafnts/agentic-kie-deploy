locals {
  visibility_timeout_seconds = var.lambda_timeout_seconds * 6
}

resource "aws_sqs_queue" "extraction_dlq" {
  name                      = "${var.name}-dlq"
  message_retention_seconds = 1209600 # 14 days
  sqs_managed_sse_enabled   = true

  tags = {
    Environment = var.environment
  }
}

resource "aws_sqs_queue" "extraction" {
  name                       = var.name
  visibility_timeout_seconds = local.visibility_timeout_seconds
  message_retention_seconds  = 345600 # 4 days
  receive_wait_time_seconds  = 20
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.extraction_dlq.arn
    maxReceiveCount     = var.max_receive_count
  })

  tags = {
    Environment = var.environment
  }
}

resource "aws_cloudwatch_event_rule" "object_created" {
  name        = "${var.name}-object-created"
  description = "Route S3 Object Created events from the ingestion bucket to the extraction queue"

  tags = {
    Environment = var.environment
  }

  event_pattern = jsonencode({
    source        = ["aws.s3"]
    "detail-type" = ["Object Created"]
    detail = {
      bucket = {
        name = [var.source_bucket_name]
      }
      object = {
        key = [{ prefix = "uploads/" }]
      }
    }
  })
}

resource "aws_cloudwatch_event_target" "extraction_queue" {
  rule = aws_cloudwatch_event_rule.object_created.name
  arn  = aws_sqs_queue.extraction.arn
}

data "aws_iam_policy_document" "extraction_queue" {
  statement {
    sid     = "AllowEventBridgeSendMessage"
    effect  = "Allow"
    actions = ["sqs:SendMessage"]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }

    resources = [aws_sqs_queue.extraction.arn]

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.object_created.arn]
    }
  }

  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["sqs:*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    resources = [aws_sqs_queue.extraction.arn]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_sqs_queue_policy" "extraction" {
  queue_url = aws_sqs_queue.extraction.id
  policy    = data.aws_iam_policy_document.extraction_queue.json
}

data "aws_iam_policy_document" "extraction_dlq" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["sqs:*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    resources = [aws_sqs_queue.extraction_dlq.arn]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_sqs_queue_policy" "extraction_dlq" {
  queue_url = aws_sqs_queue.extraction_dlq.id
  policy    = data.aws_iam_policy_document.extraction_dlq.json
}

resource "aws_cloudwatch_metric_alarm" "dlq_messages_visible" {
  alarm_name          = "${aws_sqs_queue.extraction_dlq.name}-messages-visible"
  alarm_description   = "Any message in the DLQ means a document exhausted maxReceiveCount=3 retries. The DLQ alarm is the single source of truth for failed messages."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.extraction_dlq.name
  }

  alarm_actions = [var.alarm_topic_arn]
  ok_actions    = [var.alarm_topic_arn]

  tags = {
    Environment = var.environment
  }
}
