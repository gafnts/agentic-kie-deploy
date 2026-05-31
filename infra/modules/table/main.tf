#trivy:ignore:AVD-AWS-0025
resource "aws_dynamodb_table" "results" {
  name         = var.table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "document_id"

  attribute {
    name = "document_id"
    type = "S"
  }

  stream_enabled   = true
  stream_view_type = "NEW_IMAGE"

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = true
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  deletion_protection_enabled = var.deletion_protection_enabled
}
