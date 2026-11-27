"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const rupees = (p) => "₹" + (p / 100).toLocaleString("en-IN", { maximumFractionDigits: 2 });
const timeOf = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

let state = null;
let rev = -1;
let ordersCache = [];
let seenAttention = new Set();
let soundOn = localStorage.getItem("qp_sound") !== "off";
let pollTimer = null;
let firstLoad = true;

function toast(msg, ms = 3000) {
  const t = $("toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), ms);
}

async function api(path, opts = {}) {
  const headers = { "X-Admin": "1" };
  if (opts.body && !(opts.body instanceof FormData)) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { ...opts, headers: { ...headers, ...(opts.headers || {}) }, credentials: "same-origin" });
  let data = null;
  try { data = await res.json(); } catch {}
  if (res.status === 401 && !path.includes("/login") && !path.includes("/pin")) { showAuth(); throw new Error("Please log in"); }
  if (!res.ok) throw new Error((data && typeof data.detail === "string" && data.detail) || `Error ${res.status}`);
  return data;
}

// ── auth ──
let authMode = "login";
async function boot() {
  const s = await api("/api/admin/session");
  if (s.logged_in) return startApp();
  showAuth(s);
}
async function showAuth(s) {
  stopPolling();
  s = s || await api("/api/admin/session").catch(() => ({ pin_set: true }));
  $("app").hidden = true; $("authView").hidden = false;
  if (!s.pin_set) {
    authMode = "setup";
    $("authTitle").textContent = "Welcome! Create your shop PIN";
    $("authHelp").textContent = s.local
      ? "This PIN protects your dashboard, prices and payment settings. Choose 4-12 digits."
      : "Open this page on the shop computer itself (http://localhost:8500/admin) to create the PIN.";
    $("pin2").hidden = false; $("authBtn").textContent = "Create PIN";
    $("authBtn").disabled = !s.local;
  } else {
    authMode = "login";
    $("authTitle").textContent = s.shop_name || "Shop login";
    $("authHelp").textContent = "Enter your shop PIN.";
    $("pin2").hidden = true; $("authBtn").textContent = "Log in";
  }
  $("pin").focus();
}
$("authForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("authErr"); err.hidden = true;
  const pin = $("pin").value.trim();
  try {
    if (authMode === "setup") {
      if (pin !== $("pin2").value.trim()) throw new Error("The two PINs don't match.");
      await api("/api/admin/setup", { method: "POST", body: JSON.stringify({ pin }) });
    } else {
      await api("/api/admin/login", { method: "POST", body: JSON.stringify({ pin }) });
    }
    $("pin").value = $("pin2").value = "";
    startApp();
  } catch (ex) { err.textContent = ex.message; err.hidden = false; }
});
$("logout").addEventListener("click", async () => { await api("/api/admin/logout", { method: "POST" }).catch(() => {}); showAuth(); });

// ── tabs ──
$("tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-tab]"); if (!b) return;
  document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("on", x === b));
  document.querySelectorAll(".tab").forEach((t) => (t.hidden = t.id !== "tab-" + b.dataset.tab));
  if (b.dataset.tab === "reports") loadReport();
  if (b.dataset.tab === "qr") $("qrImg").src = "/api/qr.png?t=" + Date.now();
});

// ── app ──
async function startApp() {
  $("authView").hidden = true; $("app").hidden = false;
  await loadState();
  rev = -1; firstLoad = true;
  await loadOrders();
  startPolling();
}
function startPolling() {
  stopPolling();
  let n = 0;
  pollTimer = setInterval(() => { loadOrders(); if (++n % 10 === 0) loadState(); }, 2000);
}
function stopPolling() { if (pollTimer) clearInterval(pollTimer); pollTimer = null; }

