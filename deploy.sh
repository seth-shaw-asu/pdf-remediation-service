#!/usr/bin/env bash
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-west-2}"
BDA_PROJECT_NAME="${BDA_PROJECT_NAME:-pdf-remediation-bda-project}"
BDA_S3_BUCKET="${BDA_S3_BUCKET:-pdf-remediation-bda-bucket-$(date +%s)}"
ECR_REPO_NAME="${ECR_REPO_NAME:-pdf-remediation-service}"
APP_NAME="${APP_NAME:-pdf-remediation-service}"
IMAGE_TAG="${IMAGE_TAG:-latest}"

function usage() {
  cat <<EOF
Usage: $0 [--region REGION] [--project-name NAME] [--bucket-name NAME] [--repo-name NAME] [--image-tag TAG]

Environment variables may also be used:
  AWS_REGION
  BDA_PROJECT_NAME
  BDA_S3_BUCKET
  ECR_REPO_NAME
  IMAGE_TAG

This script creates an S3 bucket, a Bedrock Data Automation project, builds a Docker image,
pushes it to ECR, and prints recommended ECS environment configuration.
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) AWS_REGION="$2"; shift 2;;
    --project-name) BDA_PROJECT_NAME="$2"; shift 2;;
    --bucket-name) BDA_S3_BUCKET="$2"; shift 2;;
    --repo-name) ECR_REPO_NAME="$2"; shift 2;;
    --image-tag) IMAGE_TAG="$2"; shift 2;;
    --help|-h) usage;;
    *) echo "Unknown option: $1" >&2; usage;;
  esac
done

function require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: required command '$1' not found." >&2
    exit 1
  fi
}

require_command aws
require_command docker
require_command jq

AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
AWS_ACCOUNT_ID=${AWS_ACCOUNT_ID:?Unable to resolve AWS account ID}

echo "Using AWS region: $AWS_REGION"

echo "Creating S3 bucket $BDA_S3_BUCKET..."
if aws s3api head-bucket --bucket "$BDA_S3_BUCKET" >/dev/null 2>&1; then
  echo "Bucket already exists: $BDA_S3_BUCKET"
else
  if [[ "$AWS_REGION" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "$BDA_S3_BUCKET" --region "$AWS_REGION"
  else
    aws s3api create-bucket --bucket "$BDA_S3_BUCKET" --region "$AWS_REGION" \
      --create-bucket-configuration LocationConstraint="$AWS_REGION"
  fi
  aws s3api put-public-access-block --bucket "$BDA_S3_BUCKET" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
fi

echo "Checking for existing Bedrock Data Automation project $BDA_PROJECT_NAME..."
LIST_OUTPUT=$(aws bedrock-data-automation list-data-automation-projects --region "$AWS_REGION" --output json 2>/dev/null || echo "{}")
BDA_PROJECT_ARN=$(echo "$LIST_OUTPUT" | jq -r ".projects[]? | select(.projectName == \"$BDA_PROJECT_NAME\") | .projectArn // empty")

if [[ -n "$BDA_PROJECT_ARN" && "$BDA_PROJECT_ARN" != "null" ]]; then
  echo "Found existing project: $BDA_PROJECT_ARN"
else
  echo "Creating new Bedrock Data Automation project $BDA_PROJECT_NAME..."
  PROJECT_OUTPUT=$(aws bedrock-data-automation create-data-automation-project \
    --project-name "$BDA_PROJECT_NAME" \
    --project-stage LIVE \
    --standard-output-configuration 'document={extraction={granularity={types=[DOCUMENT]},boundingBox={state=ENABLED}}}' \
    --region "$AWS_REGION" \
    --output json 2>&1)
  CREATE_EXIT=$?
  if [[ $CREATE_EXIT -eq 0 ]]; then
    echo "$PROJECT_OUTPUT" | jq '.'
    BDA_PROJECT_ARN=$(echo "$PROJECT_OUTPUT" | jq -r '.projectArn // empty')
  else
    # Check if the error is due to the project already existing
    if echo "$PROJECT_OUTPUT" | grep -qiE "ConflictException|already exists|AlreadyExists"; then
      echo "Project already exists, attempting to retrieve it..."
      LIST_RETRY=$(aws bedrock-data-automation list-data-automation-projects --region "$AWS_REGION" --output json 2>/dev/null || echo "{}")
      BDA_PROJECT_ARN=$(echo "$LIST_RETRY" | jq -r ".projects[]? | select(.projectName == \"$BDA_PROJECT_NAME\") | .projectArn // empty")
      if [[ -n "$BDA_PROJECT_ARN" && "$BDA_PROJECT_ARN" != "null" ]]; then
        echo "Retrieved existing project ARN: $BDA_PROJECT_ARN"
      else
        echo "ERROR: Project exists but could not retrieve ARN." >&2
        echo "AWS error: $PROJECT_OUTPUT" >&2
        exit 1
      fi
    else
      echo "ERROR: Failed to create Bedrock Data Automation project." >&2
      echo "AWS error: $PROJECT_OUTPUT" >&2
      echo "Try checking manually with: aws bedrock-data-automation list-data-automation-projects --region $AWS_REGION" >&2
      exit 1
    fi
  fi
fi

if [[ -z "$BDA_PROJECT_ARN" || "$BDA_PROJECT_ARN" == "null" ]]; then
  echo "ERROR: Could not determine Bedrock Data Automation project ARN." >&2
  echo "DEBUG: LIST_OUTPUT was: $LIST_OUTPUT" >&2
  echo "DEBUG: BDA_PROJECT_ARN was: '$BDA_PROJECT_ARN'" >&2
  exit 1
fi

echo "Using Bedrock Data Automation project: $BDA_PROJECT_ARN"

echo "Building Docker image..."
IMAGE_URI="$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$ECR_REPO_NAME:$IMAGE_TAG"

if ! aws ecr describe-repositories --repository-names "$ECR_REPO_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
  aws ecr create-repository --repository-name "$ECR_REPO_NAME" --region "$AWS_REGION" >/dev/null
fi

echo "Authenticating to ECR..."
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"

docker build -t "$IMAGE_URI" .
docker push "$IMAGE_URI"

echo "Deployment artifacts ready."
echo "ECR image pushed to: $IMAGE_URI"
echo
cat <<EOF
Next steps:
  1. Create an ECS cluster.
  2. Create an ECS task definition using the image URI above.
  3. Configure task environment variables:
       BDA_PROJECT_ARN=$BDA_PROJECT_ARN
       BDA_S3_BUCKET=$BDA_S3_BUCKET
  4. Grant the ECS task role permissions for Bedrock Data Automation and the S3 bucket.
  5. Launch an ECS service using the task definition.
EOF
