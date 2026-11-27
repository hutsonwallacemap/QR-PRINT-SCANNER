"""HTTP API + pages."""

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
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import qrcode
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, documents, printer
from .auth import (SESSION_COOKIE, SESSION_TTL, RateLimiter, SessionManager, check_pin, client_ip, hash_pin,
                   is_local_request, require_admin, valid_new_pin)
from .config import (EDITABLE_FIELDS, INSTALL_DIR, LOGO_PATH, MAX_COPIES, MAX_FILE_MB, MAX_FILES_PER_ORDER,
                     PAPER_SIZES, PORT, STATIC_DIR, VIRTUAL_PRINT_DIR, SettingsStore)
from .db import Database, today
from .orders import OrderError, OrderService, public_order
from .payments import PaymentError, Razorpay, upi_uri
from .registry import RegistryStore, normalize_shop_id
from .tunnel import PublicTunnelManager

logger = logging.getLogger("qrprint.server")


def local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def png_qr(data: str, box: int = 10) -> bytes:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=box, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(fill_color="#111827", back_color="white").save(buf, format="PNG")
    return buf.getvalue()


NO_STORE = {"Cache-Control": "no-store"}


class Ctx:
    """Everything the app needs; tests build one with temp folders."""

    def __init__(self, db: Database, settings: SettingsStore, orders: OrderService,
                 tunnel: Optional[PublicTunnelManager] = None, registry: Optional[RegistryStore] = None):
        self.db, self.settings, self.orders = db, settings, orders
        self.tunnel = tunnel or PublicTunnelManager()
        self.registry = registry
        self.sessions = SessionManager()
        self.login_limiter = RateLimiter(6, 300)
        self.upload_limiter = RateLimiter(80, 600)
        self.order_limiter = RateLimiter(20, 600)
        self.registry_status = ""
        self.tunnel_allowed = False  # set by run.py when the internet tunnel is started
        self.started = time.time()

    def mobile_status(self) -> Dict[str, Any]:
        """Can a customer on 4G/5G reach this shop right now?"""
        s = self.settings.data
        if s.public_url:
            return {"ok": True, "text": f"Working - fixed address {s.public_url}"}
        if not (s.enable_tunnel and self.tunnel_allowed):
            return {"ok": False, "text": "Off - only phones on the shop Wi-Fi can print"}
        t = self.tunnel.status
        if t == "cloudflared_not_installed":
            return {"ok": False, "text": "Not working - cloudflared is not installed (run Install.bat)"}
        if t != "connected":
            return {"ok": False, "text": "Connecting to the internet..." if t in ("starting", "inactive") else f"Not working - {t}"}
        if s.registry_url and s.shop_id:
            if self.registry_status == "ok":
                return {"ok": True, "text": "Working - customers can use Wi-Fi or mobile data"}
            if self.registry_status:
                return {"ok": False, "text": f"Permanent QR not updated: {self.registry_status}"}
            return {"ok": False, "text": "Registering the permanent QR..."}
        return {"ok": True, "text": "Working, but the QR changes after every restart (no registry set)"}

    def customer_url(self) -> str:
        s = self.settings.data
        if s.public_url:
            return s.public_url.rstrip("/")
        # The registry address never changes, so the printed poster keeps working
        # even though the free tunnel gets a new address after every restart.
        if s.registry_url and s.shop_id and s.enable_tunnel and self.tunnel_allowed:
            return f"{s.registry_url.rstrip('/')}/s/{s.shop_id}"
        if self.tunnel.public_url:
            return self.tunnel.public_url
        return f"http://{local_ip()}:{PORT}"


# ── request bodies ────────────────────────────────────────────────────

class ItemReq(BaseModel):
    upload_id: str
    page_range: str = "all"
    color_mode: str = "bw"
    sides: str = "one"
    copies: int = 1
    paper_size: str = "A4"
    orientation: str = "auto"


class QuoteReq(BaseModel):
    items: List[ItemReq] = Field(default_factory=list)


class OrderReq(QuoteReq):
    customer_name: str = ""
    pay_method: str = "online"