async function loadState() {
  state = await api("/api/admin/state");
  const s = state.settings;
  $("tShop").textContent = s.shop_name;
  const pr = state.printers.find((p) => p.name === state.active_printer);
  const cp = state.color_printer !== state.active_printer ? state.printers.find((p) => p.name === state.color_printer) : null;
  $("tPrinter").innerHTML = (pr ? `<span class="dot ${pr.ok ? "" : "bad"}"></span>${esc(pr.name)} · ${esc(pr.status)}` : "No printer selected")
    + (cp ? ` &nbsp; <span class="dot ${cp.ok ? "" : "bad"}"></span>Colour: ${esc(cp.name)} · ${esc(cp.status)}` : "");
  $("tAccepting").checked = s.accepting_orders;
  $("tAuto").checked = s.auto_print;
  $("tDuplex").checked = s.duplex_mode !== "off";
  if (s.duplex_mode !== "off") { try { localStorage.setItem("qp_duplex_on_mode", s.duplex_mode); } catch {} }
  $("soundBtn").textContent = soundOn ? "🔔 Sound on" : "🔕 Sound off";

  // alerts
  const alerts = [];
  if (cp && !cp.ok) alerts.push(["bad", `Colour printer problem: ${cp.status}.`]);
  if (!pr) alerts.push(["bad", "No printer is selected — open 'Printer & prices'."]);
  else if (!pr.ok) alerts.push(["bad", `Printer problem: ${pr.status}. Orders will wait/fail until fixed.`]);
  else if (pr.virtual) alerts.push(["", `"${pr.name}" is not a real printer. Choose your actual printer in 'Printer & prices'.`]);
  if (!s.razorpay_key_id && !s.upi_id && s.counter_payment === "off") alerts.push(["bad", "No payment method set up."]);
  if (!state.mobile.ok && s.enable_tunnel) alerts.push(["", "Mobile data (4G/5G): " + state.mobile.text]);
  if (!s.accepting_orders) alerts.push(["", "You are NOT taking orders. Customers see a 'closed' message."]);
  const ab = $("alertBar");
  ab.hidden = !alerts.length;
  ab.className = "alertbar" + (alerts.some((a) => a[0] === "bad") ? " bad" : "");
  ab.textContent = alerts.map((a) => a[1]).join("  •  ");

  // setup fields (don't overwrite a field the user is typing in)
  const setVal = (id, v) => { const el = $(id); if (el && document.activeElement !== el) el.value = v ?? ""; };
  ["price_bw", "price_color", "min_order_amount", "shop_name", "shop_address", "shop_phone", "file_retention_minutes",
    "razorpay_key_id", "upi_id", "upi_payee_name", "counter_payment", "payment_timeout_minutes", "registry_url",
    "shop_id", "public_url"].forEach((k) => setVal(k, s[k]));
  setVal("price_bw_duplex", s.price_bw_duplex || "");
  setVal("price_color_duplex", s.price_color_duplex || "");
  $("razorpay_key_secret").placeholder = s.razorpay_secret_set ? "•••••••• saved (type to change)" : "Key secret";
  $("rzpPill").hidden = !(s.razorpay_key_id && s.razorpay_secret_set);
  $("enable_tunnel").checked = s.enable_tunnel;
  $("manual_duplex_reverse").checked = s.manual_duplex_reverse;
  if (document.activeElement !== $("duplex_mode")) $("duplex_mode").value = s.duplex_mode || "auto";
  const real = state.printers.filter((p) => !p.virtual || p.name === s.printer_color);
  if (document.activeElement !== $("printer_color")) {
    $("printer_color").innerHTML = `<option value="">Same printer as B&amp;W</option>` +
      real.filter((p) => p.name !== state.active_printer).map((p) =>
        `<option value="${esc(p.name)}" ${p.name === s.printer_color ? "selected" : ""}>${esc(p.name)}${p.caps.color ? "" : " (B&W only!)"}</option>`).join("");
  }
  const targets = [...new Set([state.active_printer, state.color_printer])].filter(Boolean);
  $("duplexInfo").textContent = s.duplex_mode === "off" ? "Customers only see 1-sided."
    : targets.map((n) => {
        const p = state.printers.find((x) => x.name === n);
        const auto = p && p.caps.duplex && s.duplex_mode === "auto";
        return `${n}: ${auto ? "flips paper by itself" : "you turn the pages over when asked"}`;
      }).join(" · ");
  $("autostart").checked = state.autostart.enabled;
  $("autostart").disabled = !state.autostart.supported;
  $("papers").innerHTML = state.paper_sizes.map((p) =>
    `<label><input type="checkbox" value="${p}" ${s.allowed_paper_sizes.includes(p) ? "checked" : ""}>${p}</label>`).join("");

  $("printers").innerHTML = state.printers.map((p) => `
    <div class="printer ${p.name === state.active_printer ? "sel" : ""}" data-name="${esc(p.name)}">
      <div><div class="pn">${esc(p.name)}${p.is_default ? ' <span class="muted small">(Windows default)</span>' : ""}</div>
        <div class="ps muted"><span class="dot ${p.ok ? "" : "bad"}"></span>${esc(p.status)} · ${p.caps.color ? "Colour" : "B&W only"}
        · ${p.caps.duplex ? "2-sided" : "1-sided"}${p.virtual ? " · not a real printer" : ""}${p.jobs ? ` · ${p.jobs} in Windows queue` : ""}</div></div>
      ${p.name === state.active_printer ? "<strong>✓</strong>" : '<button class="btn sm">Use</button>'}
    </div>`).join("") || '<div class="muted">No printers installed in Windows.</div>';

  $("custUrl").textContent = state.customer_url;
  $("openCust").href = state.customer_url;
  $("lanUrl").textContent = state.local_url;
  $("tunnelUrl").textContent = state.tunnel.url || ({ cloudflared_not_installed: "cloudflared not installed", inactive: "off", starting: "starting…" }[state.tunnel.status] || state.tunnel.status);
  $("regStatus").textContent = state.registry_status || (s.registry_url ? "waiting" : "not used");
  $("qrNote").textContent = state.customer_url.startsWith("http://")
    ? "This QR works only on your shop Wi-Fi." : "This QR never changes - print the poster once.";
  $("mobileStatus").innerHTML = `<span class="dot ${state.mobile.ok ? "" : "warn"}"></span>${esc(state.mobile.text)}`;
  $("verInfo").textContent = "v" + state.version;
}

