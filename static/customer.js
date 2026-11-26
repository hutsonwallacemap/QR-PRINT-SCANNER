"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const rupees = (paise) => "₹" + (paise / 100).toLocaleString("en-IN", { minimumFractionDigits: paise % 100 ? 2 : 0, maximumFractionDigits: 2 });
const STORE_KEY = "qp_current_order";

let shop = null;
let files = [];            // {key, file, name, status:'uploading'|'ready'|'error', upload_id, pages, range, error, progress}
const opts = { color_mode: "bw", sides: "one", copies: 1, paper_size: "A4", orientation: "auto" };
let current = null;        // {id, k}
let order = null;
let pollTimer = null;
let razorpayLoaded = null;

function toast(msg, ms = 3200) {
  const t = $("toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), ms);
}
function showErr(id, msg) { const e = $(id); e.textContent = msg || ""; e.hidden = !msg; }

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json" } : {}, ...opts });
  let data = null;
  try { data = await res.json(); } catch { /* non-json */ }
  if (!res.ok) throw new Error((data && (typeof data.detail === "string" ? data.detail : "Please check your choices")) || `Error ${res.status}`);
  return data;
}

// ── steps ──
const ORDER = ["files", "options", "pay", "status"];
function go(step) {
  for (const s of ORDER) $("v-" + s).hidden = s !== step;
  const idx = ORDER.indexOf(step);
  document.querySelectorAll("#steps li").forEach((li, i) => {
    li.classList.toggle("active", i === idx);
    li.classList.toggle("done", i < idx);
  });
  window.scrollTo({ top: 0 });
  if (step === "options") updateOptionTotals();
  if (step === "pay") loadQuote();
}
document.querySelectorAll("[data-back]").forEach((b) => b.addEventListener("click", () => go(b.dataset.back)));

// ── shop info ──
async function loadShop() {
  shop = await api("/api/shop");
  document.title = `${shop.name} · Scan & Print`;
  $("shopName").textContent = shop.name;
  $("shopSub").textContent = shop.address || "Scan · Pay · Print";
  if (shop.has_logo) { $("logo").src = "/api/logo"; $("logo").hidden = false; }
  $("fileInput").accept = shop.extensions.join(",") + ",image/*,application/pdf";
  $("typesHint").textContent = `PDF, photos${shop.word_supported ? ", Word" : ""}, TXT · up to ${shop.limits.max_file_mb} MB each`;
  const p = shop.prices;
  $("pBw").textContent = `₹${p.bw}/page`;
  $("pColor").textContent = `₹${p.color}/page`;
  if (!shop.caps.color) {
    $("segColor").querySelector('[data-v="color"]').disabled = true;
    $("colorNote").hidden = false;
  }
  $("sidesField").hidden = !shop.caps.duplex;
  const paper = $("paper");
  paper.innerHTML = shop.caps.papers.map((x) => `<option>${esc(x)}</option>`).join("");
  opts.paper_size = shop.caps.papers[0];
  $("paperField").hidden = shop.caps.papers.length < 2;
  $("copies").max = shop.limits.max_copies;
  if (!shop.accepting_orders && !current) {
    $("closed").hidden = false; $("steps").hidden = true; $("v-files").hidden = true;
  }
}

// ── files ──
$("fileInput").addEventListener("change", (e) => { addFiles([...e.target.files]); e.target.value = ""; });
function addFiles(list) {
  showErr("filesErr", "");
  for (const f of list) {
    if (files.length >= shop.limits.max_files) { showErr("filesErr", `Maximum ${shop.limits.max_files} files per order.`); break; }
    const ext = "." + (f.name.split(".").pop() || "").toLowerCase();
    const item = { key: Math.random().toString(36).slice(2), file: f, name: f.name, status: "uploading", range: "", progress: 0 };
    if (!shop.extensions.includes(ext)) { item.status = "error"; item.error = "This file type can't be printed."; }
    else if (f.size > shop.limits.max_file_mb * 1048576) { item.status = "error"; item.error = `Larger than ${shop.limits.max_file_mb} MB.`; }
    files.push(item);
    if (item.status === "uploading") upload(item);
  }
  renderFiles();
}

