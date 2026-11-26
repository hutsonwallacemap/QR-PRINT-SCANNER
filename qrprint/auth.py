"""Shopkeeper PIN login, sessions and simple rate limiting."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional

from fastapi import HTTPException, Request

SESSION_COOKIE = "qp_admin"
SESSION_TTL = 12 * 3600
PBKDF2_ROUNDS = 200_000


def hash_pin(pin: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), PBKDF2_ROUNDS).hex()
    return f"pbkdf2${PBKDF2_ROUNDS}${salt}${digest}"


def check_pin(pin: str, stored: str) -> bool:
    try:
        _, rounds, salt, digest = stored.split("$")
        test = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), int(rounds)).hex()
        return hmac.compare_digest(test, digest)
    except Exception:
        return False


def valid_new_pin(pin: str) -> Optional[str]:
    if not pin.isdigit() or not 4 <= len(pin) <= 12:
        return "PIN must be 4 to 12 digits."
    if len(set(pin)) == 1 or pin in "0123456789012" or pin in "9876543210":
        return "PIN is too easy to guess. Avoid 1111 or 1234."
    return None


def client_ip(request: Request) -> str:
    # Behind the Cloudflare tunnel every request arrives from 127.0.0.1;
    # Cloudflare adds the real address in this header.
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")


def is_local_request(request: Request) -> bool:
    """True only for a browser on the shop PC itself (not via tunnel/proxy)."""
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        return False
    for h in ("cf-connecting-ip", "x-forwarded-for", "forwarded", "cf-ray"):
        if h in request.headers:
            return False
    return True


class RateLimiter:
    def __init__(self, limit: int, window_s: float):
        self.limit, self.window = limit, window_s
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str) -> bool:
        """Record a hit; return False when over the limit."""
        now = time.time()
        with self._lock:
            q = self._hits[key]
            while q and q[0] < now - self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


class SessionManager:
    def __init__(self) -> None:
        self._sessions: Dict[str, float] = {}
        self._lock = threading.Lock()

    def create(self) -> str:
        sid = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[sid] = time.time() + SESSION_TTL
        return sid

    def valid(self, sid: Optional[str]) -> bool:
        if not sid:
            return False
        with self._lock:
            exp = self._sessions.get(sid)
            if not exp or exp < time.time():
                self._sessions.pop(sid, None)
                return False
            return True

    def revoke(self, sid: Optional[str]) -> None:
        with self._lock:
            self._sessions.pop(sid or "", None)

    def revoke_all(self) -> None:
        with self._lock:
            self._sessions.clear()


def require_admin(request: Request, sessions: SessionManager) -> None:
    if not sessions.valid(request.cookies.get(SESSION_COOKIE)):
        raise HTTPException(401, "Please log in.")
    # Custom header => browsers must preflight cross-site requests => blocks CSRF.
    if request.method not in ("GET", "HEAD") and request.headers.get("x-admin") != "1":
        raise HTTPException(403, "Missing admin header.")
