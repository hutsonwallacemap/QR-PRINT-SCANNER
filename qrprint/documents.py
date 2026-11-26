"""Reading customer files: validation, page counting, previews and page rendering."""

from __future__ import annotations

import functools
import io
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFont, ImageOps

logger = logging.getLogger("qrprint.documents")

Image.MAX_IMAGE_PIXELS = 120_000_000  # block decompression bombs, allow big phone photos

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff"}
PDF_EXT = {".pdf"}
WORD_EXT = {".doc", ".docx", ".rtf", ".odt"}
TEXT_EXT = {".txt"}
ALL_EXT = IMAGE_EXT | PDF_EXT | WORD_EXT | TEXT_EXT

# A4 at 72 dpi, used when we build PDFs ourselves (text files)
A4_PT = (595, 842)


class DocumentError(ValueError):
    """A problem with the customer's file, safe to show to the customer."""


# ── Page ranges ───────────────────────────────────────────────────────

def parse_page_range(spec: str, total: int) -> List[int]:
    """'1-3, 5, 8-' -> sorted 0-based indices. Raises DocumentError on bad input."""
    if total <= 0:
        raise DocumentError("This file has no pages.")
    s = (spec or "").strip().lower().replace(" ", "")
    if s in ("", "all"):
        return list(range(total))
    picked = set()
    for part in s.split(","):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                start = int(a) if a else 1
                end = int(b) if b else total
            except ValueError:
                raise DocumentError(f"'{part}' is not a valid page range. Example: 1-3, 5")
            if start > end:
                start, end = end, start
            if start > total:
                raise DocumentError(f"Page {start} does not exist (file has {total} pages).")
            picked.update(range(max(1, start) - 1, min(end, total)))
        else:
            try:
                p = int(part)
            except ValueError:
                raise DocumentError(f"'{part}' is not a page number. Example: 1-3, 5")
            if not 1 <= p <= total:
                raise DocumentError(f"Page {p} does not exist (file has {total} pages).")
            picked.add(p - 1)
    if not picked:
        raise DocumentError("Please choose at least one page.")
    return sorted(picked)


def normalize_range(spec: str, total: int) -> str:
    """Canonical text form, e.g. '1-3,5'; 'all' when every page is selected."""
    idx = parse_page_range(spec, total)
    if len(idx) == total:
        return "all"
    parts, start, prev = [], idx[0], idx[0]
    for i in idx[1:] + [None]:  # type: ignore[list-item]
        if i is not None and i == prev + 1:
            prev = i
            continue
        parts.append(f"{start + 1}" if start == prev else f"{start + 1}-{prev + 1}")
        if i is not None:
            start = prev = i
    return ",".join(parts)


# ── Conversion ────────────────────────────────────────────────────────

_convert_lock = threading.Lock()


def _find_soffice() -> Optional[str]:
    found = shutil.which("soffice")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            p = Path(base) / "LibreOffice" / "program" / "soffice.exe"
            if p.exists():
                return str(p)
    return None


def _word_com_to_pdf(src: Path, dst: Path) -> bool:
    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except ImportError:
        return False
    pythoncom.CoInitialize()
    word = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        doc = word.Documents.Open(
            str(src), ConfirmConversions=False, ReadOnly=True, AddToRecentFiles=False,
            PasswordDocument="__none__",  # password-protected files fail instead of prompting
        )
        try:
            doc.ExportAsFixedFormat(str(dst), 17)  # wdExportFormatPDF
        finally:
            doc.Close(False)
        return dst.exists() and dst.stat().st_size > 0
    except Exception as exc:
        logger.info("Word conversion failed for %s: %s", src.name, exc)
        return False
    finally:
        if word is not None:
            try:
                word.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()