$("printers").addEventListener("click", async (e) => {
  const row = e.target.closest(".printer"); if (!row || row.classList.contains("sel")) return;
  try { await api("/api/admin/settings", { method: "POST", body: JSON.stringify({ printer_name: row.dataset.name }) });
    toast("Printer changed"); loadState(); } catch (ex) { toast(ex.message); }
});
$("testPrint").addEventListener("click", async (e) => {
  e.target.disabled = true;
  try { toast((await api("/api/admin/test-print", { method: "POST" })).message); } catch (ex) { toast(ex.message, 6000); }
  e.target.disabled = false;
});

async function saveSettings(obj, msg = "Saved") {
  try { await api("/api/admin/settings", { method: "POST", body: JSON.stringify(obj) }); toast(msg); await loadState(); return true; }
  catch (ex) { toast(ex.message, 5000); return false; }
}
$("tAccepting").addEventListener("change", (e) => saveSettings({ accepting_orders: e.target.checked }, e.target.checked ? "Taking orders" : "Paused — customers see 'closed'"));
$("tDuplex").addEventListener("change", (e) => {
  let onMode = "auto";
  try { onMode = localStorage.getItem("qp_duplex_on_mode") || "auto"; } catch {}
  saveSettings({ duplex_mode: e.target.checked ? onMode : "off" },
    e.target.checked ? "2-sided is on - customers can choose it" : "2-sided is off - customers see 1-sided only");
});
$("tAuto").addEventListener("change", (e) => saveSettings({ auto_print: e.target.checked }, e.target.checked ? "Auto-print on" : "Auto-print off — you approve each order"));
$("enable_tunnel").addEventListener("change", () => {});
$("duplex_mode").addEventListener("change", (e) => saveSettings({ duplex_mode: e.target.value }, "2-sided setting saved"));
$("printer_color").addEventListener("change", (e) => saveSettings({ printer_color: e.target.value }, e.target.value ? `Colour jobs → ${e.target.value}` : "Colour jobs use the same printer"));
$("manual_duplex_reverse").addEventListener("change", (e) => saveSettings({ manual_duplex_reverse: e.target.checked }));
$("closeApp").addEventListener("click", async () => {
  const btn = $("closeApp");
  if (btn.dataset.armed !== "1") { btn.dataset.armed = "1"; btn.textContent = "Tap again to close"; setTimeout(() => { btn.dataset.armed = ""; btn.textContent = "Close QR Print Shop"; }, 4000); return; }
  try { await api("/api/admin/shutdown", { method: "POST" }); } catch {}
  stopPolling();
  document.body.innerHTML = '<div class="login"><div class="card"><div class="big-ico">✅</div><h2>QR Print Shop is closed</h2><p class="muted">Customers now see "shop is offline". To start again, double-click <b>QRPrintShop.exe</b> or the QR Print Shop icon.</p></div></div>';
});
document.querySelectorAll("[data-save]").forEach((btn) => btn.addEventListener("click", async () => {
  const body = {};
  for (const k of btn.dataset.save.split(",")) {
    if (k === "allowed_paper_sizes") { body[k] = [...$("papers").querySelectorAll("input:checked")].map((i) => i.value); continue; }
    const el = $(k);
    if (el.type === "checkbox") body[k] = el.checked;
    else if (el.type === "number") body[k] = el.value === "" ? 0 : Number(el.value);
    else body[k] = el.value;
  }
  btn.disabled = true;
  if (await saveSettings(body) && body.razorpay_key_secret) $("razorpay_key_secret").value = "";
  btn.disabled = false;
}));
$("rzpCheck").addEventListener("click", async () => {
  try { toast((await api("/api/admin/razorpay-check", { method: "POST" })).message, 5000); } catch (ex) { toast(ex.message, 6000); }
});
$("rzpClear").addEventListener("click", async () => {
  if (!confirm("Remove Razorpay keys? Customers will pay by UPI / counter instead.")) return;
  await saveSettings({ razorpay_key_id: "" }, "Razorpay removed");
});
$("autostart").addEventListener("change", async (e) => {
  try { await api(`/api/admin/autostart?enable=${e.target.checked}`, { method: "POST" }); toast(e.target.checked ? "Will start with Windows" : "Auto-start off"); }
  catch (ex) { toast(ex.message); e.target.checked = !e.target.checked; }
});
$("logoFile").addEventListener("change", async (e) => {
  const f = e.target.files[0]; if (!f) return;
  const fd = new FormData(); fd.append("file", f);
  try { await api("/api/admin/logo", { method: "POST", body: fd }); toast("Logo updated"); } catch (ex) { toast(ex.message); }
  e.target.value = "";
});
$("logoDel").addEventListener("click", async () => { await api("/api/admin/logo", { method: "DELETE" }).catch(() => {}); toast("Logo removed"); });
$("soundBtn").addEventListener("click", () => {
  soundOn = !soundOn; localStorage.setItem("qp_sound", soundOn ? "on" : "off");
  $("soundBtn").textContent = soundOn ? "🔔 Sound on" : "🔕 Sound off";
  if (soundOn) beep();
});
$("changePin").addEventListener("click", async () => {
  try {
    await api("/api/admin/pin", { method: "POST", body: JSON.stringify({ current_pin: $("curPin").value, new_pin: $("newPin").value }) });
    $("curPin").value = $("newPin").value = ""; toast("PIN changed. Other devices were logged out.");
  } catch (ex) { toast(ex.message, 5000); }
});

