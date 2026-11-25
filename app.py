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
from printer_engine import ALL_EXTENSIONS, list_printers, parse_page_range
from tunnel import PublicTunnelManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("qr_print.app")

if getattr(sys, "frozen", False):
    BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    RUNTIME_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).resolve().parent
    RUNTIME_DIR = BASE_DIR

STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR = Path("/tmp/qr_print_uploads") if os.environ.get("VERCEL") else RUNTIME_DIR / "uploads"

MAX_FILE_SIZE_MB = 50
MAX_FILES_PER_UPLOAD = 10
SERVER_PORT = int(os.environ.get("QR_PRINT_PORT", "8500"))

app = FastAPI(title="QR Print System", version="3.0.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

job_manager = PrintJobManager(upload_dir=UPLOAD_DIR)
tunnel_manager = PublicTunnelManager()


def get_local_ip() -> str:
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
    return f"http://{get_local_ip()}:{SERVER_PORT}"


class SettingsUpdate(BaseModel):
    active_printer: Optional[str] = None
    auto_print: Optional[bool] = None
    custom_base_url: Optional[str] = None
    shop_name: Optional[str] = None


# ── Pages ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def customer_page():
    return FileResponse(STATIC_DIR / "index.html")

@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return FileResponse(STATIC_DIR / "admin.html")

@app.get("/qr-poster", response_class=HTMLResponse)
def poster_page():
    return FileResponse(STATIC_DIR / "qr_poster.html")


# ── API ──────────────────────────────────────────────────────────────

@app.get("/api/info")
def server_info(request: Request):
    return {
        "local_ip": get_local_ip(),
        "port": SERVER_PORT,
        "qr_url": get_qr_target_url(request),
        "active_printer": job_manager.active_printer,
        "auto_print": job_manager.auto_print,
        "custom_base_url": job_manager.custom_base_url,
        "shop_name": job_manager.shop_name,
        "tunnel_status": tunnel_manager.status,
        "is_public": bool(job_manager.custom_base_url or os.environ.get("VERCEL")),
        "allowed_extensions": sorted(ALL_EXTENSIONS),
        "max_file_size_mb": MAX_FILE_SIZE_MB,
        "max_files": MAX_FILES_PER_UPLOAD,
    }


@app.get("/api/printers")
def get_printers():
    return {"printers": list_printers(), "active_printer": job_manager.active_printer}


@app.post("/api/settings")
def update_settings(payload: SettingsUpdate):
    if payload.active_printer is not None:
        available = {p["name"] for p in list_printers()}
        if payload.active_printer and payload.active_printer not in available:
            raise HTTPException(400, "Printer not found.")
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
def generate_qr(request: Request, url: Optional[str] = None):
    target = (url or get_qr_target_url(request)).strip()
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
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
    sides: str = Form("one"),
    page_range: str = Form("all"),
    copies: int = Form(1),
    orientation: str = Form("auto"),
    paper_size: str = Form("A4"),
    customer_name: str = Form("Walk-in Customer"),
    printer_name: Optional[str] = Form(None),
):
    if not files:
        raise HTTPException(400, "Please select at least one file.")
    if len(files) > MAX_FILES_PER_UPLOAD:
        raise HTTPException(400, f"Maximum {MAX_FILES_PER_UPLOAD} files per upload.")

    created_jobs, errors = [], []

    for upload in files:
        orig_name = Path(upload.filename or "document").name
        ext = Path(orig_name).suffix.lower()
        if ext not in ALL_EXTENSIONS:
            errors.append(f"{orig_name}: Unsupported file type '{ext}'. Supported: images, PDF, Word, TXT.")
            continue

        content = await upload.read()
        if not content:
            errors.append(f"{orig_name}: File is empty.")
            continue
        if len(content) / (1024 * 1024) > MAX_FILE_SIZE_MB:
            errors.append(f"{orig_name}: File too large (max {MAX_FILE_SIZE_MB}MB).")
            continue

        safe_name = f"{uuid.uuid4().hex[:8]}_{orig_name}"
        dest_path = UPLOAD_DIR / safe_name
        dest_path.write_bytes(content)

        try:
            job = job_manager.create_job(
                filename=orig_name, file_path=dest_path, color_mode=color_mode,
                sides=sides, page_range=page_range, copies=copies,
                orientation=orientation, paper_size=paper_size,
                printer_name=printer_name or None, customer_name=customer_name,
            )
            parse_page_range(job.page_range, job.total_file_pages)
            created_jobs.append(job.to_dict())
        except Exception as exc:
            dest_path.unlink(missing_ok=True)
            errors.append(f"{orig_name}: {exc}")

    if not created_jobs and errors:
        raise HTTPException(400, " | ".join(errors))

    return JSONResponse({"status": "accepted", "auto_print": job_manager.auto_print, "jobs": created_jobs, "errors": errors})


@app.get("/api/jobs")
def list_jobs():
    return {
        "jobs": job_manager.list_jobs(), "auto_print": job_manager.auto_print,
        "active_printer": job_manager.active_printer, "has_new_job": job_manager.has_new_job,
    }

@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job

@app.post("/api/jobs/{job_id}/reprint")
def reprint_job(job_id: str):
    try:
        job = job_manager.approve_or_reprint(job_id)
        return {"status": "ok", "job": job.to_dict()}
    except KeyError:
        raise HTTPException(404, "Job not found")

@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    if not job_manager.delete_job(job_id):
        raise HTTPException(404, "Job not found")
    return {"status": "deleted"}


if __name__ == "__main__":
    import uvicorn
    logger.info("Starting QR Print Server on http://%s:%d", get_local_ip(), SERVER_PORT)
    if os.environ.get("DISABLE_TUNNEL") != "1" and not os.environ.get("VERCEL"):
        def _on_tunnel_url(url: str):
            if not job_manager.custom_base_url:
                job_manager.custom_base_url = url
        tunnel_manager.start(SERVER_PORT, _on_tunnel_url)
    if getattr(sys, "frozen", False) or os.environ.get("OPEN_ADMIN_BROWSER") == "1":
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{SERVER_PORT}/admin")).start()
    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT)