class RzpVerify(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class ClaimPaid(BaseModel):
    utr: str = ""


class PinReq(BaseModel):
    pin: str


class ChangePin(BaseModel):
    current_pin: str
    new_pin: str


class ConfirmPay(BaseModel):
    ref: str = ""


class Heartbeat(BaseModel):
    shop_id: str
    target_url: str
    secret: str = ""
    shop_name: str = ""


def create_app(ctx: Ctx) -> FastAPI:
    app = FastAPI(title="QR Print Shop", version=__version__, docs_url=None, redoc_url=None)
    app.state.ctx = ctx
    # Compress pages/scripts/JSON: noticeably faster for customers on 4G.
    app.add_middleware(GZipMiddleware, minimum_size=800)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    S = ctx.settings
    orders = ctx.orders

    def admin(request: Request) -> None:
        require_admin(request, ctx.sessions)

    @app.exception_handler(OrderError)
    async def _order_error(_: Request, exc: OrderError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if request.url.path.startswith("/admin"):
            resp.headers["X-Frame-Options"] = "DENY"
        return resp

    # ── pages ──
    def page(name: str) -> FileResponse:
        return FileResponse(STATIC_DIR / name, headers=NO_STORE)

    @app.get("/", response_class=HTMLResponse)
    def customer_page():
        return page("index.html")

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page():
        return page("admin.html")

    @app.get("/poster", response_class=HTMLResponse)
    def poster_page():
        return page("poster.html")

    @app.get("/healthz")
    def health():
        return {"ok": True, "version": __version__}

    # ── public shop info ──
    @app.get("/api/shop")
    def shop_info():
        s = S.data
        caps = orders.printer_caps()
        return JSONResponse({
            "name": s.shop_name, "phone": s.shop_phone, "address": s.shop_address,
            "has_logo": LOGO_PATH.exists(), "accepting_orders": s.accepting_orders,
            "prices": {"bw": s.price_bw, "color": s.price_color,
                       "bw_duplex": s.price_bw_duplex or s.price_bw,
                       "color_duplex": s.price_color_duplex or s.price_color,
                       "min_order": s.min_order_amount},
            "caps": caps, "payments": orders.payment_options(),
            "counter_mode": s.counter_payment,
            "limits": {"max_file_mb": MAX_FILE_MB, "max_files": MAX_FILES_PER_ORDER, "max_copies": MAX_COPIES},
            "extensions": sorted(documents.ALL_EXT),
            "word_supported": documents.office_converter_available(),
        }, headers=NO_STORE)

    @app.get("/api/logo")
    def get_logo():
        if not LOGO_PATH.exists():
            raise HTTPException(404, "No logo")
        return FileResponse(LOGO_PATH, headers={"Cache-Control": "no-cache"})

    @app.get("/api/qr.png")
    def shop_qr():
        return Response(png_qr(ctx.customer_url()), media_type="image/png", headers=NO_STORE)

    # ── customer flow ──
    @app.post("/api/uploads")
    def upload(request: Request, file: UploadFile = File(...)):
        if not S.data.accepting_orders:
            raise HTTPException(403, "The shop is not taking orders right now.")
        if not ctx.upload_limiter.hit(client_ip(request)):
            raise HTTPException(429, "Too many uploads. Please wait a few minutes.")
        up = orders.stage_upload(file.filename or "document", file.file)
        return {"upload_id": up.id, "filename": up.filename, "pages": up.pages, "kind": up.kind}

    @app.get("/api/uploads/{upload_id}/preview.png")
    def upload_preview(upload_id: str, page: int = 1):
        up = orders.get_upload(upload_id)
        if not up:
            raise HTTPException(404, "Upload not found")
        try:
            data = documents.thumbnail_png(up.print_path, max(0, min(page, up.pages) - 1))
        except Exception:
            raise HTTPException(500, "Preview failed")
        return Response(data, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})

    @app.post("/api/quote")
    def quote(body: QuoteReq):
        return orders.quote([i.model_dump() for i in body.items])

    @app.post("/api/orders")
    def create_order(request: Request, body: OrderReq):
        if not ctx.order_limiter.hit(client_ip(request)):
            raise HTTPException(429, "Too many orders. Please ask at the counter.")
        order = orders.create_order([i.model_dump() for i in body.items], body.customer_name, body.pay_method)
        return order_payload(order, include_key=True)

    def load_order(order_id: str, k: str) -> Dict[str, Any]:
        o = ctx.db.get_order(order_id)
        if not o or not k or o["access_key"] != k:
            raise HTTPException(404, "Order not found")
        return o

    def order_payload(o: Dict[str, Any], include_key: bool = False) -> Dict[str, Any]:
        s = S.data
        out = public_order(o)
        if include_key:
            out["access_key"] = o["access_key"]
        if o["status"] in ("queued", "waiting_approval"):
            out["queue_ahead"] = ctx.db.queue_ahead(o)
        if o["status"] == "awaiting_payment":
            if o["pay_method"] == "razorpay":
                out["razorpay"] = {"key_id": s.razorpay_key_id, "order_id": o["rzp_order_id"],
                                   "amount": o["amount_paise"], "name": s.shop_name,
                                   "description": f"Print token #{o['token']}"}
            elif o["pay_method"] == "upi":
                out["upi"] = {"uri": upi_uri(s.upi_id, s.upi_payee_name or s.shop_name, o["amount_paise"],
                                             f"Print token {o['token']}"),
                              "upi_id": s.upi_id}
        return out

    @app.get("/api/orders/{order_id}")
    def get_order(order_id: str, k: str = ""):
        o = load_order(order_id, k)
        if o["status"] == "awaiting_payment" and o["pay_method"] == "razorpay":
            if orders.reconcile_razorpay(o):
                o = ctx.db.get_order(order_id) or o
        return JSONResponse(order_payload(o), headers=NO_STORE)

    @app.post("/api/orders/{order_id}/razorpay")
    def razorpay_callback(order_id: str, body: RzpVerify, k: str = ""):
        o = load_order(order_id, k)
        if o["status"] != "awaiting_payment":
            return order_payload(o)
        if not orders.verify_razorpay(o, body.razorpay_order_id, body.razorpay_payment_id, body.razorpay_signature):
            # Signature failed - still ask Razorpay directly before giving up.
            if not orders.reconcile_razorpay(o, force=True):
                raise HTTPException(400, "Payment could not be verified. If money was deducted, show this "
                                         "screen at the counter.")
        return order_payload(ctx.db.get_order(order_id))  # type: ignore[arg-type]

    @app.post("/api/orders/{order_id}/claim-paid")
    def claim_paid(order_id: str, body: ClaimPaid, k: str = ""):
        load_order(order_id, k)
        orders.customer_claims_paid(order_id, body.utr)
        return order_payload(ctx.db.get_order(order_id))  # type: ignore[arg-type]

    @app.get("/api/orders/{order_id}/upi.png")
    def order_upi_qr(order_id: str, k: str = ""):
        o = load_order(order_id, k)
        s = S.data
        if not s.upi_id:
            raise HTTPException(404, "UPI not configured")
        uri = upi_uri(s.upi_id, s.upi_payee_name or s.shop_name, o["amount_paise"], f"Print token {o['token']}")
        return Response(png_qr(uri, box=8), media_type="image/png", headers=NO_STORE)

    # ── shopkeeper: auth ──
    @app.get("/api/admin/session")
    def admin_session(request: Request):
        return {
            "logged_in": ctx.sessions.valid(request.cookies.get(SESSION_COOKIE)),
            "pin_set": bool(S.data.admin_pin_hash),
            "local": is_local_request(request),
            "shop_name": S.data.shop_name,
        }

    def _start_session(resp: Response) -> None:
        resp.set_cookie(SESSION_COOKIE, ctx.sessions.create(), max_age=SESSION_TTL, httponly=True,
                        samesite="strict", path="/")

    @app.post("/api/admin/setup")
    def admin_setup(request: Request, body: PinReq, response: Response):
        if S.data.admin_pin_hash:
            raise HTTPException(400, "PIN already set.")
        if not is_local_request(request):
            raise HTTPException(403, "First-time setup must be done on the shop computer itself.")
        err = valid_new_pin(body.pin)
        if err:
            raise HTTPException(400, err)
        S.update(admin_pin_hash=hash_pin(body.pin))
        _start_session(response)
        return {"ok": True}

    @app.post("/api/admin/login")
    def admin_login(request: Request, body: PinReq, response: Response):
        ip = client_ip(request)
        if not S.data.admin_pin_hash:
            raise HTTPException(400, "Set up a PIN on the shop computer first.")
        if not ctx.login_limiter.hit(ip):
            raise HTTPException(429, "Too many wrong attempts. Wait 5 minutes.")
        if not check_pin(body.pin, S.data.admin_pin_hash):
            raise HTTPException(401, "Wrong PIN.")
        ctx.login_limiter.reset(ip)
        _start_session(response)
        return {"ok": True}

    @app.post("/api/admin/logout")
    def admin_logout(request: Request, response: Response):
        ctx.sessions.revoke(request.cookies.get(SESSION_COOKIE))
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    @app.post("/api/admin/pin")
    def change_pin(request: Request, body: ChangePin, response: Response):
        admin(request)
        if not check_pin(body.current_pin, S.data.admin_pin_hash):
            raise HTTPException(401, "Current PIN is wrong.")
        err = valid_new_pin(body.new_pin)
        if err:
            raise HTTPException(400, err)
        S.update(admin_pin_hash=hash_pin(body.new_pin))
        ctx.sessions.revoke_all()
        _start_session(response)
        return {"ok": True}

    # ── shopkeeper: dashboard ──
    @app.get("/api/admin/state")
    def admin_state(request: Request):
        admin(request)
        s = S.data
        printers = printer.list_printers(fresh=True)
        return JSONResponse({
            "version": __version__,
            "settings": s.public_view(),
            "active_printer": orders.active_printer(),
            "color_printer": orders.printer_for(True),
            "printers": printers,
            "caps": orders.printer_caps(),
            "customer_url": ctx.customer_url(),
            "local_url": f"http://{local_ip()}:{PORT}",
            "tunnel": {"status": ctx.tunnel.status, "url": ctx.tunnel.public_url},
            "registry_status": ctx.registry_status,
            "mobile": ctx.mobile_status(),
            "word_supported": documents.office_converter_available(),
            "worker": orders.worker_state,
            "paper_sizes": PAPER_SIZES,
            "autostart": autostart_status(),
        }, headers=NO_STORE)

    @app.get("/api/admin/orders")
    def admin_orders(request: Request, rev: int = -1, day: str = ""):
        admin(request)
        if rev == ctx.db.revision and not day:
            return JSONResponse({"rev": rev, "changed": False}, headers=NO_STORE)
        d = day or today()
        rows = ctx.db.list_orders(limit=300, day=d)
        for o in rows:
            o.pop("access_key", None)
            for it in o["items"]:
                it.pop("stored_path", None)
                it.pop("print_path", None)
        return JSONResponse({"rev": ctx.db.revision, "changed": True, "day": d, "orders": rows,
                             "summary": ctx.db.day_summary(d)}, headers=NO_STORE)

    @app.post("/api/admin/orders/{order_id}/confirm-payment")
    def admin_confirm(request: Request, order_id: str, body: ConfirmPay):
        admin(request)
        orders.admin_confirm_payment(order_id, body.ref)
        return {"ok": True}

    @app.post("/api/admin/orders/{order_id}/print")
    def admin_print(request: Request, order_id: str):
        admin(request)
        orders.admin_print(order_id)
        return {"ok": True}

    @app.post("/api/admin/orders/{order_id}/flip-done")
    def admin_flip(request: Request, order_id: str):
        admin(request)
        orders.admin_flip_done(order_id)
        return {"ok": True}

    @app.post("/api/admin/orders/{order_id}/cancel")
    def admin_cancel(request: Request, order_id: str):
        admin(request)
        orders.admin_cancel(order_id)
        return {"ok": True}

    @app.get("/api/admin/items/{item_id}/file")
    def admin_file(request: Request, item_id: str):
        admin(request)
        row = ctx.db.get_item(item_id)
        if not row or not Path(row["stored_path"]).exists():
            raise HTTPException(404, "File no longer available (deleted for privacy).")
        return FileResponse(row["stored_path"], filename=row["filename"],
                            content_disposition_type="inline")

    @app.post("/api/admin/settings")
    def admin_settings(request: Request, body: Dict[str, Any]):
        admin(request)
        changes: Dict[str, Any] = {}
        for k, v in body.items():
            if k not in EDITABLE_FIELDS:
                continue
            changes[k] = v
        s = S.data
        # validation
        for k in ("price_bw", "price_color", "price_bw_duplex", "price_color_duplex", "min_order_amount"):
            if k in changes:
                try:
                    changes[k] = round(max(0.0, min(float(changes[k]), 10000.0)), 2)
                except (TypeError, ValueError):
                    raise HTTPException(400, f"{k} must be a number.")
        for k in ("auto_print", "accepting_orders", "enable_tunnel", "manual_duplex_reverse"):
            if k in changes:
                changes[k] = bool(changes[k])
        for k in ("shop_name", "shop_phone", "shop_address", "upi_id", "upi_payee_name", "public_url",
                  "registry_url", "razorpay_key_id", "printer_name"):
            if k in changes:
                changes[k] = str(changes[k] or "").strip()[:200]
        if "razorpay_key_secret" in changes:
            sec = str(changes["razorpay_key_secret"] or "").strip()
            if not sec:  # blank field in the form = keep existing secret
                changes.pop("razorpay_key_secret")
            else:
                changes["razorpay_key_secret"] = sec
        installed = None
        for k in ("printer_name", "printer_color"):
            if k in changes:
                changes[k] = str(changes[k] or "").strip()[:200]
                if changes[k]:
                    installed = installed or {p["name"] for p in printer.list_printers()}
                    if changes[k] not in installed:
                        raise HTTPException(400, "That printer is not installed on this PC.")
        if "duplex_mode" in changes and changes["duplex_mode"] not in ("auto", "manual", "off"):
            raise HTTPException(400, "Invalid 2-sided setting.")
        if "counter_payment" in changes and changes["counter_payment"] not in ("off", "confirm", "print_first"):
            raise HTTPException(400, "Invalid counter payment mode.")
        if "allowed_paper_sizes" in changes:
            ps = [p for p in changes["allowed_paper_sizes"] if p in PAPER_SIZES]
            if not ps:
                raise HTTPException(400, "Choose at least one paper size.")
            changes["allowed_paper_sizes"] = ps
        if "upi_id" in changes and changes["upi_id"] and "@" not in changes["upi_id"]:
            raise HTTPException(400, "UPI ID looks wrong. It should look like shopname@okaxis.")
        if "shop_id" in changes:
            sid = normalize_shop_id(changes["shop_id"])
            if len(sid) < 3:
                raise HTTPException(400, "Shop ID must be at least 3 letters/numbers.")
            changes["shop_id"] = sid
        for k in ("payment_timeout_minutes", "file_retention_minutes"):
            if k in changes:
                try:
                    changes[k] = max(5, min(int(changes[k]), 24 * 60))
                except (TypeError, ValueError):
                    raise HTTPException(400, f"{k} must be a whole number.")
        if "public_url" in changes and changes["public_url"] and \
                not changes["public_url"].startswith(("http://", "https://")):
            raise HTTPException(400, "Public URL must start with https://")
        S.update(**changes)
        if any(k in changes for k in ("shop_id", "registry_url", "shop_name")):
            ctx.registry_status = ""
            threading.Thread(target=sync_registry, args=(ctx,), daemon=True).start()
        return {"ok": True, "settings": s.public_view()}

    @app.post("/api/admin/shutdown")
    def shutdown(request: Request):
        """Close the app from the dashboard (no Task Manager needed, e.g. before an update)."""
        admin(request)
        logger.info("Closed from the dashboard")

        def _bye() -> None:
            time.sleep(0.8)  # let this response reach the browser
            try:
                orders.stop()
                ctx.tunnel.stop()
            finally:
                os._exit(0)

        threading.Thread(target=_bye, daemon=True).start()
        return {"ok": True}

    @app.post("/api/admin/razorpay-check")
    def razorpay_check(request: Request):
        admin(request)
        s = S.data
        if not s.razorpay_enabled:
            raise HTTPException(400, "Enter both Razorpay Key ID and Key Secret, then Save.")
        try:
            Razorpay(s.razorpay_key_id, s.razorpay_key_secret).check_keys()
        except PaymentError as exc:
            raise HTTPException(400, str(exc))
        mode = "TEST mode (no real money)" if s.razorpay_key_id.startswith("rzp_test") else "LIVE mode"
        return {"ok": True, "message": f"Razorpay keys work - {mode}."}

    @app.post("/api/admin/test-print")
    def test_print(request: Request):
        admin(request)
        target = orders.active_printer()
        if not target:
            raise HTTPException(400, "No printer selected.")
        tmp = orders.files_dir / f"testpage_{int(time.time())}.pdf"
        documents.make_test_page(tmp, S.data.shop_name, target)
        try:
            spec = printer.PrintSpec(color=True, duplex="one", copies=1, orientation="portrait",
                                     paper_size=orders.printer_caps()["papers"][0], page_indices=[0])
            out = None
            if printer.asks_for_filename(target):
                VIRTUAL_PRINT_DIR.mkdir(parents=True, exist_ok=True)
                out = str(VIRTUAL_PRINT_DIR / f"Test page {datetime.now():%Y-%m-%d %H%M%S}.pdf")
            printer.print_document(tmp, target, spec, "QR Print Shop - test page", output_file=out)
        except Exception as exc:
            raise HTTPException(500, f"Test print failed: {exc}")
        finally:
            tmp.unlink(missing_ok=True)
        if out:
            return {"ok": True, "message": f"Test page saved as a file in {VIRTUAL_PRINT_DIR}"}
        return {"ok": True, "message": f"Test page sent to {target}."}

    @app.post("/api/admin/logo")
    async def upload_logo(request: Request, file: UploadFile = File(...)):
        admin(request)
        data = await file.read()
        if not data or len(data) > 3 * 1024 * 1024:
            raise HTTPException(400, "Logo must be an image under 3 MB.")
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(data))
            img.thumbnail((512, 512))
            img.save(LOGO_PATH, "PNG")
        except Exception:
            raise HTTPException(400, "That file is not an image.")
        return {"ok": True}

    @app.delete("/api/admin/logo")
    def delete_logo(request: Request):
        admin(request)
        LOGO_PATH.unlink(missing_ok=True)
        return {"ok": True}

    @app.get("/api/admin/report.csv")
    def report(request: Request, day: str = ""):
        admin(request)
        d = day or today()
        rows = ctx.db.list_orders(limit=100000, day=d)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Token", "Time", "Customer", "Status", "Payment", "Paid", "Amount (Rs)", "Reference",
                    "Files", "B&W sides", "Colour sides", "Sheets"])
        for o in reversed(rows):
            bw = sum(i["selected_pages"] * i["copies"] for i in o["items"] if i["color_mode"] == "bw")
            col = sum(i["selected_pages"] * i["copies"] for i in o["items"] if i["color_mode"] == "color")
            w.writerow([o["token"], datetime.fromtimestamp(o["created_at"]).strftime("%H:%M"),
                        o["customer_name"], o["status"], o["pay_method"], "yes" if o["paid"] else "no",
                        f"{o['amount_paise'] / 100:.2f}", o["payment_ref"],
                        " | ".join(i["filename"] for i in o["items"]), bw, col,
                        sum(i["sheets"] for i in o["items"])])
        sm = ctx.db.day_summary(d)
        w.writerow([])
        w.writerow(["Collected (Rs)", f"{sm['collected_paise'] / 100:.2f}", "Online (Rs)",
                    f"{sm['online_paise'] / 100:.2f}", "Printed but unpaid (Rs)",
                    f"{sm['unpaid_printed_paise'] / 100:.2f}"])
        return Response("﻿" + buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="print-report-{d}.csv"'})

    def startup_link() -> Optional[Path]:
        appdata = os.environ.get("APPDATA")
        if sys.platform != "win32" or not appdata:
            return None
        return Path(appdata) / "Microsoft/Windows/Start Menu/Programs/Startup/QR Print Shop.lnk"

    def autostart_status() -> Dict[str, bool]:
        lnk = startup_link()
        return {"supported": lnk is not None, "enabled": bool(lnk and lnk.exists())}

    @app.post("/api/admin/autostart")
    def set_autostart(request: Request, enable: bool = True):
        admin(request)
        lnk = startup_link()
        if lnk is None:
            raise HTTPException(400, "Only supported on Windows.")
        if not enable:
            lnk.unlink(missing_ok=True)
            return autostart_status()
        if getattr(sys, "frozen", False):
            target, args = sys.executable, "--no-browser"
        else:
            target = str(INSTALL_DIR / ".venv" / "Scripts" / "pythonw.exe")
            args = f'"{INSTALL_DIR / "run.py"}" --no-browser'
        ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK);"
              "$s.TargetPath=$env:TGT;$s.Arguments=$env:ARGS;$s.WorkingDirectory=$env:WD;$s.WindowStyle=7;$s.Save()")
        env = dict(os.environ, LNK=str(lnk), TGT=target, ARGS=args, WD=str(INSTALL_DIR))
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                       check=True, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return autostart_status()

    # ── permanent QR registry (only on the central server) ──
    if ctx.registry is not None:
        reg = ctx.registry

        @app.post("/api/registry/heartbeat")
        def registry_heartbeat(body: Heartbeat):
            try:
                return {"ok": True, "route": reg.heartbeat(body.shop_id, body.target_url, body.secret,
                                                           body.shop_name)}
            except PermissionError as exc:
                raise HTTPException(403, str(exc))
            except ValueError as exc:
                raise HTTPException(400, str(exc))

        @app.get("/s/{shop_id}")
        def registry_redirect(shop_id: str):
            rec = reg.get(shop_id)
            if rec and rec.get("target_url") and time.time() - rec.get("updated_at", 0) < 180:
                return RedirectResponse(rec["target_url"], status_code=307, headers=NO_STORE)
            return HTMLResponse(OFFLINE_HTML, status_code=503, headers=NO_STORE)

    return app


