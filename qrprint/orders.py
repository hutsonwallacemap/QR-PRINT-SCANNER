r"""Order lifecycle: upload -> price -> pay -> print -> clean up.

    awaiting_payment --(verified payment / shopkeeper confirms)--> queued
                     \--(auto print off)--> waiting_approval --(shopkeeper)--> queued
    queued -> printing -> printed | failed (shopkeeper can reprint)
    awaiting_payment --(timeout)--> expired      any open state --(shopkeeper)--> cancelled
"""

from __future__ import annotations

import logging
import math
import secrets
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import documents, printer
from .config import FILES_DIR, VIRTUAL_PRINT_DIR, MAX_COPIES, MAX_FILE_MB, MAX_FILES_PER_ORDER, PAPER_SIZES, SettingsStore
from .db import Database, today
from .payments import PaymentError, Razorpay

logger = logging.getLogger("qrprint.orders")

UPLOAD_TTL = 3 * 3600
OPEN_STATUSES = ("awaiting_payment", "waiting_approval", "waiting_flip", "queued", "printing", "failed")


class OrderError(ValueError):
    """Message is safe to show to the customer / shopkeeper."""


@dataclass
class StagedUpload:
    id: str
    filename: str
    folder: Path
    stored_path: Path
    print_path: Path
    pages: int
    kind: str
    created: float


def _rupees_to_paise(r: float) -> int:
    return int(round(float(r) * 100))