function upload(item) {
  const fd = new FormData();
  fd.append("file", item.file, item.name);
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/uploads");
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) { item.progress = e.loaded / e.total; renderFiles(); } };
  xhr.onload = () => {
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch {}
    if (xhr.status === 200) {
      Object.assign(item, { status: "ready", upload_id: data.upload_id, pages: data.pages, name: data.filename });
    } else {
      item.status = "error";
      item.error = (data && typeof data.detail === "string") ? data.detail : "Upload failed. Try again.";
    }
    renderFiles();
  };
  xhr.onerror = () => { item.status = "error"; item.error = "Network problem. Check Wi-Fi and try again."; renderFiles(); };
  xhr.send(fd);
}

function fileIcon(name) {
  const e = name.split(".").pop().toLowerCase();
  return e === "pdf" ? "📕" : ["doc", "docx", "rtf", "odt"].includes(e) ? "📘" : e === "txt" ? "📝" : "🖼️";
}

function renderFiles() {
  const list = $("fileList");
  list.innerHTML = files.map((f) => {
    let meta = "", body = "";
    if (f.status === "uploading") {
      meta = f.progress < 1 ? `Uploading ${Math.round(f.progress * 100)}%` : "Checking file…";
      body = `<div class="bar"><div style="width:${Math.round(f.progress * 100)}%"></div></div>`;
    } else if (f.status === "error") {
      body = `<div class="ferr">${esc(f.error)}</div>`;
    } else {
      meta = `${f.pages} page${f.pages > 1 ? "s" : ""}`;
      if (f.pages > 1) {
        body = `<div class="frange"><label for="r_${f.key}">Pages</label>
          <input id="r_${f.key}" data-key="${f.key}" class="rangeIn" placeholder="All (e.g. 1-3, 5)" value="${esc(f.range)}" inputmode="text"></div>`;
      }
    }
    const thumb = f.status === "ready" ? `<img src="/api/uploads/${f.upload_id}/preview.png" alt="" loading="lazy">` : fileIcon(f.name);
    return `<div class="file"><div class="thumb">${thumb}</div>
      <div><div class="fname">${esc(f.name)}</div><div class="fmeta">${esc(meta)}</div>${body}</div>
      <button class="x" data-del="${f.key}" aria-label="Remove">✕</button></div>`;
  }).join("");
  list.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", () => {
    files = files.filter((f) => f.key !== b.dataset.del); renderFiles();
  }));
  list.querySelectorAll(".rangeIn").forEach((inp) => inp.addEventListener("input", () => {
    const f = files.find((x) => x.key === inp.dataset.key); if (f) f.range = inp.value;
  }));
  const ready = files.filter((f) => f.status === "ready");
  const busy = files.some((f) => f.status === "uploading");
  $("drop").classList.toggle("compact", files.length > 0);
  $("drop").querySelector(".drop-title").textContent = files.length ? "+ Add more files" : "Tap to choose files";
  const btn = $("toOptions");
  btn.disabled = !ready.length || busy;
  btn.textContent = busy ? "Uploading…" : ready.length ? `Continue with ${ready.length} file${ready.length > 1 ? "s" : ""}` : "Continue";
}

// client-side page-range count (server re-checks everything)
function countRange(spec, total) {
  const s = (spec || "").trim().toLowerCase().replace(/\s+/g, "");
  if (!s || s === "all") return total;
  const set = new Set();
  for (const part of s.split(",")) {
    if (!part) continue;
    const m = part.match(/^(\d*)-(\d*)$/);
    if (m) {
      let a = m[1] ? +m[1] : 1, b = m[2] ? +m[2] : total;
      if (a > b) [a, b] = [b, a];
      if (a > total) return null;
      for (let i = Math.max(1, a); i <= Math.min(b, total); i++) set.add(i);
    } else if (/^\d+$/.test(part)) {
      const p = +part; if (p < 1 || p > total) return null; set.add(p);
    } else return null;
  }
  return set.size || null;
}

