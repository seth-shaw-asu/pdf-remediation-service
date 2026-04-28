variable "aws_region" {
  type        = string
  description = "AWS region to deploy resources into."
  default     = "us-west-2"
}

variable "vpc_id" {
  description = "Existing VPC ID"
  type        = string
}

variable "subnet_ids" {
  description = "Existing subnet IDs"
  type        = list(string)
}

variable "project_name" {
  type        = string
  description = "Name for the Bedrock Data Automation project and deployment resources."
  default     = "pdf-remediation-service"
}

variable "bucket_name" {
  type        = string
  description = "S3 bucket name for the Bedrock Data Automation project."
  default     = null
}

variable "ecr_repo_name" {
  type        = string
  description = "ECR repository name for the application image."
  default     = "pdf-remediation-service"
}

variable "image_tag" {
  type        = string
  description = "Docker image tag to use for the ECS task definition."
  default     = "latest"
}

variable "image_uri" {
  type        = string
  description = "Full container image URI (e.g. ECR repo URL + tag)"
}

variable "desired_count" {
  type        = number
  description = "Number of ECS tasks to run."
  default     = 1
}

variable "container_port" {
  type        = number
  description = "Container port to expose on ECS tasks."
  default     = 8080
}

variable "bda_project_arn" {
  type        = string
  description = "Bedrock Data Automation project ARN to pass into the ECS task environment."
  default     = ""
}
