import json
import logging
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.background import BackgroundTask
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
DEFAULT_REMEDIATION_MODEL = os.getenv("REMEDIATION_MODEL_ID", "amazon.nova-lite-v1:0")

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
        if pattern == "*":
            return True
        if pattern.startswith("*."):
            allowed_suffix = pattern[2:]
            if host == allowed_suffix or host.endswith(f".{allowed_suffix}"):
                return True
        elif host == pattern:
            return True
    return False


def sanitize_filename(url_or_name: str) -> str:
    path = urlparse(url_or_name).path
    name = Path(path).name or Path(url_or_name).name or "document.pdf"
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
    if len(name) > 128:
        name = name[-128:]
    return f"{uuid.uuid4().hex}_{name}"


def render_home_page() -> str:
    return """<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>PDF Accessibility Remediation Service</title>
  <style>
    :root {
      --bg: #f6f8fb;
      --card: #ffffff;
      --text: #1f2937;
      --muted: #6b7280;
      --border: #d1d5db;
      --accent: #1d4ed8;
      --accent2: #0f766e;
    }
    body {
      margin: 0;
      font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    .wrap {
      max-width: 900px;
      margin: 0 auto;
      padding: 32px 20px 48px;
    }
    h1 {
      margin: 0 0 8px;
      font-size: 2rem;
    }
    p.lead {
      margin: 0 0 24px;
      color: var(--muted);
      line-height: 1.5;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 20px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 20px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.05);
    }
    h2 {
      margin: 0 0 10px;
      font-size: 1.2rem;
    }
    label {
      display: block;
      font-weight: 600;
      margin: 14px 0 6px;
    }
    input[type=\"file\"], input[type=\"url\"], input[type=\"text\"] {
      width: 100%;
      box-sizing: border-box;
      padding: 10px 12px;
      border: 1px solid var(--border);
      border-radius: 10px;
      font-size: 0.95rem;
      background: #fff;
    }
    button {
      margin-top: 16px;
      border: 0;
      border-radius: 10px;
      padding: 10px 14px;
      font-size: 0.95rem;
      font-weight: 700;
      cursor: pointer;
      color: white;
    }
    .btn-upload { background: var(--accent2); }
    .btn-remediate { background: var(--accent); }
    .hint {
      margin-top: 10px;
      color: var(--muted);
      font-size: 0.9rem;
      line-height: 1.4;
    }
    .links {
      margin-top: 18px;
      font-size: 0.95rem;
    }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }
    code {
      background: #eef2ff;
      padding: 2px 6px;
      border-radius: 6px;
    }
  </style>
</head>
<body>
  <div class=\"wrap\">
    <h1>PDF Accessibility Remediation Service</h1>
    <p class=\"lead\">Upload a PDF file or provide a URL to convert a PDF into accessible HTML.</p>

    <div class=\"grid\">
      <div class=\"card\">
        <h2>Upload a file</h2>
        <form action=\"/upload\" method=\"post\" enctype=\"multipart/form-data\">
          <label for=\"file\">Choose a PDF</label>
          <input id=\"file\" name=\"file\" type=\"file\" accept=\"application/pdf\" required />
          <button class=\"btn-upload\" type=\"submit\">Upload and remediate</button>
        </form>
        <div class=\"hint\">This sends a local PDF directly to the app.</div>
      </div>

      <div class=\"card\">
        <h2>Enter a URL</h2>
        <form action=\"/remediate\" method=\"post\">
          <label for=\"url\">PDF URL</label>
          <input id=\"url\" name=\"url\" type=\"url\" placeholder=\"https://example.edu/file.pdf\" required />
          <button class=\"btn-remediate\" type=\"submit\">Remediate from URL</button>
        </form>
        <div class=\"hint\">This downloads a PDF from the URL, then processes it.</div>
      </div>
    </div>

    <div class=\"links\">
      Health check: <a href=\"/health\">/health</a> · API docs: <a href=\"/docs\">/docs</a>
    </div>
  </div>
</body>
</html>"""


