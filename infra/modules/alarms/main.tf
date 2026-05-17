#trivy:ignore:AVD-AWS-0136
resource "aws_sns_topic" "alarms" {
  name              = var.topic_name
  kms_master_key_id = "alias/aws/sns"

  tags = {
    Environment = var.environment
    Role        = "alarm-fanout"
  }
}

resource "aws_sns_topic_subscription" "email" {
  count = var.email_endpoint == null ? 0 : 1

  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.email_endpoint
}
