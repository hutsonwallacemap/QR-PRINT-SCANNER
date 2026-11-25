"""Automatic Cloudflare Quick Tunnel manager for public 4G/5G QR access."""

from __future__ import annotations

import atexit
import logging
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("qr_print.tunnel")

TUNNEL_URL_RE = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")


def find_cloudflared() -> Optional[str]:
    """Locate cloudflared executable on PATH or standard Windows install folders."""
    found = shutil.which("cloudflared")
    if found:
        return found

    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "cloudflared"
        / "cloudflared.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "cloudflared"
        / "cloudflared.exe",
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Microsoft"
        / "WinGet"
        / "Links"
        / "cloudflared.exe",
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    return None


class PublicTunnelManager:
    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self.public_url: Optional[str] = None
        self.status: str = "inactive"
        self._stop_registered = False

    def start(self, port: int, on_url: Callable[[str], None]) -> bool:
        exe = find_cloudflared()
        if not exe:
            self.status = "cloudflared_not_installed"
            logger.info("cloudflared not found; running in local Wi-Fi mode.")
            return False

        if self._proc and self._proc.poll() is None:
            return True

        self.status = "starting"
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._proc = subprocess.Popen(
                [exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
        except Exception as exc:
            self.status = f"error: {exc}"
            logger.warning("Failed to start cloudflared: %s", exc)
            return False

        if not self._stop_registered:
            atexit.register(self.stop)
            self._stop_registered = True

        threading.Thread(
            target=self._read_output, args=(on_url,), daemon=True
        ).start()
        return True

    def _read_output(self, on_url: Callable[[str], None]) -> None:
        if not self._proc or not self._proc.stdout:
            return
        for line in self._proc.stdout:
            match = TUNNEL_URL_RE.search(line)
            if match:
                url = match.group(0)
                if url != self.public_url:
                    self.public_url = url
                    self.status = "connected"
                    logger.info("Public 4G/5G QR URL ready: %s", url)
                    on_url(url)

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._proc = None
        self.status = "inactive"
