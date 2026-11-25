"""QR Print Server - FastAPI backend for universal QR-based printing."""

from __future__ import annotations

import csv
import io
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote

import qrcode
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from job_manager import PrintJobManager
from printer_engine import ALL_EXTENSIONS, count_file_pages, list_printers, parse_page_range
from registry import get_shop_route, normalize_shop_id, save_shop_route, send_heartbeat
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
BRANDING_DIR = Path("/tmp/qr_print_branding") if os.environ.get("VERCEL") else RUNTIME_DIR / "branding"
BRANDING_DIR.mkdir(parents=True, exist_ok=True)
LOGO_PATH = BRANDING_DIR / "shop_logo.png"
if LOGO_PATH.exists():
    pass

MAX_FILE_SIZE_MB = 50
MAX_FILES_PER_UPLOAD = 10
SERVER_PORT = int(os.environ.get("QR_PRINT_PORT", "8500"))

app = FastAPI(title="QR Print System", version="3.0.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

job_manager = PrintJobManager(upload_dir=UPLOAD_DIR)
job_manager.has_logo = LOGO_PATH.exists()
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
    if job_manager.registry_synced and job_manager.registry_url and job_manager.shop_id:
        return f"{job_manager.registry_url.rstrip('/')}/s/{job_manager.shop_id}"
    if job_manager.custom_base_url:
        return job_manager.custom_base_url.rstrip("/")
    if os.environ.get("VERCEL") and request is not None:
        return str(request.base_url).rstrip("/")
    return f"http://{get_local_ip()}:{SERVER_PORT}"


def _sync_registry_now() -> None:
    if os.environ.get("VERCEL"):
        return
    live_url = job_manager.custom_base_url
    if not live_url or "trycloudflare.com" not in live_url:
        return
    ok = send_heartbeat(
        job_manager.registry_url,
        job_manager.shop_id,
        live_url,
        job_manager.shop_name,
    )
    if ok:
        job_manager.registry_synced = True


class SettingsUpdate(BaseModel):
    active_printer: Optional[str] = None
    auto_print: Optional[bool] = None
    custom_base_url: Optional[str] = None
    shop_name: Optional[str] = None
    shop_id: Optional[str] = None
    registry_url: Optional[str] = None
    shop_phone: Optional[str] = None
    upi_id: Optional[str] = None
    price_bw: Optional[float] = None
    price_color: Optional[float] = None


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
        "shop_id": job_manager.shop_id,
        "registry_url": job_manager.registry_url,
        "is_permanent_qr": bool(job_manager.registry_synced and job_manager.registry_url and job_manager.shop_id),
        "shop_phone": job_manager.shop_phone,
        "upi_id": job_manager.upi_id,
        "has_logo": LOGO_PATH.exists(),
        "price_bw": job_manager.price_bw,
        "price_color": job_manager.price_color,
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
    if payload.shop_id is not None:
        clean_id = normalize_shop_id(payload.shop_id)
        if len(clean_id) < 2:
            raise HTTPException(400, "Shop ID must be at least 2 characters (letters/numbers/hyphens).")
        job_manager.shop_id = clean_id
        threading.Thread(target=_sync_registry_now, daemon=True).start()
    if payload.registry_url is not None:
        job_manager.registry_url = payload.registry_url.strip().rstrip("/")
        threading.Thread(target=_sync_registry_now, daemon=True).start()
    if payload.shop_phone is not None:
        job_manager.shop_phone = payload.shop_phone.strip()
    if payload.upi_id is not None:
        job_manager.upi_id = payload.upi_id.strip()
    if payload.price_bw is not None:
        job_manager.price_bw = max(0.0, round(float(payload.price_bw), 2))
    if payload.price_color is not None:
        job_manager.price_color = max(0.0, round(float(payload.price_color), 2))
    return {
        "status": "ok",
        "active_printer": job_manager.active_printer,
        "auto_print": job_manager.auto_print,
        "custom_base_url": job_manager.custom_base_url,
        "shop_name": job_manager.shop_name,
        "shop_id": job_manager.shop_id,
        "registry_url": job_manager.registry_url,
        "is_permanent_qr": bool(job_manager.registry_synced and job_manager.registry_url and job_manager.shop_id),
        "shop_phone": job_manager.shop_phone,
        "upi_id": job_manager.upi_id,
        "has_logo": LOGO_PATH.exists(),
        "price_bw": job_manager.price_bw,
        "price_color": job_manager.price_color,
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


@app.get("/api/upi-qr")
def generate_upi_qr(amount: float = 0.0, note: str = "QR Print"):
    if not job_manager.upi_id:
        raise HTTPException(400, "Shop UPI ID not configured.")
    shop = quote(job_manager.shop_name or "QR Print Shop")
    tn = quote(note)
    upi_uri = f"upi://pay?pa={quote(job_manager.upi_id)}&pn={shop}&am={amount:.2f}&cu=INR&tn={tn}"
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=2)
    qr.add_data(upi_uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#0f172a", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


@app.post("/api/logo")
async def upload_logo(file: UploadFile = File(...)):
    content = await file.read()
    if not content or len(content) > 5 * 1024 * 1024:
        raise HTTPException(400, "Logo must be non-empty and under 5MB.")
    LOGO_PATH.write_bytes(content)
    job_manager.has_logo = True
    return {"status": "ok", "has_logo": True}


@app.get("/api/logo")
def get_logo():
    if not LOGO_PATH.exists():
        raise HTTPException(404, "No logo uploaded.")
    return FileResponse(LOGO_PATH)


@app.get("/api/report/csv")
def download_csv_report():
    jobs = job_manager.list_jobs()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Token", "Time", "Customer", "File", "Color", "Sides", "Pages", "Copies", "Amount (INR)", "Status"])
    total_bw_pages = 0
    total_col_pages = 0
    total_rev = 0.0
    for j in jobs:
        writer.writerow([
            f"#{j.get('token', '')}", j.get("created_at", ""), j.get("customer_name", ""),
            j.get("filename", ""), j.get("color_mode", ""), j.get("sides", ""),
            j.get("selected_pages", 0), j.get("copies", 1), j.get("total_price", 0), j.get("status", ""),
        ])
        if j.get("status") == "printed":
            pgs = int(j.get("selected_pages", 0)) * int(j.get("copies", 1))
            if j.get("color_mode") == "bw":
                total_bw_pages += pgs
            else:
                total_col_pages += pgs
            total_rev += float(j.get("total_price", 0))
    writer.writerow([])
    writer.writerow(["SUMMARY", f"B&W Pages: {total_bw_pages}", f"Color Pages: {total_col_pages}", f"Total Revenue: INR {total_rev:.2f}"])
    buf.seek(0)
    filename = f"print_report_{datetime.now().strftime('%Y-%m-%d')}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _startup_shortcut_path() -> Optional[Path]:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "QR Print Shop.lnk"


@app.get("/api/autostart")
def get_autostart_status():
    lnk = _startup_shortcut_path()
    return {"supported": sys.platform == "win32" and lnk is not None, "enabled": bool(lnk and lnk.exists())}


@app.post("/api/autostart")
def toggle_autostart(enable: bool = True):
    lnk = _startup_shortcut_path()
    if sys.platform != "win32" or lnk is None:
        raise HTTPException(400, "Auto-start is only supported on Windows.")
    if not enable:
        lnk.unlink(missing_ok=True)
        return {"enabled": False}
    target = str(RUNTIME_DIR / "start_server.bat")
    if getattr(sys, "frozen", False):
        target = sys.executable
    ps_cmd = (
        f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
        f"$s.TargetPath='{target}';"
        f"$s.WorkingDirectory='{RUNTIME_DIR}';"
        f"$s.WindowStyle=7;"
        f"$s.Save()"
    )
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd], check=True)
    return {"enabled": lnk.exists()}