$("toOptions").addEventListener("click", () => {
  for (const f of files.filter((x) => x.status === "ready")) {
    if (countRange(f.range, f.pages) === null) {
      showErr("filesErr", `${f.name}: page range "${f.range}" is not valid. Example: 1-3, 5`); return;
    }
  }
  showErr("filesErr", ""); go("options");
});

// ── options ──
function seg(id, key) {
  $(id).querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
    if (b.disabled) return;
    $(id).querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
    opts[key] = b.dataset.v; updateOptionTotals();
  }));
}
seg("segColor", "color_mode"); seg("segSides", "sides"); seg("segOrient", "orientation");
$("paper").addEventListener("change", (e) => (opts.paper_size = e.target.value));
function setCopies(n) {
  const max = shop ? shop.limits.max_copies : 50;
  opts.copies = Math.max(1, Math.min(max, parseInt(n, 10) || 1));
  $("copies").value = opts.copies; updateOptionTotals();
}
$("cMinus").addEventListener("click", () => setCopies(opts.copies - 1));
$("cPlus").addEventListener("click", () => setCopies(opts.copies + 1));
$("copies").addEventListener("change", (e) => setCopies(e.target.value));

function rate() {
  const p = shop.prices, color = opts.color_mode === "color", duplex = opts.sides !== "one";
  return color ? (duplex ? p.color_duplex : p.color) : (duplex ? p.bw_duplex : p.bw);
}
function updateOptionTotals() {
  if (!shop) return;
  const ready = files.filter((f) => f.status === "ready");
  const sides = ready.reduce((a, f) => a + (countRange(f.range, f.pages) || 0), 0) * opts.copies;
  let total = Math.round(sides * rate() * 100);
  if (total > 0 && shop.prices.min_order) total = Math.max(total, Math.round(shop.prices.min_order * 100));
  $("optPages").textContent = `${sides} page${sides !== 1 ? "s" : ""} × ₹${rate()}`;
  $("optTotal").textContent = rupees(total);
}

function itemsPayload() {
  return files.filter((f) => f.status === "ready").map((f) => ({
    upload_id: f.upload_id, page_range: f.range.trim() || "all", color_mode: opts.color_mode, sides: opts.sides,
    copies: opts.copies, paper_size: opts.paper_size, orientation: opts.orientation,
  }));
}

$("toPay").addEventListener("click", () => go("pay"));

// ── pay ──
async function loadQuote() {
  showErr("payErr", "");
  $("summary").innerHTML = `<div class="muted">Calculating…</div>`;
  $("payOnline").hidden = $("payCounter").hidden = true;
  try {
    const q = await api("/api/quote", { method: "POST", body: JSON.stringify({ items: itemsPayload() }) });
    $("summary").innerHTML = q.items.map((i) => `
      <div class="sumrow"><div><div>${esc(i.filename)}</div>
        <div class="muted">${i.selected_pages} page${i.selected_pages > 1 ? "s" : ""}${i.page_range !== "all" ? ` (${esc(i.page_range)})` : ""}
        · ${i.color_mode === "bw" ? "B&W" : "Colour"} · ${i.sides === "one" ? "1-sided" : "2-sided"} · ${i.copies} cop${i.copies > 1 ? "ies" : "y"} · ${esc(i.paper_size)}</div></div>
        <strong>${rupees(i.amount_paise)}</strong></div>`).join("");
    $("payTotal").textContent = rupees(q.amount_paise);
    $("minNote").hidden = q.amount_paise === q.subtotal_paise;
    $("minNote").textContent = `Minimum order amount is ${rupees(q.amount_paise)}.`;
    const pay = shop.payments;
    if (q.amount_paise === 0) {
      $("payCounter").hidden = false; $("payCounter").textContent = "Print now";
      $("payCounter").dataset.method = "counter";
      return;
    }
    $("payCounter").dataset.method = "counter";
    $("payOnline").hidden = !pay.online;
    $("payOnline").textContent = pay.razorpay ? `Pay ${rupees(q.amount_paise)} online · UPI / Card` : `Pay ${rupees(q.amount_paise)} by UPI`;
    $("payCounter").hidden = !pay.counter;
    $("payCounter").textContent = shop.counter_mode === "print_first" ? "Print now, pay at counter" : "Pay cash at counter";
    $("payCounter").className = pay.online ? "btn outline block" : "btn primary block";
  } catch (e) {
    $("summary").innerHTML = ""; showErr("payErr", e.message);
  }
}

