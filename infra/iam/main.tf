data "aws_caller_identity" "current" {}

locals {
  account_id    = data.aws_caller_identity.current.account_id
  oidc_provider = "arn:aws:iam::${local.account_id}:oidc-provider/token.actions.githubusercontent.com"
  envs          = ["local", "dev", "prod"]
}

data "aws_iam_policy_document" "trust_local" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = [var.local_principal_arn]
    }
  }
}

data "aws_iam_policy_document" "trust_dev" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:${var.github_repo}:ref:refs/heads/develop",
        "repo:${var.github_repo}:pull_request",
      ]
    }
  }
}

data "aws_iam_policy_document" "trust_prod" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repo}:environment:prod"]
    }
  }
}

data "aws_iam_policy_document" "trust_prod_plan" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:${var.github_repo}:ref:refs/heads/main",
        "repo:${var.github_repo}:pull_request",
      ]
    }
  }
}

data "aws_iam_policy_document" "state_access" {
  for_each = toset(local.envs)

  statement {
    sid       = "ListOwnPrefix"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::${var.state_bucket_name}"]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["service/${each.key}/*"]
    }
  }

  statement {
    sid    = "ReadWriteOwnState"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:GetObjectAttributes",
    ]
    resources = ["arn:aws:s3:::${var.state_bucket_name}/service/${each.key}/*"]
  }
}

data "aws_iam_policy_document" "deny_other_envs" {
  for_each = toset(local.envs)

  statement {
    sid       = "DenyTouchingOtherEnvs"
    effect    = "Deny"
    actions   = ["*"]
    resources = ["*"]
    condition {
      test     = "StringNotEquals"
      variable = "aws:ResourceTag/Environment"
      values   = [each.key]
    }
    condition {
      test     = "Null"
      variable = "aws:ResourceTag/Environment"
      values   = ["false"]
    }
  }
}

data "aws_iam_policy_document" "iam_for_lambda" {
  statement {
    effect = "Allow"
    actions = [
      "iam:CreateRole",
      "iam:DeleteRole",
      "iam:GetRole",
      "iam:PassRole",
      "iam:AttachRolePolicy",
      "iam:DetachRolePolicy",
      "iam:PutRolePolicy",
      "iam:DeleteRolePolicy",
      "iam:GetRolePolicy",
      "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies",
      "iam:TagRole",
      "iam:UntagRole",
    ]
    resources = ["arn:aws:iam::${local.account_id}:role/agentic-kie-*"]
  }
}

resource "aws_iam_role" "deploy" {
  for_each = toset(local.envs)

  name = "agentic-kie-${each.key}-deploy"
  assume_role_policy = {
    "local" = data.aws_iam_policy_document.trust_local.json
    "dev"   = data.aws_iam_policy_document.trust_dev.json
    "prod"  = data.aws_iam_policy_document.trust_prod.json
  }[each.key]

  tags = {
    Environment = each.key
    Role        = "deploy"
  }
}

resource "aws_iam_role_policy_attachment" "power_user" {
  for_each = toset(local.envs)

  role       = aws_iam_role.deploy[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/PowerUserAccess"
}

resource "aws_iam_role_policy" "state_access" {
  for_each = toset(local.envs)

  name   = "tfstate-access"
  role   = aws_iam_role.deploy[each.key].id
  policy = data.aws_iam_policy_document.state_access[each.key].json
}

resource "aws_iam_role_policy" "deny_other_envs" {
  for_each = toset(local.envs)

  name   = "deny-other-envs"
  role   = aws_iam_role.deploy[each.key].id
  policy = data.aws_iam_policy_document.deny_other_envs[each.key].json
}

resource "aws_iam_role_policy" "iam_for_lambda" {
  for_each = toset(local.envs)

  name   = "iam-for-lambda"
  role   = aws_iam_role.deploy[each.key].id
  policy = data.aws_iam_policy_document.iam_for_lambda.json
}

resource "aws_iam_role" "prod_plan" {
  name               = "agentic-kie-prod-plan"
  assume_role_policy = data.aws_iam_policy_document.trust_prod_plan.json

  tags = {
    Environment = "prod"
    Role        = "plan"
  }
}

resource "aws_iam_role_policy_attachment" "prod_plan_readonly" {
  role       = aws_iam_role.prod_plan.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

resource "aws_iam_role_policy" "prod_plan_state_access" {
  name   = "tfstate-access"
  role   = aws_iam_role.prod_plan.id
  policy = data.aws_iam_policy_document.state_access["prod"].json
}

resource "aws_iam_role_policy" "prod_plan_deny_other_envs" {
  name   = "deny-other-envs"
  role   = aws_iam_role.prod_plan.id
  policy = data.aws_iam_policy_document.deny_other_envs["prod"].json
}
