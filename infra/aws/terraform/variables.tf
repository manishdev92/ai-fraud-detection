variable "aws_region" {
  type        = string
  description = "AWS region for all resources"
  default     = "us-east-1"
}

variable "project_name" {
  type        = string
  description = "Short name used in resource naming"
  default     = "fraud-platform"
}

variable "environment" {
  type        = string
  description = "Environment label (dev, staging, prod)"
  default     = "prod"
}

variable "github_org" {
  type        = string
  description = "GitHub organization or username"
  default     = "manishdev92"
}

variable "github_repo" {
  type        = string
  description = "GitHub repository name (without org)"
  default     = "ai-fraud-detection"
}

variable "gemini_api_key" {
  type        = string
  description = "Gemini API key for Agent B (stored in Secrets Manager)"
  sensitive   = true
  default     = ""
}

variable "ecs_cpu" {
  type    = number
  default = 512
}

variable "ecs_memory" {
  type    = number
  default = 1024
}

variable "ecs_desired_count" {
  type    = number
  default = 1
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "db_allocated_storage_gb" {
  type    = number
  default = 20
}

variable "container_image_tag" {
  type        = string
  description = "Docker image tag deployed to ECS (updated by CI/CD)"
  default     = "latest"
}