@app.post("/api/inspect")
async def inspect_file(file: UploadFile = File(...)):
    """Count pages of a selected file before printing so the customer sees page count."""
    orig_name = Path(file.filename or "document").name
    ext = Path(orig_name).suffix.lower()
    if ext not in ALL_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type '{ext}'.")
    content = await file.read()
    if not content:
        raise HTTPException(400, "File is empty.")
    if len(content) / (1024 * 1024) > MAX_FILE_SIZE_MB:
        raise HTTPException(400, f"File too large (max {MAX_FILE_SIZE_MB}MB).")

    tmp_path = UPLOAD_DIR / f"inspect_{uuid.uuid4().hex[:8]}_{orig_name}"
    tmp_path.write_bytes(content)
    try:
        pages = count_file_pages(tmp_path)
        return {"filename": orig_name, "pages": pages}
    except Exception as exc:
        raise HTTPException(400, str(exc))
    finally:
        tmp_path.unlink(missing_ok=True)


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
    jobs = job_manager.list_jobs()
    revenue = round(sum(j.get("total_price", 0.0) for j in jobs if j.get("status") == "printed"), 2)
    return {
        "jobs": jobs, "auto_print": job_manager.auto_print,
        "active_printer": job_manager.active_printer, "has_new_job": job_manager.has_new_job,
        "total_revenue": revenue,
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


class HeartbeatPayload(BaseModel):
    shop_id: str
    target_url: str
    shop_name: str = ""


@app.post("/api/registry/heartbeat")
def registry_heartbeat(payload: HeartbeatPayload):
    try:
        rec = save_shop_route(payload.shop_id, payload.target_url, payload.shop_name)
        return {"status": "ok", "route": rec}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/s/{shop_id}")
def redirect_permanent_qr(shop_id: str):
    rec = get_shop_route(shop_id)
    if rec and rec.get("target_url"):
        return RedirectResponse(url=rec["target_url"], status_code=307)
    return HTMLResponse(
        content=f"""<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
        <title>Shop Offline</title></head><body style="font-family:sans-serif;background:#0b1120;color:#f8fafc;
        display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:24px;text-align:center;">
        <div style="max-width:400px;background:#162032;border:1px solid #2b3950;border-radius:16px;padding:28px;">
        <div style="font-size:2.5rem;">🖨️</div>
        <h2 style="margin:10px 0 6px;">Shop Printer Offline</h2>
        <p style="color:#94a3b8;font-size:0.95rem;">Shop <code>{shop_id}</code> is not connected right now.
        Please ask the shopkeeper to open <b>QR Print Shop</b> on their PC.</p>
        <button onclick="location.reload()" style="margin-top:14px;padding:10px 22px;border-radius:10px;
        border:0;background:#3b82f6;color:#fff;font-weight:700;cursor:pointer;">Retry</button>
        </div></body></html>""",
        status_code=503,
    )


if __name__ == "__main__":
    import uvicorn
    logger.info("Starting QR Print Server on http://%s:%d", get_local_ip(), SERVER_PORT)
    if os.environ.get("DISABLE_TUNNEL") != "1" and not os.environ.get("VERCEL"):
        def _on_tunnel_url(url: str):
            if not job_manager.custom_base_url or "trycloudflare.com" in job_manager.custom_base_url:
                job_manager.custom_base_url = url
                threading.Thread(target=_sync_registry_now, daemon=True).start()

        def _heartbeat_loop():
            while True:
                time.sleep(45)
                _sync_registry_now()

        tunnel_manager.start(SERVER_PORT, _on_tunnel_url)
        threading.Thread(target=_heartbeat_loop, daemon=True).start()
    if getattr(sys, "frozen", False) or os.environ.get("OPEN_ADMIN_BROWSER") == "1":
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{SERVER_PORT}/admin")).start()
    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT)