OFFLINE_HTML = """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shop offline</title><meta http-equiv="refresh" content="20"></head>
<body style="font-family:system-ui,sans-serif;background:#f6f7fb;display:grid;place-items:center;min-height:100vh;margin:0;padding:24px;text-align:center">
<div style="max-width:380px;background:#fff;border-radius:18px;padding:32px;box-shadow:0 10px 30px rgba(0,0,0,.08)">
<div style="font-size:44px">&#128424;</div><h2 style="margin:8px 0">The print shop is offline</h2>
<p style="color:#555">The shop's computer is switched off or not connected. Please ask at the counter.
This page retries by itself.</p></div></body></html>"""


def dns_ready(url: str, timeout: float = 45) -> bool:
    """Wait until public DNS (Cloudflare + Google) knows the new tunnel name.

    If customers were sent there before the name exists, their mobile
    network's DNS could cache "not found" for minutes.
    """
    import json
    import urllib.request
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    resolvers = ["https://cloudflare-dns.com/dns-query?name={}&type=A", "https://dns.google/resolve?name={}&type=A"]
    end = time.time() + timeout
    while time.time() < end:
        ok = 0
        for r in resolvers:
            try:
                req = urllib.request.Request(r.format(host), headers={"accept": "application/dns-json"})
                from .net import urlopen
                with urlopen(req, timeout=6) as resp:
                    if json.loads(resp.read().decode()).get("Answer"):
                        ok += 1
            except Exception:
                pass
        if ok == len(resolvers):
            return True
        time.sleep(4)
    return False


_sync_lock = threading.Lock()


def sync_registry(ctx: Ctx) -> None:
    s = ctx.settings.data
    url = ctx.tunnel.public_url
    if not (s.registry_url and s.shop_id and url) or s.public_url:
        return
    with _sync_lock:
        if getattr(ctx, "dns_checked_url", None) != url:
            # Give public DNS a short head start so phones don't cache "not found".
            # This must never block the shop: if the DNS services can't be reached
            # from this network (blocked, no answer), send the address anyway.
            if ctx.registry_status != "ok":
                ctx.registry_status = ""
            if not dns_ready(url):
                logger.warning("Could not confirm %s in public DNS within 45 s; updating the QR anyway", url)
            ctx.dns_checked_url = url
        from .registry import send_heartbeat
        err = send_heartbeat(s.registry_url, s.shop_id, url, s.registry_secret, s.shop_name)
        ctx.registry_status = "ok" if not err else f"error: {err}"
        if err:
            logger.warning("Permanent QR update failed: %s", err)
