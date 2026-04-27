# PDF Accessibility Remediation Service

A FastAPI service that downloads PDF files from trusted domains, converts them to accessible HTML with the AWS Content Accessibility Utility, and returns a self-contained remediated HTML document.

## Endpoints

### Health Check

- `GET /`
- Returns service readiness and verifies that the `content_accessibility_with_aws.api` import is available.

### Remediate

- `POST /remediate`
- Header required: `Apix-Ldp-Resource` with the URL of the PDF to remediate.
- Optional query param: `debug` to preserve temporary files and log config.

## Environment Variables

- `BDA_PROJECT_ARN` - Bedrock Data Automation project ARN.
- `BDA_S3_BUCKET` - S3 bucket name used by the BDA project.
- `MAX_UPLOAD_SIZE_BYTES` - Maximum allowed PDF size in bytes (default `524288000`, i.e. 500 MB).
- `ALLOWED_DOMAIN_PATTERNS` - Comma-separated allowed URL host patterns (default `*.lib.asu.edu,*.cloudfront.net,*.cloudfront.com,*.amazonaws.com`).
- `INLINE_CSS` - Whether to inline CSS in conversion options (`TRUE` / `FALSE`).
- `EMBED_IMAGES` - Whether to embed images in conversion options (`TRUE` / `FALSE`).
- `PERFORM_REMEDIATION` - Whether to run remediation (`TRUE` / `FALSE`).
- `REMEDIATION_MODEL_ID` - Model ID for remediation (default `amazon.nova-lite-v1:0`).
- `AUDIT_SEVERITY_THRESHOLD` - Audit severity threshold (default `minor`).
- `AUDIT_DETAILED` - Whether audit output is detailed (`TRUE` / `FALSE`).
- `LOG_LEVEL` - Python log level (default `INFO`).

## Run locally

Requires Python 3.11 or later.

```bash
python3.11 -m pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## Docker

```bash
docker build -t pdf-remediation-service .
```

## Deployment

This service is intended to run inside AWS ECS with permissions for the Bedrock Data Automation (BDA) project and the configured S3 bucket.

A helper deployment script is available:

```bash
./deploy.sh --region us-east-1 --project-name pdf-remediation-bda-project --bucket-name pdf-remediation-bda-bucket --repo-name pdf-remediation-service --image-tag latest
```

This script will:

1. Create the S3 bucket.
2. Create the Bedrock Data Automation project.
3. Build the Docker image.
4. Push the image to AWS ECR.

After running the script, create an ECS task definition and service with environment variables:
   - `BDA_PROJECT_ARN`
   - `BDA_S3_BUCKET`

A sample ECS task definition is available in `ecs-task-definition.json`. Update the placeholders for your AWS account, region, image URI, and the required IAM roles.

## Terraform Deployment

A Terraform deployment is included under `terraform/`.

To use it:

```bash
cd terraform
terraform init
terraform apply -var="aws_region=us-east-1" -var="project_name=pdf-remediation-service" -var="image_tag=latest"
```

If you want Terraform to explicitly pass the Bedrock Data Automation ARN into the task, set:

```bash
terraform apply -var="bda_project_arn=arn:aws:bedrock:..."
```

The Terraform deployment will create:

- S3 bucket with public access blocked
- ECR repository
- ECS cluster, task definition, and Fargate service
- IAM roles for task execution and task role
- CloudWatch log group

## Notes

- The `/` health endpoint confirms the package import and that required BDA configuration values are present.
- The `/remediate` endpoint rejects non-PDFs, files larger than the configured maximum, and URLs outside of the allowed host patterns.
- Temporary files are removed after successful remediation unless `debug` is provided.