def _soffice_to_pdf(src: Path, dst: Path) -> bool:
    exe = _find_soffice()
    if not exe:
        return False
    try:
        subprocess.run(
            [exe, "--headless", "--convert-to", "pdf", "--outdir", str(dst.parent), str(src)],
            check=True, timeout=90, capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        produced = src.with_suffix(".pdf")
        if produced.exists() and produced != dst:
            produced.replace(dst)
        return dst.exists() and dst.stat().st_size > 0
    except Exception as exc:
        logger.info("LibreOffice conversion failed for %s: %s", src.name, exc)
        return False


@functools.lru_cache(maxsize=1)
def office_converter_available() -> bool:
    if _find_soffice():
        return True
    try:
        import winreg  # type: ignore
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"Word.Application"))
        return True
    except Exception:
        return False


def text_to_pdf(src: Path, dst: Path) -> None:
    raw = src.read_bytes()
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    text = text.replace("\t", "    ")
    doc = fitz.open()
    margin, size, leading = 50, 10, 13
    width, height = A4_PT
    max_chars = int((width - 2 * margin) / (size * 0.6))
    lines: List[str] = []
    for line in text.splitlines() or [""]:
        while len(line) > max_chars:
            lines.append(line[:max_chars])
            line = line[max_chars:]
        lines.append(line)
    per_page = int((height - 2 * margin) / leading)
    for i in range(0, max(1, len(lines)), per_page):
        page = doc.new_page(width=width, height=height)
        y = margin + size
        for ln in lines[i:i + per_page]:
            page.insert_text((margin, y), ln, fontsize=size, fontname="cour")
            y += leading
    doc.save(str(dst))
    doc.close()


def prepare_for_print(src: Path) -> Path:
    """Return the file the printer engine should use (converting Word/text to PDF)."""
    ext = src.suffix.lower()
    if ext in PDF_EXT or ext in IMAGE_EXT:
        return src
    dst = src.with_name(src.stem + ".print.pdf")
    if dst.exists() and dst.stat().st_size > 0:
        return dst
    if ext in TEXT_EXT:
        text_to_pdf(src, dst)
        return dst
    if ext in WORD_EXT:
        with _convert_lock:  # Word/LibreOffice don't like parallel conversions
            if _word_com_to_pdf(src, dst) or _soffice_to_pdf(src, dst):
                return dst
        raise DocumentError(
            "This shop can't open Word files right now. Please save your document as PDF and upload again."
        )
    raise DocumentError(f"File type '{ext}' is not supported.")


# ── Inspection ────────────────────────────────────────────────────────

def inspect(print_path: Path) -> Tuple[int, str]:
    """Validate the file actually opens and return (page_count, kind)."""
    ext = print_path.suffix.lower()
    if ext in IMAGE_EXT:
        try:
            with Image.open(print_path) as img:
                img.verify()
            with Image.open(print_path) as img:
                frames = getattr(img, "n_frames", 1) if ext in {".tif", ".tiff"} else 1
            return frames, "image"
        except Exception:
            raise DocumentError("This image is damaged or not a real image file.")
    if ext in PDF_EXT:
        try:
            with fitz.open(print_path) as doc:
                if doc.needs_pass:
                    raise DocumentError("This PDF is password-protected. Please remove the password and try again.")
                n = doc.page_count
        except DocumentError:
            raise
        except Exception:
            raise DocumentError("This PDF is damaged and can't be opened.")
        if n <= 0:
            raise DocumentError("This PDF has no pages.")
        if n > 500:
            raise DocumentError("This PDF has more than 500 pages. Please split it or ask at the counter.")
        return n, "pdf"
    raise DocumentError(f"File type '{ext}' is not supported.")


def thumbnail_png(print_path: Path, page_index: int = 0, max_side: int = 360) -> bytes:
    ext = print_path.suffix.lower()
    if ext in PDF_EXT:
        with fitz.open(print_path) as doc:
            page = doc.load_page(min(page_index, doc.page_count - 1))
            zoom = max_side / max(page.rect.width, page.rect.height)
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            return pix.tobytes("png")
    with Image.open(print_path) as img:
        if getattr(img, "n_frames", 1) > 1:
            img.seek(min(page_index, img.n_frames - 1))
        im = ImageOps.exif_transpose(img).convert("RGB")
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()


# ── Rendering for print ───────────────────────────────────────────────

