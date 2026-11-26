# QR Print Shop (v4)

Customers scan a QR code at the counter, upload their files from their phone, choose colour, sides and
copies, then pay. After payment the job prints automatically on the shop's Windows printer. There are no
pen drives, no WhatsApp files and no queue at the PC.

Works with **any printer installed in Windows** (USB, Wi-Fi, network, inkjet or laser).

---

## For shop owners: setup in 5 minutes

1. Copy this folder to the shop PC (the one connected to the printer).
2. Easiest: copy **`QRPrintShop.exe`** (or `QRPrintShop.zip` sent on WhatsApp) to `C:\QRPrintShop\` and double-click it.
   Nothing else to install: printing, payments and the 4G/5G link are all inside. (From source: `Install.bat`.)
3. The dashboard opens. **Create a PIN** (only possible on the shop PC itself).
4. **Printer & prices** tab: pick your printer, press **Print a test page**, then set your prices.
5. **Payments** tab: set up at least one of these:
   * **Razorpay (fully automatic).** The customer pays inside the page, the payment is verified, and
     the job prints by itself. Nobody can print without paying.
   * **UPI ID (free).** The customer pays your UPI ID. When the money arrives you tap **Paid ✓** and
     the job prints immediately.
   * **Cash at counter.** Either prints after you confirm, or prints first and you collect after.
6. **QR code & poster** tab: press **Print counter poster** and stick it on the counter.
7. Turn on **Start automatically when this PC turns on** (Printer & prices tab).

### Daily use
* Keep the dashboard open. New orders **beep** and show a red badge. Orders that need you (cash or UPI
  to confirm, or a print problem) have an orange or red edge.
* Each customer gets a **token number** (#101, #102… resetting daily) and watches live progress on their
  phone: *Payment → In queue → Printing → Ready*.
* **Reports** tab: daily totals and a CSV for Excel.
* Customer files are **deleted automatically** after printing (60 minutes by default).

### Razorpay setup (recommended for automatic printing)
1. Create a free account at razorpay.com and complete KYC.
2. Go to Dashboard → Account & Settings → API Keys → Generate Key.
3. Paste the **Key ID** and **Key Secret** into the Payments tab, then press **Check keys**.
4. Try it with `rzp_test_…` keys first. Test payments print but no money moves.

Why not plain UPI for automatic printing? A `upi://` payment link sends no confirmation back to the
shop, so software can't know whether the customer really paid. The old version printed when the
customer pressed "I have paid", which let anyone print for free. Razorpay (or the shopkeeper's tap)
is the only reliable confirmation.

### Customers on mobile data (4G/5G) - on by default
Customers can scan with **their own mobile internet**. No shop Wi-Fi is needed.

How it works:
1. Cloudflare's tunnel program is **built into QRPrintShop.exe** (nothing to install) and gives the shop PC a free internet address
   (`https://random-words.trycloudflare.com`). This address changes every time the PC restarts.
2. The poster QR points to a **permanent link**, `https://qr-print-scanner-alpha.vercel.app/s/<shop-id>`.
3. Every minute the shop PC tells that link its current address. Customers are forwarded there
   automatically, so **print the poster once and it keeps working**.

The dashboard shows **Mobile data (4G/5G): Working** in *QR code & poster*, and an alert bar if it isn't.

Good to know:
- Each shop ID is locked to its own shop's secret, so nobody can redirect your customers.
- If the PC is off, customers see a polite "shop is offline" page after about 3 minutes.
- The free Cloudflare quick tunnel has no uptime guarantee. If it fails, the app retries with
  growing pauses (15 s up to 5 min). For a business-grade link, use a **named Cloudflare tunnel on
  your own domain** and enter it under *Or your own fixed address*.

**Registry hosting (for the system owner, once):** the root `app.py` is the registry. Vercel serves it
automatically from this repo. In Vercel → Storage, add a free **Upstash Redis** database so links are
kept reliably across Vercel instances. Shops can use a different registry with the `QRPRINT_REGISTRY_URL`
environment variable or the *registry server* field in the dashboard.

### Files customers can print
PDF, JPG/PNG/WebP/GIF/BMP/TIFF photos, TXT, and Word (.doc/.docx/.rtf/.odt). Word needs Microsoft Word
or LibreOffice on the shop PC. Limits are 50 MB per file and 15 files per order. Password-protected PDFs
are rejected with a clear message.

---

## What changed from v3 (problems fixed)

| Problem in v3 | v4 |
|---|---|
| "I have paid" button started printing with no verification, so anyone could print free | Razorpay signature and API verification, or shopkeeper confirmation |
| Admin page and settings API had **no password**, even over the public internet link, so anyone could change the UPI ID | PIN login (hashed), sessions, brute-force lock, CSRF guard, first setup only on the shop PC |
| Colour, duplex, paper size and orientation were **never sent to the printer** (the DEVMODE was built but not used) | DEVMODE merged with the driver and passed to `CreateDC` |
| All settings, prices, jobs and tokens were **lost on every restart** | SQLite database and settings file in `%LOCALAPPDATA%\QRPrintShop` |
| Customer names and filenames were injected as HTML on the admin page (XSS) | All output escaped |
| Every customer's jobs were visible to anyone at `/api/jobs` | Each order needs its private access key |
| Big PDFs were rendered entirely into RAM (could crash) | One page at a time |
| Permanent-QR registry could be hijacked for any shop ID | Shop-secret lock and https-only targets |
| Word conversion failed in worker threads (COM not initialised) | Fixed, with a LibreOffice fallback |
| A crash mid-print could reprint silently on restart | Marked as interrupted, and the shopkeeper decides |

---

## For developers

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest          # 18 tests, incl. a real GDI print via "Microsoft Print to PDF"
.venv\Scripts\python dev.py             # dev server on :8600 with its own .devdata folder
build_exe.bat                           # -> dist\QRPrintShop.exe
```

Layout:

```
run.py               entry point (tunnel, heartbeat, uvicorn)
qrprint/config.py    paths + persistent settings
qrprint/db.py        SQLite orders/items/tokens
qrprint/orders.py    pricing, payment state machine, print worker, cleanup
qrprint/printer.py   Windows GDI printing with real DEVMODE
qrprint/documents.py file validation, Word/TXT->PDF, previews, page rendering
qrprint/payments.py  Razorpay REST + UPI links
qrprint/auth.py      PIN, sessions, rate limits
qrprint/registry.py  permanent-QR registry (server + client)
qrprint/server.py    HTTP API
static/              customer page, dashboard, poster
```

Data folder: `%LOCALAPPDATA%\QRPrintShop` (override with `QRPRINT_DATA_DIR`). Port: 8500 (`QRPRINT_PORT`).
