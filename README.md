# Universal QR Print System

A local Wi-Fi QR-code printing system for Windows that works with **all Windows-installed printers** (USB, Wi-Fi, Network, Inkjet, Laser, Virtual). Customers scan a QR code with their phone, upload images, PDFs, or text files, choose **Color / Black & White**, **Page Range**, **Copies**, and **Orientation**, and the computer automatically accepts and prints the job.

## Easiest & Most Trusted Way for Shopkeepers (Zero Coding / Zero Python Needed)
1. Download **`QRPrintSystem.exe`** from **GitHub Actions → Latest Workflow Run → Artifacts (`QRPrintSystem-Windows-EXE`)** (built automatically from this public source code so anyone can verify it).
2. Double-click **`QRPrintSystem.exe`** on the Windows PC connected to the printer.
3. Click **Allow** if Windows Firewall asks for Private Network access.
4. Your browser opens automatically to **`http://localhost:8500/admin`**:
   - Select your printer in **Active Windows Printer**.
   - Click **Print QR Poster** and place it on your counter.
   - Customers on the same Wi-Fi scan the QR code and print immediately!

## Features
- **Works with any Windows printer** via native Windows GDI (`pywin32` + `PyMuPDF` + `Pillow`) — no print dialogs or external PDF viewer dependencies.
- **Mobile-first Upload UI (`/`)** — scan QR code, upload multiple files, pick Color or B&W, page range (e.g. `1-3, 5`), copies, and track live print status.
- **Shopkeeper Admin Dashboard (`/admin`)** — live QR code, printer selector, Auto-Accept & Print toggle, optional public URL (ngrok / Cloudflare Tunnel), and live print queue with Reprint/Delete.
- **Printable QR Poster (`/qr-poster`)** — ready-to-print counter poster.

## Developer Setup (From Source)
```powershell
python -m venv .venv
.\.venv\Scripts\pip.exe install -r requirements.txt
```
Double-click `start_server.bat` (or run `build_exe.bat` to generate `dist\QRPrintSystem.exe` locally).
