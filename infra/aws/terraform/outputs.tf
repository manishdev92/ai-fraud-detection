output "alb_url" {
  description = "Public HTTP URL for the API"
  value       = "http://${aws_lb.main.dns_name}"
}

output "alb_dns_name" {
  value = aws_lb.main.dns_name
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "ecs_cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "ecs_service_name" {
  value = aws_ecs_service.app.name
}

output "github_actions_role_arn" {
  description = "Add to GitHub repo secret AWS_ROLE_ARN"
  value       = aws_iam_role.github_actions.arn
}

output "aws_region" {
  value = var.aws_region
}

output "rds_endpoint" {
  value = aws_db_instance.main.address
}

output "secrets_database_url_arn" {
  value = aws_secretsmanager_secret.database_url.arn
}

output "secrets_gemini_arn" {
  value = aws_secretsmanager_secret.gemini_api_key.arn
}
