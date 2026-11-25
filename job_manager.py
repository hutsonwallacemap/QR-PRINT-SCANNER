"""Background print queue and job state manager."""

from __future__ import annotations

import logging
import math
import os
import queue
import socket
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from printer_engine import (
    PrintOptions,
    count_file_pages,
    get_default_printer,
    parse_page_range,
    print_file_to_windows_printer,
)

logger = logging.getLogger("qr_print.jobs")

MAX_JOBS_HISTORY = 200
FILE_EXPIRY_SECONDS = 3600


@dataclass
class PrintJob:
    id: str
    token: int
    filename: str
    file_path: str
    file_size: int
    total_file_pages: int
    selected_pages: int
    sheets_count: int
    total_price: float
    color_mode: str
    sides: str
    page_range: str
    copies: int
    orientation: str
    paper_size: str
    printer_name: str
    customer_name: str
    status: str = "queued"
    message: str = "Waiting in queue..."
    pages_printed: int = 0
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    completed_at: Optional[str] = None
    _created_ts: float = field(default_factory=time.time, repr=False)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("file_path", None)
        d.pop("_created_ts", None)
        return d


class PrintJobManager:
    def __init__(self, upload_dir: Path):
        self.upload_dir = upload_dir
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._jobs: Dict[str, PrintJob] = {}
        self._order: List[str] = []
        self._queue: queue.Queue[str] = queue.Queue()
        self._new_job_event = threading.Event()
        self.auto_print = True
        self.active_printer: Optional[str] = get_default_printer()
        self.custom_base_url = ""
        self.shop_name = ""
        self.shop_id = self._default_shop_id()
        self.registry_url = os.environ.get("QR_REGISTRY_URL", "https://qr-print-scanner-alpha.vercel.app")
        self.registry_synced = False
        self.shop_phone = ""
        self.upi_id = ""
        self.has_logo = False
        self.price_bw: float = 2.0
        self.price_color: float = 10.0
        self._next_token: int = 101

    @staticmethod
    def _default_shop_id() -> str:
        host = socket.gethostname().lower()
        clean = "".join(ch if ch.isalnum() else "-" for ch in host).strip("-")
        return f"shop-{clean[:18]}" if clean else f"shop-{uuid.uuid4().hex[:6]}"
        self._stop = threading.Event()
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._cleanup, daemon=True).start()

    @property
    def has_new_job(self) -> bool:
        if self._new_job_event.is_set():
            self._new_job_event.clear()
            return True
        return False

    def create_job(self, filename: str, file_path: Path, color_mode: str = "color",
                   sides: str = "one", page_range: str = "all", copies: int = 1,
                   orientation: str = "auto", paper_size: str = "A4",
                   printer_name: Optional[str] = None, customer_name: str = "Walk-in Customer") -> PrintJob:
        total_pages = count_file_pages(file_path)
        clean_range = page_range.strip() or "all"
        selected_indices = parse_page_range(clean_range, total_pages)
        selected_count = len(selected_indices)
        clean_copies = max(1, min(99, int(copies)))
        clean_color = "bw" if color_mode == "bw" else "color"
        clean_sides = sides if sides in {"one", "both", "both-short"} else "one"
        rate = self.price_bw if clean_color == "bw" else self.price_color
        total_price = round(selected_count * clean_copies * rate, 2)
        per_sheet = 2 if clean_sides in {"both", "both-short"} else 1
        sheets_count = math.ceil(selected_count / per_sheet) * clean_copies

        target = printer_name or self.active_printer or get_default_printer()
        if not target:
            raise RuntimeError("No printer configured.")

        with self._lock:
            token_num = self._next_token
            self._next_token += 1

        job = PrintJob(
            id=uuid.uuid4().hex[:10], token=token_num, filename=filename, file_path=str(file_path),
            file_size=file_path.stat().st_size, total_file_pages=total_pages,
            selected_pages=selected_count, sheets_count=sheets_count, total_price=total_price,
            color_mode=clean_color,
            sides=clean_sides,
            page_range=clean_range,
            copies=clean_copies,
            orientation=orientation if orientation in {"auto", "portrait", "landscape"} else "auto",
            paper_size=paper_size if paper_size in {"A4", "Letter", "Legal", "A3", "A5"} else "A4",
            printer_name=target, customer_name=customer_name.strip() or "Walk-in Customer",
            status="queued" if self.auto_print else "waiting_approval",
            message=f"Token #{token_num} → printing on {target}" if self.auto_print else "Waiting for approval",
        )
        with self._lock:
            self._jobs[job.id] = job
            self._order.insert(0, job.id)
            self._new_job_event.set()
        if self.auto_print:
            self._queue.put(job.id)
        return job

    def list_jobs(self) -> List[dict]:
        with self._lock:
            return [self._jobs[jid].to_dict() for jid in self._order if jid in self._jobs]

    def get_job(self, job_id: str) -> Optional[dict]:
        with self._lock:
            j = self._jobs.get(job_id)
            return j.to_dict() if j else None

    def approve_or_reprint(self, job_id: str) -> PrintJob:
        with self._lock:
            j = self._jobs.get(job_id)
            if not j:
                raise KeyError("Job not found")
            if self.active_printer:
                j.printer_name = self.active_printer
            j.status = "queued"
            j.message = f"Queued for {j.printer_name}"
            j.completed_at = None
        self._queue.put(job_id)
        return j

    def delete_job(self, job_id: str) -> bool:
        with self._lock:
            j = self._jobs.pop(job_id, None)
            if not j:
                return False
            if job_id in self._order:
                self._order.remove(job_id)
        try:
            Path(j.file_path).unlink(missing_ok=True)
        except Exception:
            pass
        return True

    def _worker(self):
        while not self._stop.is_set():
            try:
                jid = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            with self._lock:
                j = self._jobs.get(jid)
                if not j:
                    self._queue.task_done()
                    continue
                j.status = "printing"
                j.message = f"Printing on {j.printer_name}..."
            try:
                opts = PrintOptions(color_mode=j.color_mode, page_range=j.page_range,
                                    copies=j.copies, orientation=j.orientation,
                                    sides=j.sides, paper_size=j.paper_size)
                count = print_file_to_windows_printer(Path(j.file_path), j.printer_name, opts,
                                                       f"QRPrint-{j.id}-{j.filename}")
                with self._lock:
                    j.status = "printed"
                    j.pages_printed = count
                    j.message = f"✅ Printed {count} page(s) on {j.printer_name}"
                    j.completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            except Exception as exc:
                logger.exception("Print job %s failed", jid)
                with self._lock:
                    j.status = "failed"
                    j.message = f"❌ Error: {exc}"
                    j.completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            finally:
                self._queue.task_done()

    def _cleanup(self):
        while not self._stop.is_set():
            try:
                now = time.time()
                with self._lock:
                    while len(self._order) > MAX_JOBS_HISTORY:
                        old_id = self._order.pop()
                        old = self._jobs.pop(old_id, None)
                        if old:
                            try:
                                Path(old.file_path).unlink(missing_ok=True)
                            except Exception:
                                pass
                    for jid in list(self._order):
                        j = self._jobs.get(jid)
                        if j and j.status in ("printed", "failed") and (now - j._created_ts) > FILE_EXPIRY_SECONDS:
                            try:
                                Path(j.file_path).unlink(missing_ok=True)
                            except Exception:
                                pass
            except Exception:
                logger.exception("Cleanup error")
            self._stop.wait(60)