// ── orders ──
const STATUS_LABEL = {
  awaiting_payment: "Awaiting payment", waiting_approval: "Waiting for you", waiting_flip: "Turn pages over", queued: "Queued", printing: "Printing",
  printed: "Printed", failed: "Failed", cancelled: "Cancelled", expired: "Expired",
};
function needsAttention(o) {
  return o.status === "waiting_approval" || o.status === "failed" || o.status === "waiting_flip" ||
    (o.status === "awaiting_payment" && (o.pay_method === "counter" || o.customer_claimed_paid));
}

async function loadOrders() {
  let data;
  try { data = await api(`/api/admin/orders?rev=${rev}`); } catch { return; }
  if (!data.changed) return;
  rev = data.rev;
  ordersCache = data.orders;
  renderStats(data.summary, $("stats"), data.orders);
  renderOrders(data.orders, $("orders"), true);
  const attn = data.orders.filter(needsAttention);
  $("attnBadge").hidden = !attn.length; $("attnBadge").textContent = attn.length;
  document.title = (attn.length ? `(${attn.length}) ` : "") + "Print Shop · Dashboard";
  // alert for new things that need the shopkeeper, and for brand-new orders
  const fresh = data.orders.filter((o) => (needsAttention(o) ? !seenAttention.has(o.id + o.status + o.customer_claimed_paid) : false));
  if (!firstLoad && fresh.length) { beep(); notify(fresh[0]); }
  data.orders.forEach((o) => { if (needsAttention(o)) seenAttention.add(o.id + o.status + o.customer_claimed_paid); });
  firstLoad = false;
}

