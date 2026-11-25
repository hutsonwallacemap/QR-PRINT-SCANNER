"""Lightweight shop-ID to live-tunnel-URL registry (Upstash Redis REST + local/tmp fallback)."""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("qr_print.registry")

SHOP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")

_MEM_STORE: Dict[str, dict] = {}
_LOCAL_FILE = Path("/tmp/qr_shop_registry.json") if os.environ.get("VERCEL") else None


def normalize_shop_id(raw: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", (raw or "").strip().lower()).strip("-")
    return slug[:40]


def _upstash_request(method: str, path: str, body: Optional[dict] = None) -> Optional[dict]:
    base = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
    if not base or not token:
        return None
    req = urllib.request.Request(
        f"{base}/{path}",
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        data=json.dumps(body).encode("utf-8") if body is not None else None,
    )
    with urllib.request.urlopen(req, timeout=4) as resp:
        return json.loads(resp.read().decode("utf-8"))


def save_shop_route(shop_id: str, target_url: str, shop_name: str = "") -> dict:
    clean_id = normalize_shop_id(shop_id)
    if not clean_id or len(clean_id) < 2:
        raise ValueError("Shop ID must be at least 2 characters (letters, numbers, hyphens).")
    record = {
        "shop_id": clean_id,
        "target_url": target_url.rstrip("/"),
        "shop_name": shop_name.strip(),
        "updated_at": time.time(),
    }
    _MEM_STORE[clean_id] = record

    # Optional Upstash Redis persistence (survives Vercel cold starts)
    try:
        if os.environ.get("UPSTASH_REDIS_REST_URL"):
            _upstash_request("POST", f"set/qrshop:{clean_id}", {"value": json.dumps(record)})
    except Exception as exc:
        logger.warning("Upstash save failed: %s", exc)

    if _LOCAL_FILE:
        try:
            data = json.loads(_LOCAL_FILE.read_text()) if _LOCAL_FILE.exists() else {}
            data[clean_id] = record
            _LOCAL_FILE.write_text(json.dumps(data))
        except Exception:
            pass
    return record


def get_shop_route(shop_id: str) -> Optional[dict]:
    clean_id = normalize_shop_id(shop_id)
    if not clean_id:
        return None

    # 1. Check Upstash Redis if configured
    try:
        if os.environ.get("UPSTASH_REDIS_REST_URL"):
            res = _upstash_request("GET", f"get/qrshop:{clean_id}")
            if res and res.get("result"):
                rec = json.loads(res["result"])
                _MEM_STORE[clean_id] = rec
                return rec
    except Exception as exc:
        logger.warning("Upstash read failed: %s", exc)

    # 2. Check in-memory store
    if clean_id in _MEM_STORE:
        return _MEM_STORE[clean_id]

    # 3. Check /tmp cache
    if _LOCAL_FILE and _LOCAL_FILE.exists():
        try:
            data = json.loads(_LOCAL_FILE.read_text())
            if clean_id in data:
                _MEM_STORE[clean_id] = data[clean_id]
                return data[clean_id]
        except Exception:
            pass
    return None


def send_heartbeat(registry_url: str, shop_id: str, target_url: str, shop_name: str = "") -> bool:
    """Push this shop PC's live tunnel URL to the central Vercel redirector."""
    if not registry_url or not shop_id or not target_url:
        return False
    url = f"{registry_url.rstrip('/')}/api/registry/heartbeat"
    payload = json.dumps({
        "shop_id": normalize_shop_id(shop_id),
        "target_url": target_url.rstrip("/"),
        "shop_name": shop_name,
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        method="POST",
        headers={"Content-Type": "application/json"},
        data=payload,
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception as exc:
        logger.debug("Registry heartbeat failed: %s", exc)
        return False
