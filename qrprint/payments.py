"""Payments.

Razorpay  - real, verified online payment (UPI / cards / wallets). The server
            checks Razorpay's HMAC signature and/or asks Razorpay's API, so the
            print starts automatically only when money has actually arrived.
UPI link  - free, no gateway. A plain upi:// link cannot be verified by
            software, so the shopkeeper confirms the payment with one tap.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, Optional
from urllib.parse import quote

logger = logging.getLogger("qrprint.payments")

RAZORPAY_API = "https://api.razorpay.com/v1"


class PaymentError(RuntimeError):
    pass


class Razorpay:
    def __init__(self, key_id: str, key_secret: str, api_base: str = RAZORPAY_API):
        self.key_id = key_id.strip()
        self.key_secret = key_secret.strip()
        self.api_base = api_base

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> Dict[str, Any]:
        auth = base64.b64encode(f"{self.key_id}:{self.key_secret}".encode()).decode()
        req = urllib.request.Request(
            f"{self.api_base}{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode()).get("error", {}).get("description", "")
            except Exception:
                detail = ""
            if exc.code == 401:
                raise PaymentError("Razorpay keys are wrong. Check Key ID and Key Secret in Setup.")
            raise PaymentError(f"Razorpay error {exc.code}: {detail or exc.reason}")
        except urllib.error.URLError as exc:
            raise PaymentError(f"Cannot reach Razorpay (check internet): {exc.reason}")

    def create_order(self, amount_paise: int, receipt: str, notes: Dict[str, str]) -> Dict[str, Any]:
        return self._request("POST", "/orders", {
            "amount": int(amount_paise),
            "currency": "INR",
            "receipt": receipt[:40],
            "notes": notes,
        })

    def fetch_order(self, rzp_order_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/orders/{quote(rzp_order_id)}")

    def fetch_order_payments(self, rzp_order_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/orders/{quote(rzp_order_id)}/payments")

    def check_keys(self) -> None:
        self._request("GET", "/orders?count=1")

    def verify_signature(self, rzp_order_id: str, rzp_payment_id: str, signature: str) -> bool:
        expected = hmac.new(
            self.key_secret.encode(), f"{rzp_order_id}|{rzp_payment_id}".encode(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, (signature or "").strip())

    def find_captured_payment(self, rzp_order_id: str, amount_paise: int) -> Optional[str]:
        """Ask Razorpay directly whether this order was paid. Returns payment id or None."""
        payments = self.fetch_order_payments(rzp_order_id).get("items", [])
        for p in payments:
            if p.get("status") == "captured" and int(p.get("amount", 0)) >= amount_paise:
                return p.get("id")
        return None


def upi_uri(upi_id: str, payee: str, amount_paise: int, note: str) -> str:
    return (
        f"upi://pay?pa={quote(upi_id)}&pn={quote(payee or 'Print Shop')}"
        f"&am={amount_paise / 100:.2f}&cu=INR&tn={quote(note[:50])}"
    )
