"""Outgoing HTTPS for the shop PC (registry heartbeat, DNS check, Razorpay).

Some shop laptops have an old or incomplete Windows certificate store, and
Python then fails HTTPS with CERTIFICATE_VERIFY_FAILED. We trust both the
Windows store and the certifi bundle shipped inside the app.
"""

from __future__ import annotations

import ssl
import urllib.request
from functools import lru_cache


@lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()  # Windows certificate store
    try:
        import certifi

        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    return ctx


def urlopen(req, timeout: float = 10):
    return urllib.request.urlopen(req, timeout=timeout, context=ssl_context())
