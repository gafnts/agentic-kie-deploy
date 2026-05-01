output "local_role_arn" {
  value = aws_iam_role.deploy["local"].arn
}

output "dev_role_arn" {
  value = aws_iam_role.deploy["dev"].arn
}

output "prod_role_arn" {
  value = aws_iam_role.deploy["prod"].arn
}
