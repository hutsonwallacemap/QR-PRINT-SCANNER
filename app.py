"""Permanent-QR registry for Vercel (or any always-on host).

Vercel serves this file automatically (FastAPI `app` in app.py). Shop PCs send
their current Cloudflare tunnel address here every minute; the poster QR
points to  https://<this-host>/s/<shop-id>  which forwards customers there.

For reliable storage across Vercel instances, add an Upstash Redis database
(Vercel -> Storage -> Upstash) so UPSTASH_REDIS_REST_URL / _TOKEN are set.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from qrprint.registry import RegistryStore

STALE_AFTER = 180  # shop PC sends a heartbeat every 60 s

store = RegistryStore(Path("/tmp/qr_registry.json") if os.environ.get("VERCEL") else Path("qr_registry.json"))
app = FastAPI(title="QR Print Shop registry", docs_url=None, redoc_url=None)
NO_STORE = {"Cache-Control": "no-store"}


class Heartbeat(BaseModel):
    shop_id: str
    target_url: str
    secret: str = ""
    shop_name: str = ""


@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(
        "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>QR Print Shop</title><body style='font-family:system-ui;padding:40px;text-align:center'>"
        "<h2>QR Print Shop</h2><p>Scan the QR code at your print shop to print from your phone.</p></body>"
    )


@app.get("/healthz")
def health():
    return {"ok": True, "role": "registry", "storage": "upstash" if os.environ.get("UPSTASH_REDIS_REST_URL") else "local"}


@app.post("/api/registry/heartbeat")
def heartbeat(body: Heartbeat):
    try:
        return {"ok": True, "route": store.heartbeat(body.shop_id, body.target_url, body.secret, body.shop_name)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/s/{shop_id}")
def go(shop_id: str):
    rec = store.get(shop_id)
    if rec and rec.get("target_url") and time.time() - rec.get("updated_at", 0) < STALE_AFTER:
        return RedirectResponse(rec["target_url"], status_code=307, headers=NO_STORE)
    return HTMLResponse(OFFLINE, status_code=503, headers=NO_STORE)


OFFLINE = """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shop offline</title><meta http-equiv="refresh" content="15"></head>
<body style="font-family:system-ui,sans-serif;background:#f6f7fb;display:grid;place-items:center;min-height:100vh;margin:0;padding:24px;text-align:center">
<div style="max-width:380px;background:#fff;border-radius:18px;padding:32px;box-shadow:0 10px 30px rgba(0,0,0,.08)">
<div style="font-size:44px">&#128424;</div><h2 style="margin:8px 0">The print shop is offline</h2>
<p style="color:#555">The shop's computer is switched off or not connected to the internet. Please ask at the
counter. This page retries by itself.</p></div></body></html>"""
