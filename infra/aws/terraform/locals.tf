locals {
  name_prefix = "${var.project_name}-${var.environment}"

  common_tags = {
    Project     = var.project_name
    Environment = var.environment
  }

  github_subjects = [
    "repo:${var.github_org}/${var.github_repo}:ref:refs/heads/main",
  ]
}