async function placeOrder(method, btn) {
  showErr("payErr", "");
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = "Please wait…";
  try {
    const o = await api("/api/orders", { method: "POST", body: JSON.stringify({
      items: itemsPayload(), customer_name: $("custName").value.trim(), pay_method: method }) });
    current = { id: o.id, k: o.access_key };
    localStorage.setItem(STORE_KEY, JSON.stringify({ ...current, t: Date.now() }));
    files = []; renderFiles();
    order = o; go("status"); renderStatus();
    startPolling();
    if (o.razorpay) openRazorpay();
  } catch (e) {
    showErr("payErr", e.message);
  } finally { btn.disabled = false; btn.textContent = label; }
}
$("payOnline").addEventListener("click", (e) => placeOrder("online", e.currentTarget));
$("payCounter").addEventListener("click", (e) => placeOrder("counter", e.currentTarget));

// ── razorpay ──
function loadRazorpay() {
  if (window.Razorpay) return Promise.resolve();
  if (razorpayLoaded) return razorpayLoaded;
  razorpayLoaded = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "https://checkout.razorpay.com/v1/checkout.js";
    s.onload = resolve;
    s.onerror = () => { razorpayLoaded = null; reject(new Error("Could not load payment page. Check internet.")); };
    document.head.appendChild(s);
  });
  return razorpayLoaded;
}
async function openRazorpay() {
  if (!order || !order.razorpay) return;
  const btn = $("rzpOpen");
  btn.disabled = true;
  try { await loadRazorpay(); } catch (e) { toast(e.message); btn.disabled = false; return; }
  btn.disabled = false;
  const r = order.razorpay;
  const rz = new window.Razorpay({
    key: r.key_id, amount: r.amount, currency: "INR", order_id: r.order_id, name: r.name, description: r.description,
    theme: { color: "#2563eb" },
    handler: async (resp) => {
      $("statusLine").textContent = "Confirming payment…";
      try {
        order = await api(`/api/orders/${current.id}/razorpay?k=${encodeURIComponent(current.k)}`, {
          method: "POST", body: JSON.stringify(resp) });
        renderStatus();
      } catch (e) { toast(e.message, 6000); poll(); }
    },
    modal: { ondismiss: () => poll() },
  });
  rz.on("payment.failed", (resp) => toast("Payment failed: " + ((resp.error && resp.error.description) || "try again"), 6000));
  rz.open();
}
$("rzpOpen").addEventListener("click", openRazorpay);

// ── UPI ──
$("claimPaid").addEventListener("click", async () => {
  const btn = $("claimPaid"); btn.disabled = true;
  try {
    order = await api(`/api/orders/${current.id}/claim-paid?k=${encodeURIComponent(current.k)}`, {
      method: "POST", body: JSON.stringify({ utr: $("utr").value }) });
    renderStatus();
  } catch (e) { toast(e.message); } finally { btn.disabled = false; }
});

