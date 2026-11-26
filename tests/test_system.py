"""End-to-end tests. Run:  .venv\\Scripts\\python -m pytest -q"""

from __future__ import annotations

import hashlib
import hmac
import io
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

TMP = Path(tempfile.mkdtemp(prefix="qrprint_test_"))
os.environ["QRPRINT_DATA_DIR"] = str(TMP / "data")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf as fitz  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from qrprint import auth, documents, printer  # noqa: E402
from qrprint.config import SettingsStore  # noqa: E402
from qrprint.db import Database  # noqa: E402
from qrprint.orders import OrderService  # noqa: E402
from qrprint.registry import RegistryStore  # noqa: E402
from qrprint.server import Ctx, create_app  # noqa: E402

PIN = "482915"
PDF_PRINTER = "Microsoft Print to PDF"
HAS_PDF_PRINTER = printer.HAS_WIN32 and any(p["name"] == PDF_PRINTER for p in printer.list_printers())


def make_pdf(pages: int, landscape: bool = False) -> bytes:
    doc = fitz.open()
    for i in range(pages):
        w, h = (842, 595) if landscape else (595, 842)
        p = doc.new_page(width=w, height=h)
        p.insert_text((72, 100), f"Page {i + 1}", fontsize=40)
        p.draw_rect(fitz.Rect(72, 150, 300, 300), color=(1, 0, 0), fill=(1, 0, 0))
    return doc.tobytes()


