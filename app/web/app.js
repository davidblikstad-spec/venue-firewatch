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
  ws.onclose = () => { setConn("down"); setTimeout(connect, 3000); };
  ws.onerror = () => ws.close();
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "snapshot") { state = msg.data; render(); loadAudit(); }
  };
}

function setConn(s) {
  const el = $("conn");
  el.dataset.state = s;
  el.textContent = s === "up" ? "linked" : "no link";
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

  // UPS
  renderUps();

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

function detRow(d) {
  let status = "ok";
  if (d.alarm) status = "alarm";
  else if (!d.online) status = "offline";
  else if (d.fault) status = "fault";
  const tele = [
    d.temperature != null ? `${d.temperature}°C` : null,
    d.battery != null ? `${d.battery}%` : null,
    !d.online ? "offline" : null,
  ].filter(Boolean).join("  ·  ");
  return `<li class="det" data-status="${status}">
    <span class="led"></span>
    <span><span class="det-name">${esc(d.label)}</span> <span class="det-zone">${esc(d.zone)}</span></span>
    <span class="det-tele">${esc(tele || "—")}</span>
  </li>`;
}

function renderUps() {
  const el = $("ups");
  const u = state.ups;
  if (!u) { el.innerHTML = '<span class="empty">No UPS configured</span>'; return; }
  const status = u.on_battery ? "ON BATTERY" : (u.online ? "online" : "unreachable");
  const bad = u.on_battery || u.low_battery || !u.online;
  el.innerHTML = `
    <div><span class="k">status </span><span class="${bad ? "ups-bad" : ""}">${status}</span></div>
    ${u.charge_pct != null ? `<div><span class="k">charge </span>${u.charge_pct}%</div>` : ""}
    ${u.low_battery ? '<div class="ups-bad">LOW BATTERY</div>' : ""}`;
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
    const rows = await (await fetch("/api/audit?limit=40")).json();
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

// ---- utils ----
function esc(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function trunc(s, n = 48) { s = String(s); return s.length > n ? s.slice(0, n) + "…" : s; }

connect();