async def download_pdf(source_url: str, target_path: Path) -> None:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
        "Referer": source_url,
    }

    async with httpx.AsyncClient(follow_redirects=True, timeout=120.0, headers=headers) as client:
        async with client.stream("GET", source_url) as response:
            if response.status_code != 200:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unable to download PDF: received status code {response.status_code}",
                )

            content_type = response.headers.get("content-type", "")
            if content_type and "pdf" not in content_type.lower() and "octet-stream" not in content_type.lower():
                raise HTTPException(status_code=400, detail="The requested file is not a PDF.")

            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    content_length_value = int(content_length)
                    if content_length_value > MAX_UPLOAD_SIZE_BYTES:
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                f"File is too large: {content_length_value} bytes exceeds "
                                f"{MAX_UPLOAD_SIZE_BYTES} bytes."
                            ),
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
                            detail=(
                                f"File is too large: exceeded {MAX_UPLOAD_SIZE_BYTES} bytes while downloading."
                            ),
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
        "model_id": DEFAULT_REMEDIATION_MODEL,
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
        "perform_audit": True,
        "perform_remediation": parse_bool(os.getenv("PERFORM_REMEDIATION", "TRUE"), True),
    }


def cleanup_paths(*paths: Path) -> None:
    for path in paths:
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
        except OSError:
            pass


def extract_html_from_payload(payload: Dict[str, object]) -> Optional[str]:
    document = payload.get("document")
    if isinstance(document, dict):
        representation = document.get("representation")
        if isinstance(representation, dict):
            html = representation.get("html")
            if isinstance(html, str) and html.strip():
                return html

    pages = payload.get("pages")
    if isinstance(pages, list):
        for page in pages:
            if not isinstance(page, dict):
                continue
            representation = page.get("representation")
            if isinstance(representation, dict):
                html = representation.get("html")
                if isinstance(html, str) and html.strip():
                    return html

    representation = payload.get("representation")
    if isinstance(representation, dict):
        html = representation.get("html")
        if isinstance(html, str) and html.strip():
            return html

    html = payload.get("html")
    if isinstance(html, str) and html.strip():
        return html

    return None


