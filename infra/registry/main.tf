data "aws_caller_identity" "current" {}

locals {
  repository_name      = "${var.project_name}-${var.environment}-extractor"
  extractor_lambda_arn = "arn:aws:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:function:${local.repository_name}"
}

#trivy:ignore:AVD-AWS-0033
resource "aws_ecr_repository" "extractor" {
  name                 = local.repository_name
  image_tag_mutability = "IMMUTABLE"
  force_delete         = var.environment != "prod"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "extractor" {
  repository = aws_ecr_repository.extractor.name
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after 1 day"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep last 10 sha-tagged images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["sha-"]
          countType     = "imageCountMoreThan"
          countNumber   = 10
        }
        action = { type = "expire" }
      }
    ]
  })
}

data "aws_iam_policy_document" "extractor" {
  statement {
    sid    = "AllowExtractorLambdaPull"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
    actions = [
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = [local.extractor_lambda_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_ecr_repository_policy" "extractor" {
  repository = aws_ecr_repository.extractor.name
  policy     = data.aws_iam_policy_document.extractor.json
}