def page_sizes(print_path: Path, indices: List[int]) -> List[Tuple[float, float]]:
    """(width, height) of each selected page, any unit - used to pick orientation."""
    ext = print_path.suffix.lower()
    if ext in PDF_EXT:
        with fitz.open(print_path) as doc:
            return [(doc[i].rect.width, doc[i].rect.height) for i in indices]
    with Image.open(print_path) as img:
        out = []
        for i in indices:
            if getattr(img, "n_frames", 1) > 1:
                img.seek(i)
            w, h = img.size
            exif = img.getexif() if hasattr(img, "getexif") else {}
            if exif.get(0x0112) in (5, 6, 7, 8):  # rotated 90 degrees by EXIF
                w, h = h, w
            out.append((w, h))
        return out


def render_pages(print_path: Path, indices: List[int], dpi: int, grayscale: bool) -> Iterator[Image.Image]:
    """Yield one page image at a time, so a 300-page PDF never sits in RAM at once."""
    ext = print_path.suffix.lower()
    mode = "L" if grayscale else "RGB"
    if ext in PDF_EXT:
        with fitz.open(print_path) as doc:
            for i in indices:
                page = doc.load_page(i)
                zoom = dpi / 72.0
                # keep huge pages (posters) to a sane pixel count
                longest = max(page.rect.width, page.rect.height) * zoom
                if longest > 6000:
                    zoom *= 6000 / longest
                pix = page.get_pixmap(
                    matrix=fitz.Matrix(zoom, zoom),
                    colorspace=fitz.csGRAY if grayscale else fitz.csRGB,
                    alpha=False,
                )
                yield Image.frombytes(mode, (pix.width, pix.height), pix.samples)
        return
    with Image.open(print_path) as img:
        for i in indices:
            if getattr(img, "n_frames", 1) > 1:
                img.seek(i)
            frame = ImageOps.exif_transpose(img)
            if frame.mode in ("RGBA", "LA", "P"):
                frame = frame.convert("RGBA")
                bg = Image.new("RGBA", frame.size, "white")
                bg.alpha_composite(frame)
                frame = bg
            yield frame.convert(mode)


def make_test_page(dst: Path, shop_name: str, printer_name: str) -> Path:
    """A one-page PDF with colour bars and alignment marks."""
    doc = fitz.open()
    w, h = A4_PT
    page = doc.new_page(width=w, height=h)
    page.draw_rect(fitz.Rect(20, 20, w - 20, h - 20), color=(0, 0, 0), width=1.5)
    page.insert_text((50, 90), "QR Print Shop - Test Page", fontsize=24, fontname="helv")
    page.insert_text((50, 125), f"Shop: {shop_name}", fontsize=13, fontname="helv")
    page.insert_text((50, 145), f"Printer: {printer_name}", fontsize=13, fontname="helv")
    from datetime import datetime
    page.insert_text((50, 165), f"Printed: {datetime.now():%d %b %Y, %I:%M %p}", fontsize=13, fontname="helv")
    colors = [(0, 1, 1), (1, 0, 1), (1, 1, 0), (0, 0, 0), (1, 0, 0), (0, 0.6, 0), (0, 0, 1)]
    for i, c in enumerate(colors):
        x = 50 + i * 70
        page.draw_rect(fitz.Rect(x, 200, x + 60, 260), color=c, fill=c)
    for i in range(11):
        g = i / 10
        x = 50 + i * 45
        page.draw_rect(fitz.Rect(x, 280, x + 40, 320), color=(0, 0, 0), fill=(g, g, g), width=0.5)
    page.insert_text(
        (50, 370),
        "If you can read this and the boxes look right, your printer is ready for QR orders.",
        fontsize=11, fontname="helv",
    )
    for x, y in ((30, 30), (w - 30, 30), (30, h - 30), (w - 30, h - 30)):
        page.draw_line((x - 8, y), (x + 8, y))
        page.draw_line((x, y - 8), (x, y + 8))
    doc.save(str(dst))
    doc.close()
    return dst
