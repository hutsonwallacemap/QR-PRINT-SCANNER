"""Background print queue and job state manager."""

from __future__ import annotations

import logging
import queue
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
    print_file_to_windows_printer,
)

logger = logging.getLogger("qr_print.jobs")

MAX_JOBS_HISTORY = 100
FILE_EXPIRY_SECONDS = 3600  # auto-delete uploaded files after 1 hour


@dataclass
class PrintJob:
    id: str
    filename: str
    file_path: str
    file_size: int
    total_file_pages: int
    color_mode: str
    page_range: str
    copies: int
    orientation: str
    printer_name: str
    customer_name: str
    status: str = "queued"  # queued, waiting_approval, printing, printed, failed
    message: str = "Waiting in queue..."
    pages_printed: int = 0
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    completed_at: Optional[str] = None
    _created_ts: float = field(default_factory=time.time, repr=False)

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("file_path", None)
        data.pop("_created_ts", None)
        return data


class PrintJobManager:
    def __init__(self, upload_dir: Path) -> None:
        self.upload_dir = upload_dir
        self.upload_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._jobs: Dict[str, PrintJob] = {}
        self._job_order: List[str] = []
        self._queue: queue.Queue[str] = queue.Queue()
        self._new_job_event = threading.Event()

        self.auto_print: bool = True
        self.active_printer: Optional[str] = get_default_printer()
        self.custom_base_url: str = ""
        self.shop_name: str = ""

        self._stop_event = threading.Event()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()
        self._cleaner = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleaner.start()

    @property
    def has_new_job(self) -> bool:
        """Check and clear the new-job flag (used for admin notification)."""
        if self._new_job_event.is_set():
            self._new_job_event.clear()
            return True
        return False

    def create_job(
        self,
        filename: str,
        file_path: Path,
        color_mode: str = "color",
        page_range: str = "all",
        copies: int = 1,
        orientation: str = "auto",
        printer_name: Optional[str] = None,
        customer_name: str = "Walk-in Customer",
    ) -> PrintJob:
        total_pages = count_file_pages(file_path)
        target_printer = printer_name or self.active_printer or get_default_printer()
        if not target_printer:
            raise RuntimeError("No printer is configured or installed on this computer.")

        job_id = uuid.uuid4().hex[:10]
        initial_status = "queued" if self.auto_print else "waiting_approval"
        initial_msg = (
            f"Accepted → printing on {target_printer}"
            if self.auto_print
            else "Waiting for shopkeeper approval"
        )

        job = PrintJob(
            id=job_id,
            filename=filename,
            file_path=str(file_path),
            file_size=file_path.stat().st_size,
            total_file_pages=total_pages,
            color_mode="bw" if color_mode == "bw" else "color",
            page_range=page_range.strip() or "all",
            copies=max(1, min(99, int(copies))),
            orientation=orientation if orientation in {"auto", "portrait", "landscape"} else "auto",
            printer_name=target_printer,
            customer_name=customer_name.strip() or "Walk-in Customer",
            status=initial_status,
            message=initial_msg,
        )

        with self._lock:
            self._jobs[job_id] = job
            self._job_order.insert(0, job_id)
            self._new_job_event.set()

        if self.auto_print:
            self._queue.put(job_id)

        return job

    def list_jobs(self) -> List[dict]:
        with self._lock:
            return [self._jobs[jid].to_dict() for jid in self._job_order if jid in self._jobs]

    def get_job(self, job_id: str) -> Optional[dict]:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.to_dict() if job else None

    def approve_or_reprint(self, job_id: str) -> PrintJob:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError("Job not found")
            if self.active_printer:
                job.printer_name = self.active_printer
            job.status = "queued"
            job.message = f"Queued for {job.printer_name}"
            job.completed_at = None
        self._queue.put(job_id)
        return job

    def delete_job(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.pop(job_id, None)
            if not job:
                return False
            if job_id in self._job_order:
                self._job_order.remove(job_id)
        try:
            Path(job.file_path).unlink(missing_ok=True)
        except Exception:
            pass
        return True

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                job_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            with self._lock:
                job = self._jobs.get(job_id)
                if not job:
                    self._queue.task_done()
                    continue
                job.status = "printing"
                job.message = f"Printing on {job.printer_name}..."

            try:
                options = PrintOptions(
                    color_mode=job.color_mode,
                    page_range=job.page_range,
                    copies=job.copies,
                    orientation=job.orientation,
                )
                printed_count = print_file_to_windows_printer(
                    file_path=Path(job.file_path),
                    printer_name=job.printer_name,
                    options=options,
                    job_title=f"QRPrint-{job.id}-{job.filename}",
                )
                with self._lock:
                    job.status = "printed"
                    job.pages_printed = printed_count
                    job.message = f"✅ Printed {printed_count} page(s) on {job.printer_name}"
                    job.completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            except Exception as exc:
                logger.exception("Print job %s failed", job_id)
                with self._lock:
                    job.status = "failed"
                    job.message = f"❌ Print error: {exc}"
                    job.completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            finally:
                self._queue.task_done()

    def _cleanup_loop(self) -> None:
        """Periodically remove old jobs and stale uploaded files."""
        while not self._stop_event.is_set():
            try:
                now = time.time()
                with self._lock:
                    # Trim job history to MAX_JOBS_HISTORY
                    while len(self._job_order) > MAX_JOBS_HISTORY:
                        old_id = self._job_order.pop()
                        old_job = self._jobs.pop(old_id, None)
                        if old_job:
                            try:
                                Path(old_job.file_path).unlink(missing_ok=True)
                            except Exception:
                                pass

                    # Delete uploaded files for completed jobs older than 1 hour
                    for jid in list(self._job_order):
                        job = self._jobs.get(jid)
                        if not job:
                            continue
                        if job.status in ("printed", "failed") and (now - job._created_ts) > FILE_EXPIRY_SECONDS:
                            try:
                                Path(job.file_path).unlink(missing_ok=True)
                            except Exception:
                                pass
            except Exception:
                logger.exception("Cleanup error")
            self._stop_event.wait(60)
