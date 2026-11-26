"""Paths and persistent shop settings (survive restarts)."""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List

# ── Paths ─────────────────────────────────────────────────────────────

if getattr(sys, "frozen", False):  # PyInstaller one-file build
    APP_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    INSTALL_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).resolve().parent.parent
    INSTALL_DIR = APP_DIR

STATIC_DIR = APP_DIR / "static"


def _default_data_dir() -> Path:
    override = os.environ.get("QRPRINT_DATA_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "QRPrintShop"


DATA_DIR = _default_data_dir()
FILES_DIR = DATA_DIR / "files"
DB_PATH = DATA_DIR / "qrprint.db"
SETTINGS_PATH = DATA_DIR / "settings.json"
LOGO_PATH = DATA_DIR / "logo.png"
LOG_PATH = DATA_DIR / "qrprint.log"

# "Microsoft Print to PDF" / XPS writer would pop up a Save dialog; we save here instead.
VIRTUAL_PRINT_DIR = Path(os.environ.get("QRPRINT_VIRTUAL_DIR") or (Path.home() / "Documents" / "QR Print Shop"))

for _d in (DATA_DIR, FILES_DIR):
    _d.mkdir(parents=True, exist_ok=True)

PORT = int(os.environ.get("QRPRINT_PORT", "8500"))

# ── Limits ────────────────────────────────────────────────────────────

MAX_FILE_MB = 50
MAX_FILES_PER_ORDER = 15
MAX_PAGES_PER_FILE = 500
MAX_COPIES = 50
PAPER_SIZES = ["A4", "A3", "A5", "Letter", "Legal"]


@dataclass
class Settings:
    # Shop identity
    shop_name: str = "Print Shop"
    shop_phone: str = ""
    shop_address: str = ""

    # Printing
    printer_name: str = ""             # empty = Windows default printer
    auto_print: bool = True            # print as soon as payment is confirmed
    accepting_orders: bool = True
    allowed_paper_sizes: List[str] = field(default_factory=lambda: ["A4"])

    # Prices in rupees per printed side
    price_bw: float = 2.0
    price_color: float = 10.0
    price_bw_duplex: float = 0.0       # 0 = same as single-sided rate
    price_color_duplex: float = 0.0
    min_order_amount: float = 0.0

    # Payments
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    upi_id: str = ""
    upi_payee_name: str = ""
    # counter payment: "off" | "confirm" (shopkeeper releases) | "print_first"
    counter_payment: str = "confirm"
    payment_timeout_minutes: int = 30

    # Privacy
    file_retention_minutes: int = 60

    # Public access
    public_url: str = ""               # fixed URL (own domain / named tunnel). Empty = auto
    enable_tunnel: bool = True
    shop_id: str = ""
    registry_url: str = ""
    registry_secret: str = ""

    # Security
    admin_pin_hash: str = ""
    session_secret: str = ""

    # ── helpers ──
    @property
    def razorpay_enabled(self) -> bool:
        return bool(self.razorpay_key_id and self.razorpay_key_secret)

    @property
    def upi_enabled(self) -> bool:
        return bool(self.upi_id)

    def public_view(self) -> Dict[str, Any]:
        """Settings safe to send to the logged-in admin page (no secrets)."""
        d = asdict(self)
        for k in ("razorpay_key_secret", "admin_pin_hash", "session_secret", "registry_secret"):
            d.pop(k, None)
        d["razorpay_secret_set"] = bool(self.razorpay_key_secret)
        return d


SECRET_FIELDS = {"admin_pin_hash", "session_secret", "registry_secret"}
EDITABLE_FIELDS = {f.name for f in fields(Settings)} - SECRET_FIELDS


class SettingsStore:
    def __init__(self, path: Path = SETTINGS_PATH):
        self.path = path
        self._lock = threading.RLock()
        self.data = self._load()

    def _load(self) -> Settings:
        s = Settings()
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                for f in fields(Settings):
                    if f.name in raw:
                        setattr(s, f.name, raw[f.name])
            except Exception:
                # Keep a copy of the unreadable file instead of silently losing it.
                self.path.replace(self.path.with_suffix(".corrupt.json"))
        changed = False
        if not s.session_secret:
            s.session_secret = secrets.token_hex(32)
            changed = True
        if not s.registry_secret:
            s.registry_secret = secrets.token_hex(24)
            changed = True
        if not s.shop_id:
            s.shop_id = "shop-" + secrets.token_hex(3)
            changed = True
        self.data = s
        if changed or not self.path.exists():
            self.save()
        return s

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self.data), indent=2), encoding="utf-8")
            os.replace(tmp, self.path)

    def update(self, **changes: Any) -> Settings:
        with self._lock:
            for k, v in changes.items():
                setattr(self.data, k, v)
            self.save()
            return self.data
