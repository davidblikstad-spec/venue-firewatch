"use strict";

const $ = (id) => document.getElementById(id);
const html = document.documentElement;

let state = null;
let countdownTimer = null;

// ---- WebSocket with auto-reconnect ----
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => setConn("up");
  ws.onclose = async (ev) => {
    setConn("down");
    if (ev.code === 4001) { location.href = "/login"; return; }
    // A 403 handshake rejection (e.g. session cleared by a server restart)
    // surfaces as a generic 1006 close, not 4001 — so check auth explicitly
    // instead of retrying /ws forever.
    try {
      const r = await fetch("/api/state", { cache: "no-store" });
      if (r.status === 401) { location.href = "/login"; return; }
    } catch { /* server unreachable — fall through and retry */ }
    setTimeout(connect, 3000);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "snapshot") { state = msg.data; render(); loadAudit(); }
  };
}

function setConn(s) {
  // Browser <-> FireWatch server WebSocket. Label stays "server"; colour says state.
  $("conn").dataset.state = s;
}

function renderLink() {
  // Zigbee pipeline health, independent of the dashboard's own connection.
  const el = $("zlink");
  const l = (state && state.link) || {};
  if (l.mqtt_connected && l.zigbee_online) {
    el.dataset.state = "up"; el.textContent = "zigbee";
    el.title = "Zigbee2MQTT online — receiving detector data";
  } else if (l.mqtt_connected) {
    el.dataset.state = "warn"; el.textContent = "mqtt only";
    el.title = "Connected to the MQTT broker, but Zigbee2MQTT reports offline";
  } else {
    el.dataset.state = "down"; el.textContent = "no mqtt";
    el.title = "Not connected to the MQTT broker — no detector data";
  }
}

// ---- Render ----
function anyAlarm(d) { return d.detectors.some((x) => x.alarm); }

function render() {
  if (!state) return;
  const mode = anyAlarm(state) ? "alarm" : state.mode;
  html.dataset.mode = mode;

  // Banner
  $("bannerMode").textContent = anyAlarm(state) ? "ALARM" : state.mode.toUpperCase();
  if (anyAlarm(state)) {
    $("bannerLine").textContent = "Active detection — check the floor";
  } else if (state.mode === "event") {
    $("bannerLine").textContent = "Alerts silenced for haze zones — fire watch is the detection";
  } else {
    $("bannerLine").textContent = "Detection live — alerts will be sent";
  }
  renderCountdown();

  // Detectors
  const list = $("detectors");
  if (!state.detectors.length) {
    list.innerHTML = '<li class="empty">No detectors configured</li>';
  } else {
    list.innerHTML = state.detectors.map(detRow).join("");
  }

  // Zigbee/MQTT pipeline health
  renderLink();

  // Internet uplinks
  renderWan();

  // UPS
  renderUps();

  // Balance
  renderBalance();

  // Controls
  $("armBtn").hidden = state.mode === "event";
  $("endBtn").hidden = state.mode !== "event";
  document.querySelectorAll(".seg-btn").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.policy === state.sms_policy))
  );
  $("policyHint").textContent =
    state.sms_policy === "both"
      ? "Both channels fire together. Recipients may get two texts."
      : "GatewayAPI first; the modem takes over only if it fails.";
}

