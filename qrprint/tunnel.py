"""Automatic Cloudflare Quick Tunnel manager for public 4G/5G QR access."""

from __future__ import annotations

import atexit
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("qrprint.tunnel")

# Real quick-tunnel names are random words joined by hyphens. This must NOT
# match "https://api.trycloudflare.com", which appears in cloudflared errors.
TUNNEL_URL_RE = re.compile(r"https://[a-z0-9]+(?:-[a-z0-9]+)+\.trycloudflare\.com")


def find_cloudflared() -> Optional[str]:
    """Find cloudflared: bundled inside QRPrintShop.exe first, so shop owners install nothing."""
    import sys
    bundled = [Path(__file__).resolve().parent.parent / "bin" / "cloudflared.exe"]
    if getattr(sys, "frozen", False):
        bundled.insert(0, Path(getattr(sys, "_MEIPASS", "")) / "cloudflared.exe")
    for path in bundled:
        if path.exists():
            return str(path)
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
        # Backoff between restarts so we never hammer Cloudflare (it answers 429).
        self._failures = 0
        self._next_try = 0.0

    def start(self, port: int, on_url: Callable[[str], None]) -> bool:
        exe = find_cloudflared()
        if not exe:
            self.status = "cloudflared_not_installed"
            logger.info("cloudflared not found; running in local Wi-Fi mode.")
            return False

        if self._proc and self._proc.poll() is None:
            return True
        if time.time() < self._next_try:
            return False

        self.status = "starting"
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._proc = subprocess.Popen(
                [exe, "tunnel", "--no-autoupdate", "--protocol", "http2", "--url", f"http://127.0.0.1:{port}"],
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

        self._tie_to_this_process()

        if not self._stop_registered:
            atexit.register(self.stop)
            self._stop_registered = True

        threading.Thread(
            target=self._read_output, args=(on_url,), daemon=True
        ).start()
        return True

    def _tie_to_this_process(self) -> None:
        """Windows job object: if this app is killed (Task Manager, crash, shutdown),
        Windows also ends cloudflared instead of leaving it running on its own."""
        try:
            import win32api
            import win32con
            import win32job
            job = win32job.CreateJobObject(None, "")
            info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
            info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
            handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False,
                                          self._proc.pid)
            win32job.AssignProcessToJobObject(job, handle)
            self._job = job  # keep the handle open for as long as this app runs
        except Exception as exc:  # non-Windows or not permitted: atexit cleanup still applies
            logger.debug("Could not tie cloudflared to this process: %s", exc)

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
        # cloudflared exited: report it so the watchdog restarts it (with backoff)
        got_url = self.public_url is not None
        self._failures = 0 if got_url else self._failures + 1
        delay = min(300, 15 * (2 ** max(0, self._failures - 1)))
        self._next_try = time.time() + delay
        self.status = "disconnected" if got_url else f"retrying in {delay}s (Cloudflare busy)"
        self.public_url = None
        logger.warning("cloudflared stopped (%s).", self.status)

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
