"""Permanent QR codes.

A free Cloudflare quick-tunnel gets a NEW address every time the PC restarts,
which would make the printed poster useless. The fix: the poster points at a
fixed address  <registry>/s/<shop-id>  and the shop PC tells the registry its
current tunnel address every minute (heartbeat).

Security fix vs. the old version: the first heartbeat for a shop ID stores a
hash of that shop's secret; later updates must present the same secret, so
nobody else can redirect your customers to their own payment page.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger("qrprint.registry")

SHOP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")


def normalize_shop_id(raw: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", (raw or "").strip().lower()).strip("-")[:40]


def valid_target(url: str) -> bool:
    u = urlparse(url or "")
    return u.scheme == "https" and bool(u.hostname) and not u.username and not u.password


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class RegistryStore:
    """Server side (runs on the central host, e.g. Vercel / any always-on server)."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path
        self._mem: Dict[str, dict] = {}
        if path and path.exists():
            try:
                self._mem = json.loads(path.read_text())
            except Exception:
                self._mem = {}

    def _upstash(self, cmd: list) -> Optional[dict]:
        base = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
        token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
        if not base or not token:
            return None
        req = urllib.request.Request(base, method="POST", data=json.dumps(cmd).encode(),
                                     headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=4) as r:
            return json.loads(r.read().decode())

    def get(self, shop_id: str) -> Optional[dict]:
        sid = normalize_shop_id(shop_id)
        try:
            res = self._upstash(["GET", f"qrshop:{sid}"])
            if res and res.get("result"):
                self._mem[sid] = json.loads(res["result"])
        except Exception as exc:
            logger.warning("Upstash read failed: %s", exc)
        return self._mem.get(sid)

    def heartbeat(self, shop_id: str, target_url: str, secret: str, shop_name: str = "") -> dict:
        sid = normalize_shop_id(shop_id)
        if not SHOP_ID_RE.match(sid):
            raise ValueError("Invalid shop ID (3-40 letters, numbers, hyphens).")
        if not valid_target(target_url):
            raise ValueError("Target must be an https:// address.")
        if len(secret or "") < 16:
            raise ValueError("Missing shop secret.")
        existing = self.get(sid)
        if existing and existing.get("secret_hash") and \
                not hmac.compare_digest(existing["secret_hash"], _hash(secret)):
            raise PermissionError("This shop ID belongs to another shop. Choose a different shop ID.")
        rec = {"shop_id": sid, "target_url": target_url.rstrip("/"), "shop_name": shop_name[:80],
               "secret_hash": _hash(secret), "updated_at": time.time()}
        self._mem[sid] = rec
        try:
            self._upstash(["SET", f"qrshop:{sid}", json.dumps(rec)])
        except Exception as exc:
            logger.warning("Upstash save failed: %s", exc)
        if self.path:
            try:
                self.path.write_text(json.dumps(self._mem))
            except Exception:
                pass
        return {k: v for k, v in rec.items() if k != "secret_hash"}


def send_heartbeat(registry_url: str, shop_id: str, target_url: str, secret: str, shop_name: str = "") -> str:
    """Client side (shop PC). Returns '' on success or an error message."""
    if not (registry_url and shop_id and target_url):
        return "not configured"
    body = json.dumps({"shop_id": shop_id, "target_url": target_url, "secret": secret,
                       "shop_name": shop_name}).encode()
    req = urllib.request.Request(f"{registry_url.rstrip('/')}/api/registry/heartbeat", method="POST",
                                 data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return "" if r.status == 200 else f"HTTP {r.status}"
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        try:
            return json.loads(exc.read().decode()).get("detail", f"HTTP {exc.code}")
        except Exception:
            return f"HTTP {exc.code}"
    except Exception as exc:
        return str(exc)
