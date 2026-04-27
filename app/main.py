import logging
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from content_accessibility_utility_on_aws.api import process_pdf_accessibility

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger("pdf_remediation_service")

MAX_UPLOAD_SIZE_BYTES = int(os.getenv("MAX_UPLOAD_SIZE_BYTES", str(500 * 1024 * 1024)))
ALLOWED_DOMAIN_PATTERNS = [
    pattern.strip()
    for pattern in os.getenv(
        "ALLOWED_DOMAIN_PATTERNS",
        "*.lib.asu.edu,*.cloudfront.net",
    ).split(",")
    if pattern.strip()
]

BDA_PROJECT_ARN = os.getenv("BDA_PROJECT_ARN")
BDA_S3_BUCKET = os.getenv("BDA_S3_BUCKET")
DEFAULT_REMIDIATION_MODEL = os.getenv("REMEDIATION_MODEL_ID", "amazon.nova-lite-v1:0")

app = FastAPI(title="PDF Accessibility Remediation Service")


def parse_bool(value: Optional[str], default: bool = True) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    return normalized in {"1", "true", "yes", "on"}


def is_allowed_domain(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme not in {"http", "https"} or not host:
        return False

    for pattern in ALLOWED_DOMAIN_PATTERNS:
        if pattern.startswith("*."):
            allowed_suffix = pattern[2:]
            if host == allowed_suffix or host.endswith(f".{allowed_suffix}"):
                return True
        elif host == pattern:
            return True
    return False


def sanitize_filename(url: str) -> str:
    path = urlparse(url).path
    name = Path(path).name
    if not name:
        name = "document.pdf"
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
    if len(name) > 128:
        name = name[-128:]
    return f"{uuid.uuid4().hex}_{name}"


async def download_pdf(source_url: str, target_path: Path) -> None:
    async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
        async with client.stream("GET", source_url) as response:
            if response.status_code != 200:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unable to download PDF: received status code {response.status_code}",
                )

            content_type = response.headers.get("content-type", "")
            if content_type and "pdf" not in content_type.lower():
                raise HTTPException(status_code=400, detail="The requested file is not a PDF.")

            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    content_length_value = int(content_length)
                    if content_length_value > MAX_UPLOAD_SIZE_BYTES:
                        raise HTTPException(
                            status_code=400,
                            detail=f"File is too large: {content_length_value} bytes exceeds {MAX_UPLOAD_SIZE_BYTES} bytes.",
                        )
                except ValueError:
                    pass

            total_bytes = 0
            first_chunk = True
            with target_path.open("wb") as destination:
                async for chunk in response.aiter_bytes(chunk_size=16384):
                    if not chunk:
                        continue
                    total_bytes += len(chunk)
                    if total_bytes > MAX_UPLOAD_SIZE_BYTES:
                        raise HTTPException(
                            status_code=400,
                            detail=f"File is too large: exceeded {MAX_UPLOAD_SIZE_BYTES} bytes while downloading.",
                        )
                    if first_chunk:
                        first_chunk = False
                        if not chunk.startswith(b"%PDF-"):
                            raise HTTPException(status_code=400, detail="The downloaded file is not a valid PDF.")
                    destination.write(chunk)


def build_process_config() -> Dict[str, object]:
    conversion_options = {
        "inline_css": parse_bool(os.getenv("INLINE_CSS", "TRUE"), True),
        "embed_images": parse_bool(os.getenv("EMBED_IMAGES", "TRUE"), True),
    }

    remediation_options = {
        "model_id": DEFAULT_REMIDIATION_MODEL,
        "auto_fix": parse_bool(os.getenv("AUTO_FIX", "TRUE"), True),
    }
    if BDA_PROJECT_ARN:
        remediation_options["bda_project_arn"] = BDA_PROJECT_ARN
    if BDA_S3_BUCKET:
        remediation_options["bda_s3_bucket"] = BDA_S3_BUCKET

    audit_options = {
        "severity_threshold": os.getenv("AUDIT_SEVERITY_THRESHOLD", "minor"),
        "detailed": parse_bool(os.getenv("AUDIT_DETAILED", "TRUE"), True),
    }

    return {
        "conversion_options": conversion_options,
        "audit_options": audit_options,
        "remediation_options": remediation_options,
        "perform_audit": False,
        "perform_remediation": parse_bool(os.getenv("PERFORM_REMEDIATION", "TRUE"), True),
    }


def find_remediated_html(output_dir: Path) -> Path:
    html_files = sorted(output_dir.rglob("*.html"))
    if not html_files:
        raise HTTPException(
            status_code=500,
            detail="No remediated HTML file was produced by the remediation process.",
        )
    return html_files[0]


@app.get("/", response_class=JSONResponse)
async def health_check() -> dict:
    try:
        from content_accessibility_with_aws.api import process_pdf_accessibility  # noqa: F401
        import_ok = True
        import_error = None
    except Exception as exc:
        import_ok = False
        import_error = str(exc)

    bda_ready = bool(BDA_PROJECT_ARN and BDA_S3_BUCKET)
    ready = import_ok and bda_ready
    status = "ok" if ready else "degraded"

    return {
        "status": status,
        "content_accessibility_import": import_ok,
        "bda_project_configured": bda_ready,
        "details": {
            "import_error": import_error,
            "bda_project_arn": bool(BDA_PROJECT_ARN),
            "bda_s3_bucket": bool(BDA_S3_BUCKET),
        },
    }


@app.post("/remediate")
async def remediate_pdf(
    Apix_Ldp_Resource: str = Header(..., alias="Apix-Ldp-Resource"),
    debug: Optional[str] = Query(None),
) -> FileResponse:
    logger.info("Received remediation request for URL=%s debug=%s", Apix_Ldp_Resource, debug)
    if not is_allowed_domain(Apix_Ldp_Resource):
        logger.warning("Rejected unsupported domain for URL=%s", Apix_Ldp_Resource)
        raise HTTPException(status_code=403, detail="PDF URL is not in the allowed domain list.")

    sanitized_name = sanitize_filename(Apix_Ldp_Resource)
    download_dir = Path(tempfile.mkdtemp(prefix="pdf_download_"))
    output_dir = Path(tempfile.mkdtemp(prefix="pdf_remediation_out_"))
    downloaded_file = download_dir / sanitized_name

    debug_mode = debug is not None
    try:
        await download_pdf(Apix_Ldp_Resource, downloaded_file)

        config = build_process_config()
        if debug_mode:
            logger.info("Remediation configuration: %s", config)

        await run_in_threadpool(
            process_pdf_accessibility,
            pdf_path=str(downloaded_file),
            output_dir=str(output_dir),
            conversion_options=config["conversion_options"],
            audit_options=config["audit_options"],
            remediation_options=config["remediation_options"],
            perform_audit=config["perform_audit"],
            perform_remediation=config["perform_remediation"],
        )

        html_file = find_remediated_html(output_dir)
        response = FileResponse(
            path=html_file,
            media_type="text/html",
            filename=html_file.name,
        )
        response.headers["Content-Disposition"] = f"attachment; filename={html_file.name}"
        return response
    finally:
        if not debug_mode:
            shutil.rmtree(download_dir, ignore_errors=True)
            shutil.rmtree(output_dir, ignore_errors=True)
            logger.info("Cleaned up temporary files for URL=%s", Apix_Ldp_Resource)
        else:
            logger.info("Debug mode enabled; preserving temporary directories %s and %s", download_dir, output_dir)