class OrderService:
    def __init__(self, db: Database, settings: SettingsStore, files_dir: Path = FILES_DIR):
        self.db = db
        self.settings = settings
        self.files_dir = files_dir
        self._uploads: Dict[str, StagedUpload] = {}
        self._uploads_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self.razorpay_factory = lambda s: Razorpay(s.razorpay_key_id, s.razorpay_key_secret)
        self.print_output_dir: Optional[Path] = None  # tests: capture virtual-printer output
        self.worker_state = "idle"

    # ── printer selection ─────────────────────────────────────────────
    def active_printer(self) -> str:
        return self.settings.data.printer_name or printer.default_printer()

    def printer_caps(self) -> Dict[str, Any]:
        info = printer.printer_info(self.active_printer())
        caps = dict(info["caps"]) if info else {"color": True, "duplex": False, "papers": ["A4"]}
        allowed = [p for p in self.settings.data.allowed_paper_sizes if p in PAPER_SIZES]
        caps["papers"] = [p for p in allowed if p in caps.get("papers", allowed)] or ["A4"]
        caps["auto_duplex"] = bool(caps.get("duplex"))
        manual = not caps["auto_duplex"] and self.settings.data.manual_duplex
        caps["duplex"] = caps["auto_duplex"] or manual
        caps["duplex_mode"] = "auto" if caps["auto_duplex"] else ("manual" if manual else "none")
        return caps

    # ── uploads ───────────────────────────────────────────────────────
    def stage_upload(self, filename: str, src_file, max_bytes: int = MAX_FILE_MB * 1024 * 1024) -> StagedUpload:
        name = Path(filename or "document").name.strip() or "document"
        name = "".join(ch for ch in name if ch not in '<>:"/\\|?*\x00')[:120] or "document"
        ext = Path(name).suffix.lower()
        if ext not in documents.ALL_EXT:
            raise OrderError("This file type can't be printed. Use PDF, photos (JPG/PNG), Word or TXT.")
        uid = secrets.token_hex(10)
        folder = self.files_dir / uid
        folder.mkdir(parents=True)
        stored = folder / f"original{ext}"
        size = 0
        try:
            with open(stored, "wb") as out:
                while True:
                    chunk = src_file.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise OrderError(f"File is larger than {max_bytes // (1024 * 1024)} MB.")
                    out.write(chunk)
            if size == 0:
                raise OrderError("This file is empty.")
            print_path = documents.prepare_for_print(stored)
            pages, kind = documents.inspect(print_path)
        except documents.DocumentError as exc:
            shutil.rmtree(folder, ignore_errors=True)
            raise OrderError(str(exc))
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        up = StagedUpload(uid, name, folder, stored, print_path, pages, kind, time.time())
        with self._uploads_lock:
            self._uploads[uid] = up
        return up

    def get_upload(self, uid: str) -> Optional[StagedUpload]:
        with self._uploads_lock:
            return self._uploads.get(uid)

    # ── pricing ───────────────────────────────────────────────────────
    def rate_paise(self, color: bool, duplex: bool) -> int:
        s = self.settings.data
        if color:
            r = s.price_color_duplex if (duplex and s.price_color_duplex > 0) else s.price_color
        else:
            r = s.price_bw_duplex if (duplex and s.price_bw_duplex > 0) else s.price_bw
        return _rupees_to_paise(r)

    def build_items(self, items_req: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not items_req:
            raise OrderError("Please add at least one file.")
        if len(items_req) > MAX_FILES_PER_ORDER:
            raise OrderError(f"Maximum {MAX_FILES_PER_ORDER} files per order.")
        caps = self.printer_caps()
        built = []
        for pos, req in enumerate(items_req):
            up = self.get_upload(str(req.get("upload_id", "")))
            if not up:
                raise OrderError("A file expired or was not uploaded. Please add it again.")
            label = f"{up.filename}: "
            color = req.get("color_mode") == "color"
            if color and not caps.get("color", True):
                raise OrderError("This shop's printer prints black & white only.")
            sides = req.get("sides", "one")
            if sides not in ("one", "long", "short"):
                sides = "one"
            if sides != "one" and not caps.get("duplex"):
                raise OrderError("This shop's printer can't print on both sides.")
            try:
                copies = int(req.get("copies", 1))
            except (TypeError, ValueError):
                copies = 1
            if not 1 <= copies <= MAX_COPIES:
                raise OrderError(f"Copies must be between 1 and {MAX_COPIES}.")
            paper = req.get("paper_size", caps["papers"][0])
            if paper not in caps["papers"]:
                raise OrderError(f"Paper size {paper} is not available here.")
            orientation = req.get("orientation", "auto")
            if orientation not in ("auto", "portrait", "landscape"):
                orientation = "auto"
            try:
                rng = documents.normalize_range(str(req.get("page_range", "all")), up.pages)
                selected = len(documents.parse_page_range(rng, up.pages))
            except documents.DocumentError as exc:
                raise OrderError(label + str(exc))
            sides_per_copy = selected
            sheets = (math.ceil(selected / 2) if sides != "one" else selected) * copies
            amount = sides_per_copy * copies * self.rate_paise(color, sides != "one")
            built.append({
                "upload": up, "position": pos, "filename": up.filename,
                "total_pages": up.pages, "page_range": rng, "selected_pages": selected,
                "color_mode": "color" if color else "bw", "sides": sides, "copies": copies,
                "orientation": orientation, "paper_size": paper, "sheets": sheets,
                "amount_paise": amount,
            })
        return built

    def quote(self, items_req: List[Dict[str, Any]]) -> Dict[str, Any]:
        items = self.build_items(items_req)
        subtotal = sum(i["amount_paise"] for i in items)
        total = max(subtotal, _rupees_to_paise(self.settings.data.min_order_amount)) if subtotal else 0
        return {
            "items": [{k: v for k, v in i.items() if k != "upload"} for i in items],
            "subtotal_paise": subtotal,
            "amount_paise": total,
        }

    # ── create ────────────────────────────────────────────────────────
    def payment_options(self) -> Dict[str, bool]:
        s = self.settings.data
        online = s.razorpay_enabled or s.upi_enabled
        counter = s.counter_payment in ("confirm", "print_first")
        if not online and not counter:
            counter = True  # never leave customers with no way to pay
        return {"online": online, "razorpay": s.razorpay_enabled, "upi": s.upi_enabled and not s.razorpay_enabled,
                "counter": counter}

    def create_order(self, items_req: List[Dict[str, Any]], customer_name: str, pay_method: str) -> Dict[str, Any]:
        s = self.settings.data
        if not s.accepting_orders:
            raise OrderError("The shop is not taking orders right now. Please ask at the counter.")
        built = self.build_items(items_req)
        subtotal = sum(i["amount_paise"] for i in built)
        amount = max(subtotal, _rupees_to_paise(s.min_order_amount)) if subtotal else 0
        opts = self.payment_options()
        if pay_method not in ("online", "counter"):
            raise OrderError("Please choose how you will pay.")
        if pay_method == "online" and not opts["online"]:
            raise OrderError("Online payment is not set up at this shop. Please choose Pay at Counter.")
        if pay_method == "counter" and not opts["counter"]:
            raise OrderError("Please pay online.")

        order_id = secrets.token_hex(8)
        token = self.db.next_token()
        now = time.time()
        method = "free" if amount == 0 else ("razorpay" if pay_method == "online" and s.razorpay_enabled
                                            else "upi" if pay_method == "online" else "counter")
        order = {
            "id": order_id, "access_key": secrets.token_urlsafe(18), "token": token, "day": today(),
            "customer_name": (customer_name or "").strip()[:60], "status": "awaiting_payment",
            "pay_method": method, "paid": 0, "amount_paise": amount, "printer_name": self.active_printer(),
            "message": "", "created_at": now,
        }
        if method == "razorpay":
            try:
                rz = self.razorpay_factory(s).create_order(
                    amount, receipt=f"T{token}-{order_id}", notes={"token": str(token), "order": order_id}
                )
            except PaymentError as exc:
                logger.warning("Razorpay order failed: %s", exc)
                raise OrderError("Online payment is not available right now. Please choose Pay at Counter.")
            order["rzp_order_id"] = rz["id"]
            order["message"] = "Waiting for online payment"
        elif method == "upi":
            order["message"] = "Pay by UPI, then show the payment to the shopkeeper"
        elif method == "counter":
            order["message"] = "Pay at the counter to release your print"

        items_rows = []
        for it in built:
            up: StagedUpload = it["upload"]
            items_rows.append({
                "id": secrets.token_hex(8), "order_id": order_id, "position": it["position"],
                "filename": it["filename"], "stored_path": str(up.stored_path), "print_path": str(up.print_path),
                "total_pages": it["total_pages"], "page_range": it["page_range"],
                "selected_pages": it["selected_pages"], "color_mode": it["color_mode"], "sides": it["sides"],
                "copies": it["copies"], "orientation": it["orientation"], "paper_size": it["paper_size"],
                "sheets": it["sheets"], "amount_paise": it["amount_paise"],
            })
        self.db.insert_order(order, items_rows)
        with self._uploads_lock:  # files now belong to the order
            for it in built:
                self._uploads.pop(it["upload"].id, None)

        if method == "free":
            self._payment_done(order_id, paid=True, ref="", from_statuses=("awaiting_payment",))
        elif method == "counter" and s.counter_payment == "print_first":
            self._payment_done(order_id, paid=False, ref="", from_statuses=("awaiting_payment",))
        return self.db.get_order(order_id)  # type: ignore[return-value]

    # ── payment events ────────────────────────────────────────────────
    def _payment_done(self, order_id: str, paid: bool, ref: str, from_statuses=("awaiting_payment",),
                      rzp_payment_id: Optional[str] = None) -> bool:
        s = self.settings.data
        now = time.time()
        fields: Dict[str, Any] = {"paid": 1 if paid else 0}
        if paid:
            fields["paid_at"] = now
        if ref:
            fields["payment_ref"] = ref[:60]
        if rzp_payment_id:
            fields["rzp_payment_id"] = rzp_payment_id
        if s.auto_print:
            fields.update(status="queued", message="In the print queue")
        else:
            fields.update(status="waiting_approval", message="Paid - the shopkeeper will start your print")
        if not paid:
            fields["message"] = "Printing now - please pay at the counter" if s.auto_print else \
                "Waiting for the shopkeeper - pay at the counter"
        ok = self.db.transition(order_id, from_statuses, **fields)
        if ok and fields["status"] == "queued":
            self._wake.set()
        return ok

    def verify_razorpay(self, order: Dict[str, Any], rzp_order_id: str, rzp_payment_id: str, signature: str) -> bool:
        if order["pay_method"] != "razorpay" or order["rzp_order_id"] != rzp_order_id:
            return False
        rz = self.razorpay_factory(self.settings.data)
        if not rz.verify_signature(rzp_order_id, rzp_payment_id, signature):
            logger.warning("Bad Razorpay signature for order %s", order["id"])
            return False
        self._payment_done(order["id"], paid=True, ref=rzp_payment_id, rzp_payment_id=rzp_payment_id)
        return True

    def reconcile_razorpay(self, order: Dict[str, Any], force: bool = False) -> bool:
        """Ask Razorpay if an awaiting order was paid (customer may have closed the page)."""
        if order["status"] != "awaiting_payment" or order["pay_method"] != "razorpay" or not order["rzp_order_id"]:
            return False
        now = time.time()
        if not force and order.get("last_checked_at") and now - order["last_checked_at"] < 8:
            return False
        self.db.update_order(order["id"], last_checked_at=now)
        try:
            pid = self.razorpay_factory(self.settings.data).find_captured_payment(
                order["rzp_order_id"], order["amount_paise"])
        except PaymentError as exc:
            logger.info("Razorpay check failed for %s: %s", order["id"], exc)
            return False
        if pid:
            return self._payment_done(order["id"], paid=True, ref=pid, rzp_payment_id=pid)
        return False

    def customer_claims_paid(self, order_id: str, utr: str) -> None:
        utr = "".join(ch for ch in (utr or "") if ch.isalnum())[:30]
        order = self.db.get_order(order_id)
        if not order or order["status"] != "awaiting_payment":
            return
        self.db.update_order(order_id, customer_claimed_paid=1, payment_ref=utr or order["payment_ref"],
                             message="Payment sent - waiting for the shopkeeper to confirm")

    # ── shopkeeper actions ────────────────────────────────────────────
    def admin_confirm_payment(self, order_id: str, ref: str = "") -> None:
        order = self.db.get_order(order_id)
        if not order:
            raise OrderError("Order not found.")
        if order["status"] == "awaiting_payment":
            self._payment_done(order_id, paid=True, ref=ref or order["payment_ref"])
        elif not order["paid"]:
            # print-first counter orders: record the cash afterwards
            self.db.update_order(order_id, paid=1, paid_at=time.time(), payment_ref=ref or order["payment_ref"])

    def admin_print(self, order_id: str) -> None:
        """Approve a held order, or reprint a printed/failed one."""
        order = self.db.get_order(order_id)
        if not order:
            raise OrderError("Order not found.")
        if order["files_deleted"]:
            raise OrderError("Files were already deleted for privacy. Ask the customer to upload again.")
        if order["status"] in ("queued", "printing"):
            raise OrderError("This order is already printing.")
        if order["status"] == "awaiting_payment":
            raise OrderError("Payment not received yet. Use 'Mark paid' first.")
        if order["status"] == "failed":
            for it in order["items"]:  # retry only what did not print
                if it["status"] != "printed":
                    self.db.update_item(it["id"], status="pending", pages_printed=0, error="")
        else:
            self.db.reset_items(order_id)
        self.db.update_order(order_id, status="queued", message="In the print queue",
                             printer_name=self.active_printer(), completed_at=None)
        self._wake.set()

    def admin_flip_done(self, order_id: str) -> None:
        """The shopkeeper put the printed stack back in the tray: print the back sides."""
        if not self.db.transition(order_id, ["waiting_flip"], status="queued", message="Printing the back sides"):
            raise OrderError("This order is not waiting for pages to be turned over.")
        self._wake.set()

    def admin_cancel(self, order_id: str) -> None:
        order = self.db.get_order(order_id)
        if not order:
            raise OrderError("Order not found.")
        if order["status"] == "printing":
            raise OrderError("Can't cancel while printing. Cancel it in the Windows print queue if needed.")
        self.db.update_order(order_id, status="cancelled", message="Cancelled by the shop", completed_at=time.time())

    # ── background work ───────────────────────────────────────────────
    def start(self) -> None:
        # A crash mid-print must not cause a silent duplicate print on restart.
        for o in self.db.orders_with_status(["printing"]):
            self.db.update_order(o["id"], status="failed",
                                 message="Interrupted (PC restarted). Check the paper, then press Reprint.")
        # Ask the printer drivers once now, so the first customer doesn't wait for it.
        threading.Thread(target=printer.list_printers, daemon=True, name="warm-printers").start()
        for target in (self._print_loop, self._maintenance_loop):
            t = threading.Thread(target=target, daemon=True, name=target.__name__)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _print_loop(self) -> None:
        while not self._stop.is_set():
            order = self.db.next_queued()
            if not order:
                self.worker_state = "idle"
                self._wake.wait(2)
                self._wake.clear()
                continue
            self.print_order(order)

    def print_order(self, order: Dict[str, Any]) -> None:
        oid = order["id"]
        if not self.db.transition(oid, ["queued"], status="printing", message="Printing..."):
            return
        self.worker_state = f"printing #{order['token']}"
        target = self.active_printer()
        self.db.update_order(oid, printer_name=target)
        failed = []
        for it in order["items"]:
            if it["status"] == "printed":
                continue
            self.db.update_item(it["id"], status="printing", error="")
            try:
                indices = documents.parse_page_range(it["page_range"], it["total_pages"])
                two_sided = it["sides"] != "one"
                manual = two_sided and not self.printer_caps()["auto_duplex"]
                side = None
                if manual:
                    side1, side2 = printer.manual_duplex_sequences(
                        indices, it["copies"], self.settings.data.manual_duplex_reverse)
                    side = 1 if it.get("phase", 0) == 0 else 2
                    indices, copies = (side1 if side == 1 else side2), 1
                else:
                    copies = it["copies"]
                spec = printer.PrintSpec(
                    color=it["color_mode"] == "color", duplex="one" if manual else it["sides"], copies=copies,
                    orientation=it["orientation"], paper_size=it["paper_size"], page_indices=indices,
                )
                out_file = None
                if self.print_output_dir is not None:
                    suffix = f"_side{side}" if side else ""
                    out_file = str(self.print_output_dir / f"{order['token']}_{it['position']}{suffix}.pdf")
                elif printer.asks_for_filename(target):
                    VIRTUAL_PRINT_DIR.mkdir(parents=True, exist_ok=True)
                    stem = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in Path(it["filename"]).stem)[:60]
                    ext = ".xps" if "xps" in target.lower() else ".pdf"
                    out_file = str(VIRTUAL_PRINT_DIR / f"Token{order['token']}_{it['position'] + 1}_{stem}{ext}")
                last = [0.0]

                def progress(done: int, total: int, _id=it["id"]) -> None:
                    if time.time() - last[0] > 0.7 or done == total:
                        last[0] = time.time()
                        self.db.update_item(_id, pages_printed=done)
                        self.db.update_order(oid, message=f"Printing {it['filename']} - page {done} of {total}")

                if not indices:  # e.g. 1-page file "2-sided": nothing for side 2
                    self.db.update_item(it["id"], status="printed", phase=0)
                    continue
                sent = printer.print_document(
                    Path(it["print_path"]), target, spec,
                    job_title=f"Token {order['token']} - {it['filename']}" + (f" (side {side})" if side else ""),
                    output_file=out_file, on_progress=progress,
                )
                if side == 1 and printer.manual_duplex_sequences(
                        documents.parse_page_range(it["page_range"], it["total_pages"]), it["copies"])[1]:
                    # Fronts done: the shopkeeper turns the stack over, then presses "Print backs".
                    self.db.update_item(it["id"], status="pending", phase=1, pages_printed=sent)
                    self.db.update_order(oid, status="waiting_flip",
                                         message="Front sides printed - the shop is turning the pages for the backs")
                    return
                self.db.update_item(it["id"], status="printed", phase=0)
            except Exception as exc:
                logger.exception("Printing failed for order %s item %s", oid, it["id"])
                msg = str(exc) or exc.__class__.__name__
                if "pywintypes" in repr(type(exc)) or isinstance(exc, OSError) or "StartDoc" in msg:
                    msg = f"Printer error ({msg}). Check the printer is switched on, connected and has paper, then press Retry."
                self.db.update_item(it["id"], status="failed", error=msg[:300])
                failed.append(msg)
        if failed:
            self.db.update_order(oid, status="failed", completed_at=time.time(),
                                 message="Print problem - the shopkeeper has been alerted")
        else:
            self.db.update_order(oid, status="printed", completed_at=time.time(),
                                 message="Printed - collect it at the counter")

    def _maintenance_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.maintenance()
            except Exception:
                logger.exception("Maintenance error")
            self._stop.wait(10)

    def maintenance(self) -> None:
        s = self.settings.data
        now = time.time()
        # 1) Razorpay: catch payments where the customer closed the page.
        for o in self.db.orders_with_status(["awaiting_payment"]):
            if o["pay_method"] == "razorpay" and s.razorpay_enabled:
                if o.get("last_checked_at") is None or now - o["last_checked_at"] >= 20:
                    self.reconcile_razorpay(o, force=True)
            # 2) Expire unpaid orders.
            fresh = self.db.get_order(o["id"])
            if fresh and fresh["status"] == "awaiting_payment" and \
                    now - fresh["created_at"] > s.payment_timeout_minutes * 60:
                # Give Razorpay one last check so a late payment is never lost.
                if fresh["pay_method"] == "razorpay" and s.razorpay_enabled and \
                        self.reconcile_razorpay(fresh, force=True):
                    continue
                self.db.transition(o["id"], ["awaiting_payment"], status="expired", completed_at=now,
                                   message="Payment not received in time - order expired")
        # 3) Delete customer files after printing (privacy).
        cutoff = now - s.file_retention_minutes * 60
        for o in self.db.orders_needing_file_cleanup(cutoff):
            for it in self.db.get_items(o["id"]):
                shutil.rmtree(Path(it["stored_path"]).parent, ignore_errors=True)
            self.db.update_order(o["id"], files_deleted=1)
        # 4) Forget uploads that never became orders.
        with self._uploads_lock:
            stale = [u for u in self._uploads.values() if now - u.created > UPLOAD_TTL]
            for u in stale:
                self._uploads.pop(u.id, None)
        for u in stale:
            shutil.rmtree(u.folder, ignore_errors=True)

    def sweep_orphan_files(self) -> None:
        """On startup: delete upload folders that belong to no order (left by a crash)."""
        known = set()
        for o in self.db.list_orders(limit=100000):
            for it in o.get("items", []):
                known.add(Path(it["stored_path"]).parent.name)
        for d in self.files_dir.iterdir():
            if d.is_dir() and d.name not in known:
                shutil.rmtree(d, ignore_errors=True)


def public_order(order: Dict[str, Any]) -> Dict[str, Any]:
    """What the customer is allowed to see about their own order."""
    return {
        "id": order["id"], "token": order["token"], "status": order["status"], "message": order["message"],
        "amount_paise": order["amount_paise"], "pay_method": order["pay_method"], "paid": bool(order["paid"]),
        "customer_claimed_paid": bool(order["customer_claimed_paid"]), "created_at": order["created_at"],
        "items": [{
            "filename": i["filename"], "selected_pages": i["selected_pages"], "page_range": i["page_range"],
            "color_mode": i["color_mode"], "sides": i["sides"], "copies": i["copies"],
            "paper_size": i["paper_size"], "amount_paise": i["amount_paise"], "status": i["status"],
            "pages_printed": i["pages_printed"],
        } for i in order.get("items", [])],
    }
