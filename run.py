"""Start QR Print Shop:  python run.py   (or double-click QRPrintShop.exe)

Options:
  --no-browser    don't open the dashboard (used by Windows auto-start)
  --no-tunnel     local Wi-Fi only, no internet link
  --registry      also act as the permanent-QR registry server
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import socket
import sys
import threading
import time
import webbrowser

from qrprint.config import DATA_DIR, DB_PATH, LOG_PATH, PORT, SettingsStore
from qrprint.db import Database
from qrprint.orders import OrderService
from qrprint.registry import RegistryStore
from qrprint.server import Ctx, create_app, local_ip, sync_registry
from qrprint.tunnel import PublicTunnelManager


def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stdout:  # pythonw / windowed exe have no console
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main() -> None:
    args = set(sys.argv[1:])
    open_browser = "--no-browser" not in args
    admin_url = f"http://localhost:{PORT}/admin"

    if port_in_use(PORT):
        # Already running (e.g. auto-started) - just show the dashboard.
        if open_browser:
            webbrowser.open(admin_url)
        return

    setup_logging()
    log = logging.getLogger("qrprint")
    log.info("Data folder: %s", DATA_DIR)

    settings = SettingsStore()
    db = Database(DB_PATH)
    orders = OrderService(db, settings)
    orders.sweep_orphan_files()
    orders.start()
    tunnel = PublicTunnelManager()
    registry = RegistryStore(DATA_DIR / "registry.json") if "--registry" in args else None
    ctx = Ctx(db, settings, orders, tunnel=tunnel, registry=registry)
    app = create_app(ctx)

    if settings.data.enable_tunnel and "--no-tunnel" not in args and not settings.data.public_url:
        def on_url(url: str) -> None:
            threading.Thread(target=sync_registry, args=(ctx,), daemon=True).start()

        def heartbeat() -> None:
            while True:
                time.sleep(60)
                sync_registry(ctx)

        tunnel.start(PORT, on_url)
        threading.Thread(target=heartbeat, daemon=True).start()

    log.info("Customers on this Wi-Fi: http://%s:%d   Shop dashboard: %s", local_ip(), PORT, admin_url)
    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(admin_url)).start()

    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning", log_config=None,
                proxy_headers=False, timeout_keep_alive=30)


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(sys.argv[0])) or ".")
    main()
