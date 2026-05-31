resource "aws_sqs_queue" "publisher_dlq" {
  name                      = "${var.function_name}-dlq"
  message_retention_seconds = 1209600 # 14 days
  sqs_managed_sse_enabled   = true

  tags = {
    Environment = var.environment
  }
}

data "aws_iam_policy_document" "publisher_dlq" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["sqs:*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    resources = [aws_sqs_queue.publisher_dlq.arn]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_sqs_queue_policy" "publisher_dlq" {
  queue_url = aws_sqs_queue.publisher_dlq.id
  policy    = data.aws_iam_policy_document.publisher_dlq.json
}

resource "aws_cloudwatch_metric_alarm" "dlq_messages_visible" {
  alarm_name          = "${aws_sqs_queue.publisher_dlq.name}-messages-visible"
  alarm_description   = "Any message in the publisher DLQ means a stream batch exhausted maximum_retry_attempts. Single source of truth for failed batches; mirrors the extractor DLQ alarm (ADR-0009 / ADR-0012)."
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
    QueueName = aws_sqs_queue.publisher_dlq.name
  }

  alarm_actions = [var.alarm_topic_arn]
  ok_actions    = [var.alarm_topic_arn]

  tags = {
    Environment = var.environment
  }
}