// ── status ──
const FINAL = ["printed", "cancelled", "expired"];
function renderStatus() {
  if (!order) return;
  const o = order;
  $("token").textContent = "#" + o.token;
  $("statusLine").textContent = o.message || "";
  const st = o.status;
  const paidish = o.paid || ["queued", "printing", "printed", "waiting_approval"].includes(st);
  const marks = {
    paid: st === "awaiting_payment" ? "now" : paidish ? "done" : "",
    queued: st === "queued" || st === "waiting_approval" ? "now" : ["printing", "printed"].includes(st) ? "done" : "",
    printing: st === "printing" ? "now" : st === "printed" ? "done" : st === "failed" ? "fail" : "",
    printed: st === "printed" ? "done" : "",
  };
  $("timeline").querySelectorAll("li").forEach((li) => { li.className = marks[li.dataset.t] || ""; });
  $("timeline").querySelector('[data-t="paid"]').textContent = o.pay_method === "counter" && !o.paid ? "Counter" : "Payment";

  const total = o.items.reduce((a, i) => a + i.selected_pages * i.copies, 0);
  const done = o.items.reduce((a, i) => a + (i.status === "printed" ? i.selected_pages * i.copies : i.pages_printed), 0);
  $("progressWrap").hidden = !(st === "printing" || st === "printed");
  $("progressBar").style.width = (total ? Math.min(100, (done / total) * 100) : 0) + "%";

  const awaiting = st === "awaiting_payment";
  $("payBox").hidden = !awaiting;
  $("rzpBox").hidden = !(awaiting && o.razorpay);
  $("upiBox").hidden = !(awaiting && o.upi);
  $("counterBox").hidden = !(awaiting && o.pay_method === "counter");
  $("payAmt").textContent = rupees(o.amount_paise);
  $("cAmt").textContent = rupees(o.amount_paise); $("cTok").textContent = "#" + o.token;
  if (awaiting && o.upi) {
    $("upiApp").href = o.upi.uri;
    $("upiId").textContent = o.upi.upi_id;
    const src = `/api/orders/${o.id}/upi.png?k=${encodeURIComponent(current.k)}`;
    if ($("upiQr").getAttribute("src") !== src) $("upiQr").src = src;
    $("claimNote").hidden = !o.customer_claimed_paid;
    $("claimPaid").textContent = o.customer_claimed_paid ? "Payment sent ✓" : "I have paid";
  }
  $("statusItems").innerHTML = o.items.map((i) => `
    <div class="sumrow"><div><div>${esc(i.filename)}</div>
      <div class="muted">${i.selected_pages} pg · ${i.color_mode === "bw" ? "B&W" : "Colour"} · ${i.sides === "one" ? "1-sided" : "2-sided"} · ×${i.copies}</div></div>
      <span class="pill ${i.status === "printed" ? "printed" : i.status === "failed" ? "failed" : i.status === "printing" ? "printing" : "queued"}">${
        i.status === "printed" ? "Printed" : i.status === "failed" ? "Problem" : i.status === "printing" ? "Printing" : "Waiting"}</span></div>`).join("");
  if (st === "printed") { document.title = `✅ Token #${o.token} ready`; }
  if (FINAL.includes(st)) stopPolling();
}

async function poll() {
  if (!current) return;
  try {
    order = await api(`/api/orders/${current.id}?k=${encodeURIComponent(current.k)}`);
    renderStatus();
  } catch (e) {
    if (/not found/i.test(e.message)) { forget(); }
  }
}
function startPolling() { stopPolling(); pollTimer = setInterval(poll, 2500); }
function stopPolling() { if (pollTimer) clearInterval(pollTimer); pollTimer = null; }
function forget() { stopPolling(); current = null; order = null; localStorage.removeItem(STORE_KEY); }

$("newOrder").addEventListener("click", () => {
  if (order && !FINAL.includes(order.status) && order.status !== "failed") {
    if (!confirm(`Token #${order.token} is not finished yet. Start a new order anyway? (This one continues.)`)) return;
  }
  forget(); go("files");
  if (shop && !shop.accepting_orders) location.reload();
});
document.addEventListener("visibilitychange", () => { if (!document.hidden && current) poll(); });

// ── boot ──
(async function boot() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORE_KEY) || "null");
    if (saved && Date.now() - saved.t < 12 * 3600 * 1000) current = { id: saved.id, k: saved.k };
  } catch {}
  try { await loadShop(); } catch { toast("Can't reach the shop. Check you're on the shop's Wi-Fi.", 8000); return; }
  if (current) {
    await poll();
    if (order && !FINAL.includes(order.status)) { go("status"); startPolling(); return; }
    if (order && order.status === "printed" && Date.now() - order.created_at * 1000 < 3600 * 1000) { go("status"); return; }
    forget();
  }
  renderFiles();
})();