function timeAgo(iso) {
  if (!iso) return "no data";
  const s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function metric(label, value, cls = "") {
  return `<span class="m ${cls}"><span class="ml">${label}</span>${esc(value)}</span>`;
}

// Z2M reports link quality as an LQI in 0–255, which means nothing to a
// non-expert. Show it as a percentage + a 4-bar indicator, coloured by tier,
// and keep the raw value in the tooltip.
function signalCell(lqi) {
  const pct = Math.round((lqi / 255) * 100);
  const level = lqi <= 63 ? 1 : lqi <= 127 ? 2 : lqi <= 191 ? 3 : 4;
  const cls = level === 1 ? "m-bad" : level === 2 ? "m-warn" : "";
  const bars = "█".repeat(level) + "░".repeat(4 - level);
  return `<span class="m ${cls}" title="Zigbee link quality ${esc(lqi)}/255">` +
    `<span class="ml">signal</span><span class="sig-bars">${bars}</span> ${pct}%</span>`;
}

function detRow(d) {
  let status = "ok", badge = "OK";
  if (d.alarm) { status = "alarm"; badge = "ALARM"; }
  else if (!d.online) { status = "offline"; badge = "OFFLINE"; }
  else if (d.fault) { status = "fault"; badge = "FAULT"; }

  const sub = [d.kind, d.zone].filter(Boolean).join("  ·  ");

  const battCls = d.battery == null ? ""
    : d.battery <= 10 ? "m-bad"
    : d.battery <= 20 ? "m-warn" : "";

  const metrics = [
    d.temperature != null ? metric("temp", `${Number(d.temperature).toFixed(1)}°C`) : "",
    d.battery != null ? metric("batt", `${d.battery}%`, battCls) : "",
    d.linkquality != null ? signalCell(d.linkquality) : "",
    metric("seen", timeAgo(d.last_seen)),
  ].filter(Boolean).join("");

  const seenTitle = d.last_seen ? `Last message: ${new Date(d.last_seen).toLocaleString()}` : "No message received yet";
  return `<li class="det" data-status="${status}" title="${esc(seenTitle)}">
    <span class="led"></span>
    <div class="det-main">
      <div class="det-head">
        <span class="det-name">${esc(d.label)}</span>
        <span class="det-zone">${esc(sub)}</span>
      </div>
      <div class="det-metrics">${metrics}</div>
    </div>
    <span class="det-badge badge-${status}">${badge}</span>
  </li>`;
}

function renderUps() {
  const el = $("ups");
  const u = state.ups;
  if (!u || !u.monitored) { el.innerHTML = '<span class="empty">Not monitored — no UPS wired</span>'; return; }
  const status = u.on_battery ? "ON BATTERY" : (u.online ? "online" : "unreachable");
  const bad = u.on_battery || u.low_battery || !u.online;
  const runtime = u.runtime_s != null
    ? (u.runtime_s >= 60 ? `${Math.round(u.runtime_s / 60)} min` : `${u.runtime_s}s`)
    : null;
  el.innerHTML = `
    <div><span class="k">status </span><span class="${bad ? "ups-bad" : ""}">${status}</span></div>
    ${u.grid_voltage != null ? `<div><span class="k">grid </span>${u.grid_voltage.toFixed(0)} V</div>` : ""}
    ${u.load_pct != null ? `<div><span class="k">load </span>${u.load_pct}%</div>` : ""}
    ${u.charge_pct != null ? `<div><span class="k">charge </span>${u.charge_pct}%</div>` : ""}
    ${runtime != null ? `<div><span class="k">runtime </span>${runtime}</div>` : ""}
    ${u.low_battery ? '<div class="ups-bad">LOW BATTERY</div>' : ""}`;
}

function renderWan() {
  const el = $("wan");
  const w = state.wan;
  if (!w || !w.monitored || !w.adapters || !w.adapters.length) {
    el.innerHTML = '<span class="empty">No uplink data</span>';
    return;
  }
  const rows = w.adapters.map((a) => {
    let cls, label;
    if (a.active) { cls = "wan-active"; label = "in use"; }
    else if (a.link) { cls = "wan-standby"; label = "standby"; }
    else { cls = "wan-down"; label = "down"; }
    const meta = [a.ip, a.metric != null ? `m${a.metric}` : null].filter(Boolean).join("  ·  ");
    return `<div class="wan-row">
      <span class="led"></span>
      <span class="wan-name">${esc(a.label)}</span>
      <span class="wan-meta">${esc(meta)}</span>
      <span class="${cls}">${label}</span>
    </div>`;
  }).join("");
  // No adapter active == the box currently has no internet at all.
  const none = w.adapters.every((a) => !a.active)
    ? '<div class="ups-bad">No internet — all uplinks down</div>' : "";
  el.innerHTML = rows + none;
}

function renderBalance() {
  const el = $("balance");
  const b = state.balance;
  if (!b || b.credit == null) {
    el.innerHTML = '<span class="empty">Not checked</span>';
    return;
  }
  const cls = b.low ? "ups-bad" : "";
  el.innerHTML = `
    <div><span class="k">credit </span><span class="${cls}">${b.credit.toFixed(2)}</span></div>
    ${b.low ? '<div class="ups-bad">LOW — top up SMS credits</div>' : ""}
    ${b.last_checked ? `<div><span class="k">checked </span>${new Date(b.last_checked).toLocaleTimeString()}</div>` : ""}`;
}

function renderCountdown() {
  const el = $("countdown");
  if (countdownTimer) { clearInterval(countdownTimer); countdownTimer = null; }
  if (state.mode !== "event" || !state.event_until) { el.hidden = true; return; }
  el.hidden = false;
  const tick = () => {
    const ms = new Date(state.event_until).getTime() - Date.now();
    if (ms <= 0) { el.textContent = "reverting…"; return; }
    const m = Math.floor(ms / 60000), s = Math.floor((ms % 60000) / 1000);
    el.textContent = `auto-revert in ${m}m ${String(s).padStart(2, "0")}s`;
  };
  tick();
  countdownTimer = setInterval(tick, 1000);
}

// ---- Audit ----
async function loadAudit() {
  try {
    const resp = await fetch("/api/audit?limit=40");
    if (resp.status === 401) { location.href = "/login"; return; }
    const rows = await resp.json();
    const el = $("audit");
    el.innerHTML = rows.length
      ? rows.map(auditRow).join("")
      : '<li class="empty">—</li>';
  } catch { /* ignore */ }
}

function auditRow(r) {
  const cls = r.severity === "critical" ? "a-crit" : r.severity === "warning" ? "a-warn" : "";
  const t = new Date(r.ts).toLocaleTimeString();
  const text = summarize(r);
  return `<li><span class="a-ts">${t}</span><span class="${cls}">${esc(text)}</span></li>`;
}

function summarize(r) {
  const d = r.detail || {};
  switch (r.kind) {
    case "alarm": return `ALARM ${d.detector}${d.silenced ? " (silenced)" : ""}`;
    case "mode_change": return `mode ${d.from} → ${d.to}`;
    case "sms": return `SMS ${d.policy}: ${trunc(d.text)}`;
    case "dlr": return `DLR ${d.message_id}: ${d.status}`;
    case "escalation": return `ESCALATED to ${(d.recipients || []).join(", ")}`;
    case "fault": return `fault: ${trunc(JSON.stringify(d))}`;
    default: return `${r.kind}: ${trunc(JSON.stringify(d))}`;
  }
}

// ---- Controls ----
function defaultUntil(hours) {
  const d = new Date(Date.now() + hours * 3600_000);
  d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
  return d.toISOString().slice(0, 16);
}

$("until").value = defaultUntil(4);

document.querySelectorAll(".quick button").forEach((b) =>
  b.addEventListener("click", () => { $("until").value = defaultUntil(Number(b.dataset.hours)); })
);

$("armBtn").addEventListener("click", async () => {
  const val = $("until").value;
  if (!val) return;
  await fetch("/api/event/arm", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ until: new Date(val).toISOString(), actor: "dashboard" }),
  });
});

$("endBtn").addEventListener("click", async () => {
  await fetch("/api/event/end", { method: "POST" });
});

document.querySelectorAll(".seg-btn").forEach((b) =>
  b.addEventListener("click", async () => {
    await fetch("/api/sms/policy", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ policy: b.dataset.policy, actor: "dashboard" }),
    });
  })
);

// (The 🐝 Zigbee2MQTT link href is set inline in index.html so it survives a
// cached app.js.)

// ---- utils ----
function esc(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function trunc(s, n = 48) { s = String(s); return s.length > n ? s.slice(0, n) + "…" : s; }

connect();
