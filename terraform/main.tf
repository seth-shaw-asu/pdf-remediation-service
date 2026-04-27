data "aws_caller_identity" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

# VPC and subnet creation
resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {
    Name = "${var.project_name}-vpc"
  }
}

resource "aws_subnet" "main" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index)
  availability_zone = data.aws_availability_zones.available.names[count.index]

  tags = {
    Name = "${var.project_name}-subnet-${count.index + 1}"
  }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "${var.project_name}-igw"
  }
}

resource "aws_route_table" "main" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = {
    Name = "${var.project_name}-rt"
  }
}

resource "aws_route_table_association" "main" {
  count          = 2
  subnet_id      = aws_subnet.main[count.index].id
  route_table_id = aws_route_table.main.id
}

# Use our VPC and subnets
locals {
  vpc_id     = aws_vpc.main.id
  subnet_ids = aws_subnet.main[*].id
}

resource "random_id" "bucket_suffix" {
  byte_length = 4
}

resource "aws_s3_bucket" "bda_bucket" {
  bucket = coalesce(var.bucket_name, "${var.project_name}-${random_id.bucket_suffix.hex}")

  tags = {
    Name        = var.project_name
    Environment = "production"
  }
}

resource "aws_s3_bucket_versioning" "bda_bucket_versioning" {
  bucket = aws_s3_bucket.bda_bucket.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "bda_bucket_encryption" {
  bucket = aws_s3_bucket.bda_bucket.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "block" {
  bucket = aws_s3_bucket.bda_bucket.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_ecr_repository" "app_repo" {
  name = var.ecr_repo_name

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = {
    Name = var.ecr_repo_name
  }
}

resource "aws_cloudwatch_log_group" "ecs" {
  name              = "/ecs/${var.project_name}"
  retention_in_days = 30
}

resource "aws_iam_role" "ecs_task_execution" {
  name = "${var.project_name}-execution-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution_policy" {
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "ecs_task_role" {
  name = "${var.project_name}-task-role"

  assume_role_policy = aws_iam_role.ecs_task_execution.assume_role_policy
}

resource "aws_iam_role_policy" "task_role_policy" {
  name   = "${var.project_name}-task-policy"
  role   = aws_iam_role.ecs_task_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid = "S3Access"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket"
        ]
        Resource = [
          aws_s3_bucket.bda_bucket.arn,
          "${aws_s3_bucket.bda_bucket.arn}/*"
        ]
      },
      {
        Sid = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:CreateLogGroup"
        ]
        Resource = "*"
      },
      {
        Sid = "BedrockAccess"
        Effect = "Allow"
        Action = [
          "bedrock:*",
          "bedrock-data-automation:*"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_security_group" "ecs_service" {
  name        = "${var.project_name}-sg"
  description = "Allow inbound HTTP access to ECS tasks"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port   = var.container_port
    to_port     = var.container_port
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_ecs_cluster" "main" {
  name = "${var.project_name}-cluster"
}

resource "aws_ecs_task_definition" "service" {
  family                   = "${var.project_name}-task"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "512"
  memory                   = "1024"
  network_mode             = "awsvpc"
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task_role.arn

  container_definitions = jsonencode([
    {
      name      = var.project_name
      image     = "${aws_ecr_repository.app_repo.repository_url}:${var.image_tag}"
      essential = true
      portMappings = [
        {
          containerPort = var.container_port
          protocol      = "tcp"
        }
      ]
      environment = [
        {
          name  = "BDA_PROJECT_ARN"
          value = var.bda_project_arn
        },
        {
          name  = "BDA_S3_BUCKET"
          value = aws_s3_bucket.bda_bucket.bucket
        },
        {
          name  = "LOG_LEVEL"
          value = "INFO"
        }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.ecs.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "ecs"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "service" {
  name            = "${var.project_name}-service"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.service.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = slice(local.subnet_ids, 0, 2)
    security_groups = [aws_security_group.ecs_service.id]
    assign_public_ip = true
  }

  depends_on = [
    aws_iam_role_policy_attachment.ecs_task_execution_policy,
    aws_iam_role_policy.task_role_policy
  ]
}

resource "null_resource" "bda_project" {
  provisioner "local-exec" {
    command = <<EOT
set -e
LIST_OUTPUT=$(aws bedrock-data-automation list-data-automation-projects --region ${var.aws_region} --output json)
EXISTING_ARN=$(echo "$LIST_OUTPUT" | jq -r ".projects[]? | select(.projectName == \"${var.project_name}\") | .projectArn // empty")

if [[ -n "$EXISTING_ARN" && "$EXISTING_ARN" != "null" ]]; then
  echo "Found existing BDA project: $EXISTING_ARN"
else
  echo "Creating new BDA project: ${var.project_name}"
  aws bedrock-data-automation create-data-automation-project \
    --project-name ${var.project_name} \
    --project-stage LIVE \
    --standard-output-configuration 'document={extraction={granularity={types=[DOCUMENT]},boundingBox={state=ENABLED}}}' \
    --region ${var.aws_region} \
    --output json
fi
EOT
  }

  triggers = {
    project_name = var.project_name
    region       = var.aws_region
    bucket_name  = aws_s3_bucket.bda_bucket.bucket
  }

  depends_on = [aws_s3_bucket.bda_bucket]
}
