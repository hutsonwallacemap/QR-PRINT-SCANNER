"""Windows printing through GDI - works with any installed printer driver.

The previous version edited the printer's DEVMODE but then created the device
context without it, so colour / duplex / paper size were silently ignored.
Here the DEVMODE is merged with the driver (DocumentProperties) and passed to
CreateDC, so the driver really receives the customer's choices.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from PIL import Image, ImageWin

from . import documents

logger = logging.getLogger("qrprint.printer")

try:
    import win32con
    import win32gui
    import win32print
    import win32ui

    HAS_WIN32 = True
except ImportError:  # non-Windows dev machine
    HAS_WIN32 = False

DEMO_PRINTER = "Virtual Demo Printer"

DMPAPER = {"Letter": 1, "Legal": 5, "A3": 8, "A4": 9, "A5": 11}
DMDUP_SIMPLEX, DMDUP_VERTICAL, DMDUP_HORIZONTAL = 1, 2, 3
MAX_RENDER_DPI = 300

_print_lock = threading.Lock()


class PrinterError(RuntimeError):
    pass


@dataclass
class PrintSpec:
    color: bool
    duplex: str          # "one" | "long" | "short"
    copies: int
    orientation: str     # "auto" | "portrait" | "landscape"
    paper_size: str
    page_indices: List[int]


# ── Discovery ─────────────────────────────────────────────────────────

def default_printer() -> str:
    if not HAS_WIN32:
        return DEMO_PRINTER
    try:
        return win32print.GetDefaultPrinter()
    except Exception:
        names = [p["name"] for p in list_printers()]
        return names[0] if names else ""


_caps_cache: Dict[Tuple[str, str, str], Tuple[float, Dict[str, object]]] = {}
CAPS_SECONDS = 30 * 60


def _cached_capabilities(name: str, port: str, driver: str) -> Dict[str, object]:
    """Colour / 2-sided / paper support hardly ever changes, but asking the driver
    is slow (0.5 s+ per printer, more for some Epson/HP drivers). Remember it."""
    key = (name, port, driver)
    hit = _caps_cache.get(key)
    if hit and time.time() - hit[0] < CAPS_SECONDS:
        return dict(hit[1])
    caps = _capabilities(name, port)
    _caps_cache[key] = (time.time(), caps)
    return dict(caps)


def _capabilities(name: str, port: str) -> Dict[str, object]:
    caps: Dict[str, object] = {"color": True, "duplex": False, "papers": list(DMPAPER)}
    try:
        caps["color"] = bool(win32print.DeviceCapabilities(name, port, win32con.DC_COLORDEVICE))
    except Exception:
        pass
    try:
        caps["duplex"] = win32print.DeviceCapabilities(name, port, win32con.DC_DUPLEX) == 1
    except Exception:
        pass
    try:
        ids = set(win32print.DeviceCapabilities(name, port, win32con.DC_PAPERS) or [])
        if ids:
            caps["papers"] = [k for k, v in DMPAPER.items() if v in ids]
    except Exception:
        pass
    return caps


_STATUS_FLAGS = [
    (0x00000080, "Offline"), (0x00000008, "Paper jam"), (0x00000010, "Out of paper"),
    (0x00000040, "Paper problem"), (0x00000002, "Error"), (0x00040000, "Toner/ink low"),
    (0x00400000, "Door open"), (0x00000001, "Paused"), (0x00100000, "Needs attention"),
    (0x00000800, "Output bin full"), (0x00000400, "Printing"),
]


def _status_text(status: int, attributes: int) -> Tuple[str, bool]:
    if attributes & 0x400:  # PRINTER_ATTRIBUTE_WORK_OFFLINE
        return "Offline", False
    problems = [label for flag, label in _STATUS_FLAGS if status & flag]
    if not problems:
        return "Ready", True
    ok = all(p in ("Printing", "Toner/ink low") for p in problems)
    return ", ".join(problems), ok


_cache: Dict[str, object] = {"t": 0.0, "v": None}
_cache_lock = threading.Lock()
CACHE_SECONDS = 3.0  # status only; capabilities are cached separately


def list_printers(fresh: bool = False) -> List[Dict[str, object]]:
    """Installed printers with status + capabilities (cached briefly: enumeration is ~0.5 s)."""
    with _cache_lock:
        if not fresh and _cache["v"] is not None and time.time() - float(_cache["t"]) < CACHE_SECONDS:  # type: ignore[arg-type]
            return _cache["v"]  # type: ignore[return-value]
    result = _enumerate()
    with _cache_lock:
        _cache.update(t=time.time(), v=result)
    return result


def _enumerate() -> List[Dict[str, object]]:
    if not HAS_WIN32:
        return [{"name": DEMO_PRINTER, "is_default": True, "status": "Ready", "ok": True,
                 "jobs": 0, "virtual": True, "caps": {"color": True, "duplex": True, "papers": list(DMPAPER)}}]
    try:
        default = win32print.GetDefaultPrinter()
    except Exception:
        default = ""
    out = []
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    for p in win32print.EnumPrinters(flags, None, 2):
        name = p.get("pPrinterName") or ""
        if not name:
            continue
        port = p.get("pPortName") or ""
        status, ok = _status_text(p.get("Status", 0) or 0, p.get("Attributes", 0) or 0)
        virtual = port.upper() in ("PORTPROMPT:", "NUL:", "SHRFAX:") or "onenote" in name.lower() or "fax" in name.lower()
        out.append({
            "name": name,
            "is_default": name == default,
            "driver": p.get("pDriverName", ""),
            "port": port,
            "status": status,
            "ok": ok,
            "jobs": p.get("cJobs", 0),
            "virtual": virtual,
            "caps": _cached_capabilities(name, port, p.get("pDriverName", "")),
        })
    out.sort(key=lambda x: (x["virtual"], not x["is_default"], str(x["name"]).lower()))
    return out


def asks_for_filename(name: str) -> bool:
    """Virtual printers (Print to PDF, XPS) that would open a Save dialog."""
    info = printer_info(name)
    return bool(info and str(info.get("port", "")).upper() == "PORTPROMPT:")


def printer_info(name: str) -> Optional[Dict[str, object]]:
    for p in list_printers():
        if p["name"] == name:
            return p
    return None


# ── Printing ──────────────────────────────────────────────────────────

def _fit(img_w: int, img_h: int, box_w: int, box_h: int) -> Tuple[int, int, int, int]:
    scale = min(box_w / img_w, box_h / img_h)
    w, h = max(1, int(img_w * scale)), max(1, int(img_h * scale))
    x, y = (box_w - w) // 2, (box_h - h) // 2
    return x, y, x + w, y + h


def _decide_landscape(spec: PrintSpec, sizes: List[Tuple[float, float]]) -> bool:
    if spec.orientation == "landscape":
        return True
    if spec.orientation == "portrait":
        return False
    landscape = sum(1 for w, h in sizes if w > h * 1.05)
    return landscape > len(sizes) / 2


def _build_devmode(printer_name: str, spec: PrintSpec, landscape: bool):
    handle = win32print.OpenPrinter(printer_name)
    try:
        dm = win32print.GetPrinter(handle, 2)["pDevMode"]
        if dm is None:
            raise PrinterError(f"Printer '{printer_name}' did not return its settings (driver problem).")
        fields = 0
        dm.Orientation = win32con.DMORIENT_LANDSCAPE if landscape else win32con.DMORIENT_PORTRAIT
        fields |= win32con.DM_ORIENTATION
        if spec.paper_size in DMPAPER:
            dm.PaperSize = DMPAPER[spec.paper_size]
            fields |= win32con.DM_PAPERSIZE
        dm.Color = win32con.DMCOLOR_COLOR if spec.color else win32con.DMCOLOR_MONOCHROME
        fields |= win32con.DM_COLOR
        dm.Duplex = {"one": DMDUP_SIMPLEX, "long": DMDUP_VERTICAL, "short": DMDUP_HORIZONTAL}[spec.duplex]
        fields |= win32con.DM_DUPLEX
        dm.Copies = 1  # we send copies ourselves so they are always collated
        fields |= win32con.DM_COPIES
        dm.Fields = dm.Fields | fields
        # Let the driver validate/merge our changes into its private data.
        win32print.DocumentProperties(
            0, handle, printer_name, dm, dm, win32con.DM_IN_BUFFER | win32con.DM_OUT_BUFFER
        )
        return dm
    finally:
        win32print.ClosePrinter(handle)


def print_document(
    print_path: Path,
    printer_name: str,
    spec: PrintSpec,
    job_title: str,
    output_file: Optional[str] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> int:
    """Send one document to the printer. Returns physical page sides sent.

    output_file is only used for virtual printers like "Microsoft Print to PDF"
    (lets us test the full pipeline without a save dialog).
    """
    if not spec.page_indices:
        raise PrinterError("No pages selected.")
    copies = max(1, min(int(spec.copies), 99))
    total = len(spec.page_indices) * copies

    if not HAS_WIN32 or printer_name == DEMO_PRINTER:
        for i in range(total):
            if on_progress:
                on_progress(i + 1, total)
        return total

    sizes = documents.page_sizes(print_path, spec.page_indices)
    landscape = _decide_landscape(spec, sizes)

    with _print_lock:
        dm = _build_devmode(printer_name, spec, landscape)
        hdc_raw = win32gui.CreateDC("WINSPOOL", printer_name, dm)
        if not hdc_raw:
            raise PrinterError(f"Could not open printer '{printer_name}'.")
        dc = win32ui.CreateDCFromHandle(hdc_raw)
        try:
            page_w = dc.GetDeviceCaps(win32con.HORZRES)
            page_h = dc.GetDeviceCaps(win32con.VERTRES)
            dpi = min(dc.GetDeviceCaps(win32con.LOGPIXELSX) or 300, MAX_RENDER_DPI)
            if output_file:
                dc.StartDoc(job_title[:120], output_file)
            else:
                dc.StartDoc(job_title[:120])
            sent = 0
            try:
                for copy in range(copies):
                    pages_this_copy = 0
                    for img in documents.render_pages(print_path, spec.page_indices, dpi, not spec.color):
                        _draw_page(dc, img, page_w, page_h)
                        pages_this_copy += 1
                        sent += 1
                        if on_progress:
                            on_progress(sent, total)
                    # On 2-sided, make every copy start on a fresh sheet.
                    if spec.duplex != "one" and pages_this_copy % 2 == 1 and copy < copies - 1:
                        dc.StartPage()
                        dc.EndPage()
                dc.EndDoc()
            except Exception:
                try:
                    dc.AbortDoc()
                except Exception:
                    pass
                raise
            return sent
        finally:
            dc.DeleteDC()


def _draw_page(dc, img: Image.Image, page_w: int, page_h: int) -> None:
    # Rotate content that doesn't match the paper orientation, then scale to fit.
    if (img.width > img.height) != (page_w > page_h) and abs(img.width - img.height) > min(img.size) * 0.05:
        img = img.rotate(90, expand=True)
    dc.StartPage()
    try:
        ImageWin.Dib(img).draw(dc.GetHandleOutput(), _fit(img.width, img.height, page_w, page_h))
    finally:
        dc.EndPage()