function renderStats(sm, el, orders) {
  el.innerHTML = [
    ["Orders", sm.orders], ["Printed", sm.printed], ["In progress", sm.pending],
    ["Collected", rupees(sm.collected_paise)], ["B&W / Colour sides", `${sm.bw_pages} / ${sm.color_pages}`],
    ...(sm.unpaid_printed_paise ? [["Printed, unpaid", rupees(sm.unpaid_printed_paise)]] : []),
  ].map(([k, v]) => `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
}

function renderOrders(list, el, actions) {
  if (!list.length) { el.innerHTML = `<div class="empty">No orders yet today.<br>Put the QR poster on your counter — orders appear here instantly.</div>`; return; }
  el.innerHTML = list.map((o) => {
    const attn = needsAttention(o);
    const items = o.items.map((i) => {
      const link = o.files_deleted ? esc(i.filename) : `<a href="/api/admin/items/${i.id}/file" target="_blank" rel="noopener">${esc(i.filename)}</a>`;
      return `<div>${i.status === "printed" ? "✅" : i.status === "failed" ? "❌" : "📄"} ${link}
        <span class="muted">· ${i.selected_pages} pg${i.page_range !== "all" ? ` (${esc(i.page_range)})` : ""} · ${i.color_mode === "bw" ? "B&W" : "Colour"}
        · ${i.sides === "one" ? "1-sided" : "2-sided"} · ×${i.copies} · ${esc(i.paper_size)}</span>
        ${i.error ? `<div class="msg bad">${esc(i.error)}</div>` : ""}</div>`;
    }).join("");
    const payTxt = { razorpay: "Online (Razorpay)", upi: "UPI", counter: "Counter", free: "Free" }[o.pay_method] || o.pay_method;
    let btns = "";
    if (actions) {
      if (o.status === "awaiting_payment") btns += `<button class="btn sm ok" data-act="confirm" data-id="${o.id}">Paid ✓ ${o.pay_method === "razorpay" ? "(manual)" : ""}</button>`;
      if (o.status === "waiting_approval") btns += `<button class="btn sm ok" data-act="print" data-id="${o.id}">Print now</button>`;
      if (o.status === "waiting_flip") btns += `<button class="btn sm ok" data-act="flip" data-id="${o.id}">Pages turned → Print backs</button>`;
      if (["printed", "failed", "cancelled", "expired"].includes(o.status) && !o.files_deleted && (o.paid || o.status !== "expired"))
        btns += `<button class="btn sm" data-act="print" data-id="${o.id}">${o.status === "failed" ? "Retry" : "Reprint"}</button>`;
      if (o.status === "printed" && !o.paid && o.amount_paise) btns += `<button class="btn sm ok" data-act="confirm" data-id="${o.id}">Cash received</button>`;
      if (["awaiting_payment", "waiting_approval", "waiting_flip", "queued", "failed"].includes(o.status)) btns += `<button class="btn sm bad" data-act="cancel" data-id="${o.id}">Cancel</button>`;
    }
    return `<div class="order ${attn ? "attn" : ""} ${o.status === "failed" ? "failed" : ""}">
      <div><div class="tok">#${o.token}</div><div class="when">${timeOf(o.created_at)}</div></div>
      <div><div class="who">${esc(o.customer_name || "Customer")} <span class="pill ${o.status}">${STATUS_LABEL[o.status] || o.status}</span></div>
        <div class="items">${items}</div>
        <div class="msg ${o.status === "failed" ? "bad" : ""}">${esc(o.message)}</div>
        ${o.status === "waiting_flip" ? `<div class="flipbox"><b>Front sides are printed.</b><ol>
          <li>Take all printed sheets from the output tray. Keep them in the same order.</li>
          <li>Turn the whole stack over (blank sides now face the way the fronts did).</li>
          <li>Put it back in the paper tray with the same top edge going in first.</li>
          <li>Press <b>Print backs</b>.</li></ol></div>` : ""}
        ${o.customer_claimed_paid && o.status === "awaiting_payment" ? `<div class="claimed">Customer says paid${o.payment_ref ? " · UTR " + esc(o.payment_ref) : ""} — check your UPI app</div>` : ""}
      </div>
      <div class="right"><div class="amt">${rupees(o.amount_paise)}</div>
        <div class="muted small" style="text-align:right">${payTxt} · ${o.paid ? "paid" : "unpaid"}</div>
        <div class="acts">${btns}</div></div>
    </div>`;
  }).join("");
}

$("orders").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-act]"); if (!b) return;
  const o = ordersCache.find((x) => x.id === b.dataset.id);
  if (b.dataset.act === "cancel" && !confirm(`Cancel token #${o ? o.token : ""}?`)) return;
  if (b.dataset.act === "confirm" && o && o.status === "awaiting_payment" &&
      !confirm(`Confirm you received ${rupees(o.amount_paise)} for token #${o.token}? It will print immediately.`)) return;
  b.disabled = true;
  try {
    const path = { confirm: "confirm-payment", print: "print", cancel: "cancel", flip: "flip-done" }[b.dataset.act];
    await api(`/api/admin/orders/${b.dataset.id}/${path}`, { method: "POST", body: JSON.stringify({}) });
    rev = -1; loadOrders();
  } catch (ex) { toast(ex.message, 5000); b.disabled = false; }
});