def resolve_html_output(output_dir: Path) -> Path:
    html_files = sorted(p for p in output_dir.rglob("*.html") if p.is_file())
    if html_files:
        return html_files[0]

    for result_json in sorted(p for p in output_dir.rglob("result.json") if p.is_file()):
        try:
            payload = json.loads(result_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        html = extract_html_from_payload(payload)
        if html:
            fd, temp_name = tempfile.mkstemp(prefix="remediated_", suffix=".html")
            os.close(fd)
            temp_path = Path(temp_name)
            temp_path.write_text(html, encoding="utf-8")
            return temp_path

    raise HTTPException(
        status_code=500,
        detail="No HTML output was produced by the remediation process.",
    )

def create_zip_package(source_dir: Path, archive_stem: str) -> Path:
    fd, zip_name = tempfile.mkstemp(
        prefix=f"{archive_stem}_",
        suffix=".zip",
    )
    os.close(fd)

    zip_path = Path(zip_name)

    with zipfile.ZipFile(
        zip_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                archive.write(
                    path,
                    path.relative_to(source_dir).as_posix(),
                )

    return zip_path

async def process_local_pdf(
    pdf_path: Path,
    original_name: str,
    download_dir: Path,
    output_dir: Path,
    debug_mode: bool,
) -> FileResponse:
    config = build_process_config()

    if debug_mode:
        logger.info("Remediation configuration: %s", config)

    zip_path: Optional[Path] = None

    try:
        await run_in_threadpool(
            process_pdf_accessibility,
            pdf_path=str(pdf_path),
            output_dir=str(output_dir),
            conversion_options=config["conversion_options"],
            audit_options=config["audit_options"],
            remediation_options=config["remediation_options"],
            perform_audit=config["perform_audit"],
            perform_remediation=config["perform_remediation"],
        )

        # The accessibility utility creates the complete output tree.
        # Package everything it produced rather than selecting one HTML file.
        archive_stem = Path(original_name).stem or "remediated"
        zip_path = create_zip_package(output_dir, archive_stem)

        download_name = f"{archive_stem}.zip"

        background = None
        if not debug_mode:
            background = BackgroundTask(
                cleanup_paths,
                download_dir,
                output_dir,
                zip_path,
            )

        response = FileResponse(
            path=str(zip_path),
            media_type="application/zip",
            filename=download_name,
            background=background,
        )

        response.headers["Content-Disposition"] = (
            f'attachment; filename="{download_name}"'
        )

        return response

    except Exception:
        if not debug_mode:
            cleanup_paths(download_dir, output_dir)

            if zip_path is not None:
                cleanup_paths(zip_path)

        raise

@app.get("/", response_class=HTMLResponse)
async def home() -> str:
    return render_home_page()


@app.get("/health", response_class=JSONResponse)
async def health_check() -> dict:
    try:
        from content_accessibility_utility_on_aws.api import process_pdf_accessibility as _process_pdf_accessibility  # noqa: F401
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


@app.post("/upload")
async def upload_and_remediate_pdf(
    file: UploadFile = File(...),
    debug: Optional[str] = Query(None),
) -> FileResponse:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Please upload a PDF file.")
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Please upload a PDF file.")

    download_dir = Path(tempfile.mkdtemp(prefix="pdf_upload_"))
    output_dir = Path(tempfile.mkdtemp(prefix="pdf_remediation_out_"))
    uploaded_file = download_dir / sanitize_filename(file.filename)
    debug_mode = debug is not None

    try:
        with uploaded_file.open("wb") as destination:
            shutil.copyfileobj(file.file, destination)

        return await process_local_pdf(
            pdf_path=uploaded_file,
            original_name=file.filename,
            download_dir=download_dir,
            output_dir=output_dir,
            debug_mode=debug_mode,
        )
    finally:
        await file.close()
        if debug_mode:
            logger.info(
                "Debug mode enabled; preserving temporary directories %s and %s",
                download_dir,
                output_dir,
            )


@app.post("/remediate")
async def remediate_pdf(
    url: Optional[str] = Form(None),
    Apix_Ldp_Resource: Optional[str] = Header(None, alias="Apix-Ldp-Resource"),
    debug: Optional[str] = Query(None),
) -> FileResponse:
    source_url = url or Apix_Ldp_Resource
    logger.info("Received remediation request for URL=%s debug=%s", source_url, debug)

    if not source_url:
        raise HTTPException(status_code=400, detail="A PDF URL is required.")
    if not is_allowed_domain(source_url):
        logger.warning("Rejected unsupported domain for URL=%s", source_url)
        raise HTTPException(status_code=403, detail="PDF URL is not in the allowed domain list.")

    sanitized_name = sanitize_filename(source_url)
    download_dir = Path(tempfile.mkdtemp(prefix="pdf_download_"))
    output_dir = Path(tempfile.mkdtemp(prefix="pdf_remediation_out_"))
    downloaded_file = download_dir / sanitized_name
    debug_mode = debug is not None

    try:
        await download_pdf(source_url, downloaded_file)
        return await process_local_pdf(
            pdf_path=downloaded_file,
            original_name=sanitized_name,
            download_dir=download_dir,
            output_dir=output_dir,
            debug_mode=debug_mode,
        )
    except Exception:
        if not debug_mode:
            cleanup_paths(download_dir, output_dir)
        raise
    finally:
        if debug_mode:
            logger.info(
                "Debug mode enabled; preserving temporary directories %s and %s",
                download_dir,
                output_dir,
            )

