"""Universal Windows GDI printing engine for images, PDFs, and text files."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageWin

try:
    import win32con
    import win32print
    import win32ui

    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False

logger = logging.getLogger("qr_print.engine")

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tiff", ".tif"}
PDF_EXTENSIONS = {".pdf"}
TEXT_EXTENSIONS = {".txt", ".log", ".csv", ".md"}


@dataclass
class PrintOptions:
    color_mode: str = "color"  # "color" or "bw"
    page_range: str = "all"  # "all" or e.g. "1-3, 5"
    copies: int = 1
    orientation: str = "auto"  # "auto", "portrait", "landscape"


def list_printers() -> List[dict]:
    """Return all installed Windows printers with default indicator."""
    if not HAS_WIN32:
        return [{"name": "Virtual Demo Printer", "is_default": True, "status": "Ready"}]

    try:
        default_name = win32print.GetDefaultPrinter()
    except Exception:
        default_name = ""

    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    raw_printers = win32print.EnumPrinters(flags, None, 2)
    printers = []
    for p in raw_printers:
        name = p.get("pPrinterName", "")
        if not name:
            continue
        status_code = p.get("Status", 0)
        printers.append(
            {
                "name": name,
                "is_default": name == default_name,
                "driver": p.get("pDriverName", ""),
                "port": p.get("pPortName", ""),
                "status": "Ready" if status_code == 0 else f"Status {status_code}",
            }
        )
    return printers


def get_default_printer() -> Optional[str]:
    if not HAS_WIN32:
        return "Virtual Demo Printer"
    try:
        return win32print.GetDefaultPrinter()
    except Exception:
        printers = list_printers()
        return printers[0]["name"] if printers else None


def parse_page_range(page_range: str, total_pages: int) -> List[int]:
    """Parse 1-based page range string like '1-3, 5' into sorted 0-based page indices."""
    if total_pages <= 0:
        return []

    cleaned = (page_range or "all").strip().lower()
    if not cleaned or cleaned == "all":
        return list(range(total_pages))

    selected = set()
    for part in cleaned.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            bounds = part.split("-", 1)
            try:
                start = int(bounds[0].strip()) if bounds[0].strip() else 1
                end = int(bounds[1].strip()) if bounds[1].strip() else total_pages
            except ValueError as exc:
                raise ValueError(f"Invalid page range segment: '{part}'") from exc
            if start > end:
                start, end = end, start
            for p in range(max(1, start), min(total_pages, end) + 1):
                selected.add(p - 1)
        else:
            try:
                p = int(part)
            except ValueError as exc:
                raise ValueError(f"Invalid page number: '{part}'") from exc
            if 1 <= p <= total_pages:
                selected.add(p - 1)

    if not selected:
        raise ValueError(
            f"No valid pages in range '{page_range}' (document has {total_pages} pages)."
        )
    return sorted(selected)


def count_file_pages(file_path: Path) -> int:
    """Return the number of printable pages in a file."""
    ext = file_path.suffix.lower()
    if ext in PDF_EXTENSIONS:
        with fitz.open(file_path) as doc:
            return len(doc)
    if ext in IMAGE_EXTENSIONS:
        with Image.open(file_path) as img:
            return getattr(img, "n_frames", 1)
    if ext in TEXT_EXTENSIONS:
        return max(1, len(_render_text_to_images(file_path)))
    raise ValueError(f"Unsupported file type: {ext}")


def _render_text_to_images(file_path: Path, dpi: int = 200) -> List[Image.Image]:
    """Render a plain text file onto A4-proportioned PIL Images."""
    width = int(8.27 * dpi)
    height = int(11.69 * dpi)
    margin = int(0.6 * dpi)
    text = file_path.read_text(encoding="utf-8", errors="replace")

    try:
        font = ImageFont.truetype("consola.ttf", size=int(dpi * 0.14))
    except Exception:
        font = ImageFont.load_default()

    line_height = int(dpi * 0.2)
    max_lines_per_page = max(1, (height - 2 * margin) // line_height)
    lines = text.splitlines() or [""]

    pages: List[Image.Image] = []
    for i in range(0, len(lines), max_lines_per_page):
        img = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(img)
        y = margin
        for line in lines[i : i + max_lines_per_page]:
            draw.text((margin, y), line[:120], fill="black", font=font)
            y += line_height
        pages.append(img)
    return pages


def render_pages(file_path: Path, options: PrintOptions, dpi: int = 250) -> List[Image.Image]:
    """Render selected pages of a file as RGB/Grayscale PIL Images."""
    ext = file_path.suffix.lower()
    images: List[Image.Image] = []

    if ext in PDF_EXTENSIONS:
        with fitz.open(file_path) as doc:
            indices = parse_page_range(options.page_range, len(doc))
            zoom = dpi / 72.0
            matrix = fitz.Matrix(zoom, zoom)
            colorspace = fitz.csGRAY if options.color_mode == "bw" else fitz.csRGB
            for idx in indices:
                page = doc.load_page(idx)
                pix = page.get_pixmap(matrix=matrix, colorspace=colorspace, alpha=False)
                mode = "L" if options.color_mode == "bw" else "RGB"
                img = Image.frombytes(mode, [pix.width, pix.height], pix.samples)
                images.append(img.convert("RGB"))

    elif ext in IMAGE_EXTENSIONS:
        with Image.open(file_path) as src:
            src = ImageOps.exif_transpose(src)
            total_frames = getattr(src, "n_frames", 1)
            indices = parse_page_range(options.page_range, total_frames)
            for idx in indices:
                if total_frames > 1:
                    src.seek(idx)
                frame = src.convert("RGB")
                if options.color_mode == "bw":
                    frame = ImageOps.grayscale(frame).convert("RGB")
                images.append(frame)

    elif ext in TEXT_EXTENSIONS:
        all_pages = _render_text_to_images(file_path, dpi=dpi)
        indices = parse_page_range(options.page_range, len(all_pages))
        images = [all_pages[idx] for idx in indices]

    else:
        raise ValueError(f"Unsupported file format: {ext}")

    return images


def _fit_rect(
    img_w: int, img_h: int, page_w: int, page_h: int
) -> Tuple[int, int, int, int]:
    """Compute centered aspect-preserving target rectangle inside printer printable area."""
    scale = min(page_w / img_w, page_h / img_h)
    target_w = max(1, int(img_w * scale))
    target_h = max(1, int(img_h * scale))
    x0 = (page_w - target_w) // 2
    y0 = (page_h - target_h) // 2
    return (x0, y0, x0 + target_w, y0 + target_h)


def print_file_to_windows_printer(
    file_path: Path,
    printer_name: str,
    options: PrintOptions,
    job_title: str = "QR Print Job",
) -> int:
    """
    Render and silently print a file to any Windows printer using GDI.
    Returns the total number of physical pages sent (pages * copies).
    """
    pages = render_pages(file_path, options)
    if not pages:
        raise ValueError("No pages rendered for printing.")

    copies = max(1, min(99, int(options.copies)))

    if not HAS_WIN32:
        logger.warning("pywin32 not available; simulated print of %d page(s).", len(pages) * copies)
        return len(pages) * copies

    # Skip interactive virtual printers that pop up a Save As dialog if user didn't explicitly pick them?
    # Note: Microsoft Print to PDF pops a Save File dialog when StartDoc has no OutputFile,
    # whereas physical printers (USB/Wi-Fi/Network) print immediately and silently.
    hprinter = win32print.OpenPrinter(printer_name)
    try:
        printer_info = win32print.GetPrinter(hprinter, 2)
        devmode = printer_info.get("pDevMode")
        if devmode is not None:
            devmode.Color = (
                win32con.DMCOLOR_MONOCHROME
                if options.color_mode == "bw"
                else win32con.DMCOLOR_COLOR
            )
            if options.orientation == "landscape":
                devmode.Orientation = win32con.DMORIENT_LANDSCAPE
            elif options.orientation == "portrait":
                devmode.Orientation = win32con.DMORIENT_PORTRAIT
    finally:
        win32print.ClosePrinter(hprinter)

    hdc = win32ui.CreateDC()
    hdc.CreatePrinterDC(printer_name)
    try:
        printable_w = hdc.GetDeviceCaps(win32con.HORZRES)
        printable_h = hdc.GetDeviceCaps(win32con.VERTRES)

        hdc.StartDoc(job_title)
        try:
            total_printed = 0
            for _copy in range(copies):
                for page_img in pages:
                    img = page_img
                    # Auto-rotate image to match page orientation if orientation == "auto"
                    if options.orientation == "auto":
                        page_is_landscape = printable_w > printable_h
                        img_is_landscape = img.width > img.height
                        if page_is_landscape != img_is_landscape and (
                            img.width > img.height * 1.1 or img.height > img.width * 1.1
                        ):
                            img = img.rotate(90, expand=True)
                    elif options.orientation == "landscape" and img.height > img.width:
                        img = img.rotate(90, expand=True)
                    elif options.orientation == "portrait" and img.width > img.height:
                        img = img.rotate(90, expand=True)

                    hdc.StartPage()
                    dib = ImageWin.Dib(img)
                    rect = _fit_rect(img.width, img.height, printable_w, printable_h)
                    dib.draw(hdc.GetHandleOutput(), rect)
                    hdc.EndPage()
                    total_printed += 1
            hdc.EndDoc()
            return total_printed
        except Exception:
            hdc.AbortDoc()
            raise
    finally:
        hdc.DeleteDC()