// ── reports ──
function todayStr() { const d = new Date(); d.setMinutes(d.getMinutes() - d.getTimezoneOffset()); return d.toISOString().slice(0, 10); }
$("repDay").value = todayStr();
$("repDay").addEventListener("change", loadReport);
async function loadReport() {
  const day = $("repDay").value || todayStr();
  $("repCsv").href = `/api/admin/report.csv?day=${day}`;
  try {
    const d = await api(`/api/admin/orders?day=${day}`);
    renderStats(d.summary, $("repStats"), d.orders);
    renderOrders(d.orders, $("repOrders"), false);
  } catch (ex) { toast(ex.message); }
}

// ── alerts ──
let audioCtx = null;
function beep() {
  if (!soundOn) return;
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    [0, 0.18].forEach((t, i) => {
      const o = audioCtx.createOscillator(), g = audioCtx.createGain();
      o.frequency.value = i ? 1046 : 784; o.connect(g); g.connect(audioCtx.destination);
      g.gain.setValueAtTime(0.0001, audioCtx.currentTime + t);
      g.gain.exponentialRampToValueAtTime(0.3, audioCtx.currentTime + t + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + t + 0.16);
      o.start(audioCtx.currentTime + t); o.stop(audioCtx.currentTime + t + 0.17);
    });
  } catch {}
}
function notify(o) {
  if (!("Notification" in window)) return;
  const body = `Token #${o.token} · ${STATUS_LABEL[o.status]} · ${rupees(o.amount_paise)}`;
  if (Notification.permission === "granted") new Notification("Print order needs you", { body });
  else if (Notification.permission === "default") Notification.requestPermission();
}
document.addEventListener("click", () => { if (audioCtx && audioCtx.state === "suspended") audioCtx.resume(); }, { once: true });

boot().catch(() => toast("Server not reachable"));
