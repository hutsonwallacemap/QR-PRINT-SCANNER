# Universal QR Print System

A local Wi-Fi QR-code printing system for Windows that works with **all Windows-installed printers** (USB, Wi-Fi, Network, Inkjet, Laser, Virtual). Customers scan a QR code with their phone, upload images, PDFs, or text files, choose **Color / Black & White**, **Page Range**, **Copies**, and **Orientation**, and the computer automatically accepts and prints the job.

## Features
- **Works with any Windows printer** via native Windows GDI (`pywin32` + `PyMuPDF` + `Pillow`) — no print dialogs or external PDF viewer dependencies.
- **Mobile-first Upload UI (`/`)** — scan QR code, upload multiple files, pick Color or B&W, page range (e.g. `1-3, 5`), copies, and track live print status.
- **Shopkeeper Admin Dashboard (`/admin`)** — live QR code, printer selector, Auto-Accept & Print toggle, optional public URL (ngrok / Cloudflare Tunnel), and live print queue with Reprint/Delete.
- **Printable QR Poster (`/qr-poster`)** — ready-to-print counter poster.

## Quick Start (Windows)
```powershell
cd qr_print_system
python -m venv .venv
.\.venv\Scripts\pip.exe install -r requirements.txt
```
Then double-click `start_server.bat` (or run `.\.venv\Scripts\python.exe app.py`) and open:
- Admin Dashboard: `http://localhost:8500/admin`
- Customer Upload Page: `http://<your-lan-ip>:8500`