class FakeRazorpay:
    secret = "test_secret_123"
    captured: dict = {}

    def __init__(self, *_):
        pass

    def create_order(self, amount, receipt, notes):
        oid = "order_" + hashlib.md5(receipt.encode()).hexdigest()[:10]
        return {"id": oid, "amount": amount}

    def verify_signature(self, oid, pid, sig):
        exp = hmac.new(self.secret.encode(), f"{oid}|{pid}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(exp, sig)

    def find_captured_payment(self, oid, amount):
        return self.captured.get(oid)


@pytest.fixture()
def env(tmp_path):
    settings = SettingsStore(tmp_path / "settings.json")
    settings.update(admin_pin_hash=auth.hash_pin(PIN), price_bw=2.0, price_color=10.0,
                    printer_name=PDF_PRINTER if HAS_PDF_PRINTER else "")
    db = Database(tmp_path / "t.db")
    files = tmp_path / "files"
    files.mkdir()
    orders = OrderService(db, settings, files_dir=files)
    orders.razorpay_factory = lambda s: FakeRazorpay()
    out = tmp_path / "printed"
    out.mkdir()
    orders.print_output_dir = out
    orders.start()
    ctx = Ctx(db, settings, orders, registry=RegistryStore(None))
    client = TestClient(create_app(ctx))
    yield {"client": client, "settings": settings, "db": db, "orders": orders, "out": out, "ctx": ctx}
    orders.stop()
    db.close()


def login(c):
    r = c.post("/api/admin/login", json={"pin": PIN})
    assert r.status_code == 200, r.text
    return {"X-Admin": "1"}


def upload(c, name="doc.pdf", data=None):
    r = c.post("/api/uploads", files={"file": (name, data or make_pdf(3), "application/pdf")})
    assert r.status_code == 200, r.text
    return r.json()


def wait_status(env, oid, statuses, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        o = env["db"].get_order(oid)
        if o["status"] in statuses:
            return o
        time.sleep(0.2)
    raise AssertionError(f"order stuck in {env['db'].get_order(oid)['status']}: {env['db'].get_order(oid)['message']}")


# ── unit ──────────────────────────────────────────────────────────────

def test_page_ranges():
    assert documents.parse_page_range("all", 5) == [0, 1, 2, 3, 4]
    assert documents.parse_page_range("1-2, 4", 5) == [0, 1, 3]
    assert documents.parse_page_range("4-", 5) == [3, 4]
    assert documents.parse_page_range("3-1", 5) == [0, 1, 2]
    assert documents.normalize_range("1,2,3,5", 6) == "1-3,5"
    assert documents.normalize_range("1-6", 6) == "all"
    for bad in ("abc", "9", "0", "7-9"):
        with pytest.raises(documents.DocumentError):
            documents.parse_page_range(bad, 5)


def test_pin_rules():
    assert auth.valid_new_pin("1234")
    assert auth.valid_new_pin("1111")
    assert auth.valid_new_pin("12a4")
    assert auth.valid_new_pin("482915") is None
    h = auth.hash_pin("482915")
    assert auth.check_pin("482915", h) and not auth.check_pin("482916", h)


# ── security ──────────────────────────────────────────────────────────

def test_admin_requires_login(env):
    c = env["client"]
    assert c.get("/api/admin/state").status_code == 401
    assert c.post("/api/admin/settings", json={"upi_id": "thief@ybl"}).status_code == 401
    assert c.post("/api/admin/login", json={"pin": "000000"}).status_code == 401
    h = login(c)
    # CSRF guard: logged-in cookie without the custom header is refused
    assert c.post("/api/admin/settings", json={"shop_name": "X"}).status_code == 403
    assert c.post("/api/admin/settings", json={"shop_name": "My Shop"}, headers=h).status_code == 200
    assert "razorpay_key_secret" not in c.get("/api/admin/state").json()["settings"]


def test_first_setup_only_on_shop_pc(env):
    env["settings"].update(admin_pin_hash="")
    r = env["client"].post("/api/admin/setup", json={"pin": "482915"})
    assert r.status_code == 403  # test client is not 127.0.0.1


def test_login_rate_limit(env):
    c = env["client"]
    codes = [c.post("/api/admin/login", json={"pin": "999999"}).status_code for _ in range(8)]
    assert codes[-1] == 429


def test_order_needs_access_key(env):
    c = env["client"]
    up = upload(c)
    o = c.post("/api/orders", json={"items": [{"upload_id": up["upload_id"]}], "pay_method": "counter"}).json()
    assert c.get(f"/api/orders/{o['id']}").status_code == 404
    assert c.get(f"/api/orders/{o['id']}?k=wrong").status_code == 404
    assert c.get(f"/api/orders/{o['id']}?k={o['access_key']}").status_code == 200


def test_rejects_bad_files(env):
    c = env["client"]
    r = c.post("/api/uploads", files={"file": ("virus.exe", b"MZ....", "application/octet-stream")})
    assert r.status_code == 400
    r = c.post("/api/uploads", files={"file": ("fake.pdf", b"not a pdf at all", "application/pdf")})
    assert r.status_code == 400 and "damaged" in r.json()["detail"]
    r = c.post("/api/uploads", files={"file": ("empty.pdf", b"", "application/pdf")})
    assert r.status_code == 400


# ── pricing ───────────────────────────────────────────────────────────

def test_quote_is_server_side(env):
    c = env["client"]
    up = upload(c, data=make_pdf(5))
    q = c.post("/api/quote", json={"items": [{"upload_id": up["upload_id"], "page_range": "1-3",
                                              "color_mode": "color", "copies": 2}]}).json()
    assert q["amount_paise"] == 3 * 2 * 1000  # 3 pages x 2 copies x Rs10
    q = c.post("/api/quote", json={"items": [{"upload_id": up["upload_id"], "color_mode": "bw"}]}).json()
    assert q["amount_paise"] == 5 * 200
    env["settings"].update(min_order_amount=20)
    q = c.post("/api/quote", json={"items": [{"upload_id": up["upload_id"], "page_range": "1"}]}).json()
    assert q["amount_paise"] == 2000 and q["subtotal_paise"] == 200
    bad = c.post("/api/quote", json={"items": [{"upload_id": up["upload_id"], "page_range": "9"}]})
    assert bad.status_code == 400


# ── payment flows ─────────────────────────────────────────────────────

def test_upi_claim_does_not_print_until_shopkeeper_confirms(env):
    c = env["client"]
    env["settings"].update(upi_id="shop@okaxis")
    up = upload(c)
    o = c.post("/api/orders", json={"items": [{"upload_id": up["upload_id"]}], "pay_method": "online"}).json()
    assert o["pay_method"] == "upi" and o["status"] == "awaiting_payment"
    assert o["upi"]["uri"].startswith("upi://pay?pa=shop%40okaxis") and "am=6.00" in o["upi"]["uri"]
    k = o["access_key"]
    r = c.post(f"/api/orders/{o['id']}/claim-paid?k={k}", json={"utr": "123456789012"}).json()
    assert r["status"] == "awaiting_payment" and r["customer_claimed_paid"]
    time.sleep(1)
    assert env["db"].get_order(o["id"])["status"] == "awaiting_payment"  # nothing printed for free
    h = login(c)
    assert c.post(f"/api/admin/orders/{o['id']}/confirm-payment", json={}, headers=h).status_code == 200
    done = wait_status(env, o["id"], ["printed", "failed"])
    assert done["status"] == "printed", done["message"]
    assert done["paid"] == 1 and done["payment_ref"] == "123456789012"


def test_razorpay_signature_auto_prints(env):
    c = env["client"]
    env["settings"].update(razorpay_key_id="rzp_test_x", razorpay_key_secret=FakeRazorpay.secret)
    up = upload(c)
    o = c.post("/api/orders", json={"items": [{"upload_id": up["upload_id"]}], "pay_method": "online"}).json()
    assert o["pay_method"] == "razorpay" and o["razorpay"]["amount"] == 600
    k, rid = o["access_key"], o["razorpay"]["order_id"]
    # forged signature is refused and nothing prints
    r = c.post(f"/api/orders/{o['id']}/razorpay?k={k}", json={
        "razorpay_order_id": rid, "razorpay_payment_id": "pay_fake", "razorpay_signature": "deadbeef"})
    assert r.status_code == 400
    assert env["db"].get_order(o["id"])["status"] == "awaiting_payment"
    # genuine signature -> prints automatically
    sig = hmac.new(FakeRazorpay.secret.encode(), f"{rid}|pay_real".encode(), hashlib.sha256).hexdigest()
    r = c.post(f"/api/orders/{o['id']}/razorpay?k={k}", json={
        "razorpay_order_id": rid, "razorpay_payment_id": "pay_real", "razorpay_signature": sig})
    assert r.status_code == 200
    done = wait_status(env, o["id"], ["printed", "failed"])
    assert done["status"] == "printed" and done["rzp_payment_id"] == "pay_real"


def test_razorpay_paid_but_page_closed_still_prints(env):
    c = env["client"]
    env["settings"].update(razorpay_key_id="rzp_test_x", razorpay_key_secret=FakeRazorpay.secret)
    up = upload(c)
    o = c.post("/api/orders", json={"items": [{"upload_id": up["upload_id"]}], "pay_method": "online"}).json()
    FakeRazorpay.captured[o["razorpay"]["order_id"]] = "pay_bg"
    env["orders"].maintenance()  # background reconciler
    done = wait_status(env, o["id"], ["printed", "failed"])
    assert done["status"] == "printed" and done["payment_ref"] == "pay_bg"


def test_unpaid_orders_expire(env):
    c = env["client"]
    up = upload(c)
    o = c.post("/api/orders", json={"items": [{"upload_id": up["upload_id"]}], "pay_method": "counter"}).json()
    env["db"].update_order(o["id"], created_at=time.time() - 3600)
    env["orders"].maintenance()
    assert env["db"].get_order(o["id"])["status"] == "expired"


def test_auto_print_off_needs_approval(env):
    c = env["client"]
    env["settings"].update(auto_print=False, counter_payment="print_first")
    up = upload(c)
    o = c.post("/api/orders", json={"items": [{"upload_id": up["upload_id"]}], "pay_method": "counter"}).json()
    assert o["status"] == "waiting_approval"
    h = login(c)
    c.post(f"/api/admin/orders/{o['id']}/print", headers=h)
    assert wait_status(env, o["id"], ["printed", "failed"])["status"] == "printed"


def test_closed_shop_refuses(env):
    env["settings"].update(accepting_orders=False)
    r = env["client"].post("/api/uploads", files={"file": ("a.pdf", make_pdf(1), "application/pdf")})
    assert r.status_code == 403


def test_tokens_increment(env):
    c = env["client"]
    toks = []
    for _ in range(3):
        up = upload(c)
        toks.append(c.post("/api/orders", json={"items": [{"upload_id": up["upload_id"]}],
                                                "pay_method": "counter"}).json()["token"])
    assert toks[1] == toks[0] + 1 and toks[2] == toks[1] + 1


def test_files_deleted_after_print(env):
    c = env["client"]
    env["settings"].update(counter_payment="print_first", file_retention_minutes=5)
    up = upload(c)
    o = c.post("/api/orders", json={"items": [{"upload_id": up["upload_id"]}], "pay_method": "counter"}).json()
    done = wait_status(env, o["id"], ["printed"])
    path = Path(env["db"].get_items(o["id"])[0]["stored_path"])
    assert path.exists()
    env["db"].update_order(o["id"], completed_at=time.time() - 3600)
    env["orders"].maintenance()
    assert not path.exists() and env["db"].get_order(o["id"])["files_deleted"] == 1


# ── real printing (Windows GDI -> Microsoft Print to PDF) ─────────────

@pytest.mark.skipif(not HAS_PDF_PRINTER, reason="needs Windows + Microsoft Print to PDF")
def test_real_gdi_print_pages_copies_and_images(env):
    c = env["client"]
    env["settings"].update(counter_payment="print_first")
    pdf = upload(c, "notes.pdf", make_pdf(5))
    img = io.BytesIO()
    from PIL import Image
    Image.new("RGB", (1600, 900), (30, 120, 200)).save(img, "JPEG")
    photo = upload(c, "photo.jpg", img.getvalue())
    txt = upload(c, "readme.txt", ("Hello printer\n" * 120).encode())
    o = c.post("/api/orders", json={"pay_method": "counter", "items": [
        {"upload_id": pdf["upload_id"], "page_range": "2-4", "copies": 2, "color_mode": "bw"},
        {"upload_id": photo["upload_id"], "color_mode": "color"},
        {"upload_id": txt["upload_id"]},
    ]}).json()
    done = wait_status(env, o["id"], ["printed", "failed"], timeout=120)
    assert done["status"] == "printed", [i["error"] for i in done["items"]]
    outs = sorted(env["out"].glob(f"{o['token']}_*.pdf"))
    assert len(outs) == 3
    with fitz.open(outs[0]) as d:
        assert d.page_count == 6  # pages 2-4 x 2 copies
    with fitz.open(outs[1]) as d:
        assert d.page_count == 1
        assert d[0].rect.width > d[0].rect.height  # landscape photo printed landscape
    with fitz.open(outs[2]) as d:
        assert d.page_count == txt["pages"]


# ── registry ──────────────────────────────────────────────────────────

def test_registry_cannot_be_hijacked(env):
    c = env["client"]
    ok = c.post("/api/registry/heartbeat", json={"shop_id": "sai-xerox", "target_url": "https://a.trycloudflare.com",
                                                 "secret": "s" * 32})
    assert ok.status_code == 200
    evil = c.post("/api/registry/heartbeat", json={"shop_id": "sai-xerox", "target_url": "https://evil.example",
                                                   "secret": "x" * 32})
    assert evil.status_code == 403
    r = c.get("/s/sai-xerox", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "https://a.trycloudflare.com"
    assert c.post("/api/registry/heartbeat", json={"shop_id": "abc", "target_url": "javascript:alert(1)",
                                                   "secret": "s" * 32}).status_code == 400


def test_vercel_registry_app():
    import app as registry_app  # root app.py deployed to Vercel
    registry_app.store = RegistryStore(None)
    c = TestClient(registry_app.app)
    assert c.get("/healthz").json()["role"] == "registry"
    assert c.get("/s/nobody-here").status_code == 503
    hb = {"shop_id": "ravi-print", "target_url": "https://x1.trycloudflare.com", "secret": "k" * 48}
    assert c.post("/api/registry/heartbeat", json=hb).status_code == 200
    r = c.get("/s/ravi-print", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "https://x1.trycloudflare.com"
    # PC restarted -> new tunnel address, same secret: allowed
    assert c.post("/api/registry/heartbeat", json={**hb, "target_url": "https://x2.trycloudflare.com"}).status_code == 200
    assert c.get("/s/ravi-print", follow_redirects=False).headers["location"] == "https://x2.trycloudflare.com"
    # someone else: refused
    assert c.post("/api/registry/heartbeat", json={**hb, "secret": "z" * 48}).status_code == 403
    # stale shop (PC off for >10 min) shows the offline page instead of a dead link
    registry_app.store._mem["ravi-print"]["updated_at"] -= 3600
    assert c.get("/s/ravi-print", follow_redirects=False).status_code == 503


def test_qr_uses_permanent_registry_only_when_tunnel_runs(env):
    ctx, s = env["ctx"], env["settings"]
    s.update(registry_url="https://reg.example", shop_id="sai-xerox", enable_tunnel=True)
    assert ctx.customer_url().startswith("http://")  # --no-tunnel: Wi-Fi address
    ctx.tunnel_allowed = True
    assert ctx.customer_url() == "https://reg.example/s/sai-xerox"


def test_tunnel_url_pattern_ignores_cloudflare_errors():
    from qrprint.tunnel import TUNNEL_URL_RE
    assert not TUNNEL_URL_RE.search('failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel"')
    m = TUNNEL_URL_RE.search("|  https://saying-webmasters-defining-automobiles.trycloudflare.com  |")
    assert m and m.group(0) == "https://saying-webmasters-defining-automobiles.trycloudflare.com"


def test_permanent_qr_updates_even_if_dns_check_is_blocked(env, monkeypatch):
    """Epson laptop bug: DNS-over-HTTPS blocked -> QR stayed 'Registering...' forever."""
    from qrprint import registry, server
    ctx, s = env["ctx"], env["settings"]
    s.update(registry_url="https://reg.example", shop_id="shop-03354c", enable_tunnel=True)
    ctx.tunnel_allowed = True
    ctx.tunnel.public_url = "https://a-b-c.trycloudflare.com"
    sent = []
    monkeypatch.setattr(server, "dns_ready", lambda url, timeout=45: False)  # DNS services unreachable
    monkeypatch.setattr(registry, "send_heartbeat", lambda *a, **k: sent.append(a) or "")
    server.sync_registry(ctx)
    assert sent and sent[0][2] == "https://a-b-c.trycloudflare.com"
    assert ctx.registry_status == "ok"
    assert ctx.mobile_status()["text"].startswith("Not working") is False


def test_https_helper_trusts_bundled_certificates():
    from qrprint.net import ssl_context
    assert ssl_context().cert_store_stats()["x509_ca"] > 100
