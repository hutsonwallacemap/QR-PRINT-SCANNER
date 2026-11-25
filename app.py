"""QR Print Server - FastAPI backend for universal QR-based printing."""

from __future__ import annotations

import io
import logging
import os
import socket
import sys
import threading
import uuid
import webbrowser
from pathlib import Path
from typing import List, Optional

import qrcode
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from job_manager import PrintJobManager
from printer_engine import (
    IMAGE_EXTENSIONS,
    PDF_EXTENSIONS,
    TEXT_EXTENSIONS,
    list_printers,
    parse_page_range,
)
from tunnel import PublicTunnelManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("qr_print.app")

if getattr(sys, "frozen", False):
    BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    RUNTIME_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).resolve().parent
    RUNTIME_DIR = BASE_DIR

STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR = (
    Path("/tmp/qr_print_uploads")
    if os.environ.get("VERCEL")
    else RUNTIME_DIR / "uploads"
)
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS | PDF_EXTENSIONS | TEXT_EXTENSIONS

MAX_FILE_SIZE_MB = 50
MAX_FILES_PER_UPLOAD = 10

SERVER_PORT = int(os.environ.get("QR_PRINT_PORT", "8500"))

app = FastAPI(title="Universal QR Print System", version="2.0.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

job_manager = PrintJobManager(upload_dir=UPLOAD_DIR)
tunnel_manager = PublicTunnelManager()


def get_local_ip() -> str:
    """Detect the computer's primary LAN IP address for Wi-Fi QR scanning."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


def get_qr_target_url(request: Optional[Request] = None) -> str:
    if job_manager.custom_base_url:
        return job_manager.custom_base_url.rstrip("/")
    if os.environ.get("VERCEL") and request is not None:
        return str(request.base_url).rstrip("/")
    ip = get_local_ip()
    return f"http://{ip}:{SERVER_PORT}"


class SettingsUpdate(BaseModel):
    active_printer: Optional[str] = None
    auto_print: Optional[bool] = None
    custom_base_url: Optional[str] = None
    shop_name: Optional[str] = None


@app.get("/", response_class=HTMLResponse)
def customer_upload_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "admin.html")


@app.get("/qr-poster", response_class=HTMLResponse)
def qr_poster_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "qr_poster.html")


@app.get("/api/info")
def server_info(request: Request) -> dict:
    qr_url = get_qr_target_url(request)
    return {
        "local_ip": get_local_ip(),
        "port": SERVER_PORT,
        "qr_url": qr_url,
        "active_printer": job_manager.active_printer,
        "auto_print": job_manager.auto_print,
        "custom_base_url": job_manager.custom_base_url,
        "shop_name": job_manager.shop_name,
        "tunnel_status": tunnel_manager.status,
        "is_public": bool(job_manager.custom_base_url or os.environ.get("VERCEL")),
        "allowed_extensions": sorted(ALLOWED_EXTENSIONS),
        "max_file_size_mb": MAX_FILE_SIZE_MB,
        "max_files": MAX_FILES_PER_UPLOAD,
    }


@app.get("/api/printers")
def get_printers() -> dict:
    return {
        "printers": list_printers(),
        "active_printer": job_manager.active_printer,
    }


@app.post("/api/settings")
def update_settings(payload: SettingsUpdate) -> dict:
    if payload.active_printer is not None:
        available = {p["name"] for p in list_printers()}
        if payload.active_printer and payload.active_printer not in available:
            raise HTTPException(status_code=400, detail="Selected printer was not found.")
        job_manager.active_printer = payload.active_printer

    if payload.auto_print is not None:
        job_manager.auto_print = payload.auto_print

    if payload.custom_base_url is not None:
        job_manager.custom_base_url = payload.custom_base_url.strip()

    if payload.shop_name is not None:
        job_manager.shop_name = payload.shop_name.strip()

    return {
        "status": "ok",
        "active_printer": job_manager.active_printer,
        "auto_print": job_manager.auto_print,
        "custom_base_url": job_manager.custom_base_url,
        "shop_name": job_manager.shop_name,
        "qr_url": get_qr_target_url(),
    }


@app.get("/api/qr")
def generate_qr(request: Request, url: Optional[str] = None) -> StreamingResponse:
    target = (url or get_qr_target_url(request)).strip()
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(target)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#0f172a", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


@app.post("/api/upload")
async def upload_and_print(
    files: List[UploadFile] = File(...),
    color_mode: str = Form("color"),
    page_range: str = Form("all"),
    copies: int = Form(1),
    orientation: str = Form("auto"),
    customer_name: str = Form("Walk-in Customer"),
    printer_name: Optional[str] = Form(None),
) -> JSONResponse:
    if not files:
        raise HTTPException(status_code=400, detail="Please select at least one file.")

    if len(files) > MAX_FILES_PER_UPLOAD:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {MAX_FILES_PER_UPLOAD} files per upload. You sent {len(files)}.",
        )

    created_jobs = []
    errors = []

    for upload in files:
        orig_name = Path(upload.filename or "document").name
        ext = Path(orig_name).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            errors.append(f"{orig_name}: Unsupported file type '{ext}'.")
            continue

        safe_name = f"{uuid.uuid4().hex[:8]}_{orig_name}"
        dest_path = UPLOAD_DIR / safe_name

        content = await upload.read()
        if not content:
            errors.append(f"{orig_name}: File is empty.")
            continue

        file_size_mb = len(content) / (1024 * 1024)
        if file_size_mb > MAX_FILE_SIZE_MB:
            errors.append(f"{orig_name}: File too large ({file_size_mb:.1f}MB). Maximum is {MAX_FILE_SIZE_MB}MB.")
            continue

        dest_path.write_bytes(content)

        try:
            job = job_manager.create_job(
                filename=orig_name,
                file_path=dest_path,
                color_mode=color_mode,
                page_range=page_range,
                copies=copies,
                orientation=orientation,
                printer_name=printer_name or None,
                customer_name=customer_name,
            )
            parse_page_range(job.page_range, job.total_file_pages)
            created_jobs.append(job.to_dict())
        except Exception as exc:
            dest_path.unlink(missing_ok=True)
            errors.append(f"{orig_name}: {exc}")

    if not created_jobs and errors:
        raise HTTPException(status_code=400, detail=" | ".join(errors))

    return JSONResponse(
        {
            "status": "accepted",
            "auto_print": job_manager.auto_print,
            "jobs": created_jobs,
            "errors": errors,
        }
    )


@app.get("/api/jobs")
def list_jobs() -> dict:
    return {
        "jobs": job_manager.list_jobs(),
        "auto_print": job_manager.auto_print,
        "active_printer": job_manager.active_printer,
        "has_new_job": job_manager.has_new_job,
    }


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/jobs/{job_id}/reprint")
def reprint_job(job_id: str) -> dict:
    try:
        job = job_manager.approve_or_reprint(job_id)
        return {"status": "ok", "job": job.to_dict()}
    except KeyError:
        raise HTTPException(status_code=404, detail="Job not found")


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    if not job_manager.delete_job(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"status": "deleted"}


if __name__ == "__main__":
    import uvicorn

    logger.info("Starting QR Print Server on http://%s:%d", get_local_ip(), SERVER_PORT)
    if os.environ.get("DISABLE_TUNNEL") != "1" and not os.environ.get("VERCEL"):
        def _on_tunnel_url(url: str) -> None:
            if not job_manager.custom_base_url:
                job_manager.custom_base_url = url

        tunnel_manager.start(SERVER_PORT, _on_tunnel_url)

    if getattr(sys, "frozen", False) or os.environ.get("OPEN_ADMIN_BROWSER") == "1":
        threading.Timer(
            1.5, lambda: webbrowser.open(f"http://localhost:{SERVER_PORT}/admin")
        ).start()
    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT)
