output "aws_region" {
  value       = var.aws_region
  description = "AWS region used for deployment."
}

output "s3_bucket_name" {
  value       = aws_s3_bucket.bda_bucket.bucket
  description = "S3 bucket created for Bedrock Data Automation."
}

output "ecr_repository_url" {
  value       = aws_ecr_repository.app_repo.repository_url
  description = "ECR repository URL for the application image."
}

output "ecs_cluster_name" {
  value       = aws_ecs_cluster.main.name
  description = "ECS cluster name."
}

output "ecs_service_name" {
  value       = aws_ecs_service.service.name
  description = "ECS service name."
}

output "task_role_arn" {
  value       = aws_iam_role.ecs_task_role.arn
  description = "IAM role ARN for ECS tasks."
}
