"use strict";

const $ = (id) => document.getElementById(id);
const html = document.documentElement;

let state = null;
let countdownTimer = null;

const MODE_LABELS = { normal: "NORMAL", event: "EVENTMODUS" };
const KIND_LABELS = { smoke: "røyk", heat: "varme", other: "annet" };

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
    el.title = "Zigbee2MQTT tilkoblet — mottar data fra detektorene";
  } else if (l.mqtt_connected) {
    el.dataset.state = "warn"; el.textContent = "kun mqtt";
    el.title = "Koblet til MQTT-brokeren, men Zigbee2MQTT melder frakoblet";
  } else {
    el.dataset.state = "down"; el.textContent = "ingen mqtt";
    el.title = "Ikke koblet til MQTT-brokeren — ingen data fra detektorene";
  }
}

// ---- Render ----
function render() {
  if (!state) return;
  const fires = state.detectors.filter((x) => x.alarm);
  const sirens = sirensSounding();
  const mode = fires.length || sirens ? "alarm" : state.mode;
  html.dataset.mode = mode;

  // Venue name in the header (and page title); falls back to the default sub.
  const venue = (state.venue || "").trim();
  $("brandSub").textContent = venue || "sekundær overvåking";
  document.title = venue ? `${venue} — FireWatch` : "Venue FireWatch";

  // Banner. On a trip the headline reads FIRE and the sub-line names where.
  // Interconnect sirens outlive the trip (they run on their own timer), so
  // a cleared detector with sirens still going must not read "normal".
  $("bannerMode").textContent = fires.length ? "BRANN" : sirens ? (state.sirens_test ? "SIRENETEST" : "SIRENER") : (MODE_LABELS[state.mode] || state.mode.toUpperCase());
  $("bannerSilenceBtn").hidden = !sirens;
  if (fires.length) {
    const f = fires[0];
    const where = f.zone ? `${f.label} — sone ${f.zone}` : f.label;
    $("bannerLine").textContent = fires.length > 1 ? `${where} (+${fires.length - 1} til)` : where;
  } else if (sirens && state.sirens_test) {
    $("bannerLine").textContent = "Sirenetest pågår — ikke brann, ingen SMS sendt";
  } else if (sirens) {
    $("bannerLine").textContent =
      `Brannsirenene piper i hele bygget — utløst av ${state.sirens_origin || "en detektor"}, som nå er normal igjen`;
  } else if (state.mode === "event") {
    $("bannerLine").textContent = "Sirenelink avslått · SMS sendes fortsatt; bare detektoren som løser ut piper";
  } else {
    $("bannerLine").textContent = "Overvåking aktiv — varsler sendes";
  }
  renderCountdown();

  // Detectors
  const list = $("detectors");
  if (!state.detectors.length) {
    list.innerHTML = '<li class="empty">Ingen detektorer satt opp</li>';
  } else {
    list.innerHTML = state.detectors.map(detRow).join("");
  }

  // Mesh repeaters (smart plugs etc.)
  renderMesh();

  // Zigbee/MQTT pipeline health
  renderLink();

  // Internet uplinks
  renderWan();

  // UPS
  renderUps();

  // Balance
  renderBalance();

  // Uptime (immediate; a 1s interval keeps it ticking between snapshots)
  tickUptime();

  // Controls
  $("armBtn").hidden = state.mode === "event";
  $("endBtn").hidden = state.mode !== "event";
  document.querySelectorAll(".seg-btn").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.policy === state.sms_policy))
  );
  $("policyHint").textContent =
    state.sms_policy === "both"
      ? "Begge kanalene sender samtidig. Mottakerne kan få to SMS."
      : "GatewayAPI først; modemet tar over bare hvis det feiler.";
}

function timeAgo(iso) {
  if (!iso) return "ingen data";
  const s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 5) return "akkurat nå";
  if (s < 60) return `${s} s siden`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min siden`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} t siden`;
  return `${Math.floor(h / 24)} d siden`;
}

function metric(label, value, cls = "") {
  return `<span class="m ${cls}"><span class="ml">${label}</span>${esc(value)}</span>`;
}

// FireWatch process uptime, ticking once a second off the snapshot's start time.
function fmtUptime(ms) {
  let s = Math.max(0, Math.floor(ms / 1000));
  const d = Math.floor(s / 86400); s -= d * 86400;
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60); s -= m * 60;
  const p = (n) => String(n).padStart(2, "0");
  if (d > 0) return `oppe ${d}d ${p(h)}t ${p(m)}m`;
  if (h > 0) return `oppe ${h}t ${p(m)}m`;
  return `oppe ${m}m ${p(s)}s`;
}

function tickUptime() {
  const el = $("uptime");
  if (!state || !state.started_at) { el.textContent = ""; return; }
  el.textContent = fmtUptime(Date.now() - new Date(state.started_at).getTime());
}

// Z2M reports link quality as an LQI in 0–255, which means nothing to a
// non-expert. Show it as a percentage + a 4-bar indicator, coloured by tier,
// and keep the raw value in the tooltip.
function signalCell(lqi) {
  const pct = Math.round((lqi / 255) * 100);
  const level = lqi <= 63 ? 1 : lqi <= 127 ? 2 : lqi <= 191 ? 3 : 4;
  const cls = level === 1 ? "m-bad" : level === 2 ? "m-warn" : "";
  const bars = "█".repeat(level) + "░".repeat(4 - level);
  return `<span class="m ${cls}" title="Zigbee-signalkvalitet ${esc(lqi)}/255">` +
    `<span class="ml">signal</span><span class="sig-bars">${bars}</span> ${pct}%</span>`;
}

function detRow(d) {
  let status = "ok", badge = "OK";
  if (d.alarm) { status = "alarm"; badge = "BRANN"; }
  else if (d.alarm_unconfirmed) { status = "fault"; badge = "UBEKREFTET"; }
  else if (!d.online) { status = "offline"; badge = "FRAKOBLET"; }
  else if (d.fault) { status = "fault"; badge = "FEIL"; }

  const sub = [KIND_LABELS[d.kind] || d.kind, d.zone].filter(Boolean).join("  ·  ");

  const battCls = d.battery == null ? ""
    : d.battery <= 10 ? "m-bad"
    : d.battery <= 20 ? "m-warn" : "";

  const metrics = [
    d.temperature != null ? metric("temp", `${Number(d.temperature).toFixed(1)}°C`) : "",
    d.battery != null ? metric("batteri", `${d.battery}%`, battCls) : "",
    d.linkquality != null ? signalCell(d.linkquality) : "",
    metric("sett", timeAgo(d.last_seen)),
  ].filter(Boolean).join("");

  const seenTitle = d.last_seen ? `Siste melding: ${new Date(d.last_seen).toLocaleString("no-NO")}` : "Ingen melding mottatt ennå";
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

// Mains routers (smart plugs / relays) that act as mesh repeaters. Reuses the
// detector row layout: online/offline LED + link quality, plus on/off and load
// if the plug reports them.
function renderMesh() {
  const el = $("mesh");
  const m = (state && state.mesh) || [];
  if (!m.length) {
    el.innerHTML = '<li class="empty">Ingen strømdrevne enheter sett ennå</li>';
    return;
  }
  el.innerHTML = m.map(meshRow).join("");
}

function meshRow(d) {
  // No report yet (just seeded from the inventory): show it as present but
  // awaiting data rather than a misleading online/offline.
  const seen = !!d.last_seen;
  const status = !seen ? "offline" : (d.online ? "ok" : "offline");
  const badge = !seen ? "…" : (d.online ? (d.state || "OK") : "FRAKOBLET");
  const sub = [d.vendor, d.model].filter(Boolean).join("  ·  ");
  const metrics = [
    d.power != null ? metric("last", `${Number(d.power).toFixed(0)} W`) : "",
    d.linkquality != null ? signalCell(d.linkquality) : "",
    metric("sett", timeAgo(d.last_seen)),
  ].filter(Boolean).join("");
  const seenTitle = d.last_seen ? `Siste melding: ${new Date(d.last_seen).toLocaleString("no-NO")}` : "Ingen melding mottatt ennå";
  return `<li class="det" data-status="${status}" title="${esc(seenTitle)}">
    <span class="led"></span>
    <div class="det-main">
      <div class="det-head">
        <span class="det-name">${esc(d.friendly_name)}</span>
        <span class="det-zone">${esc(sub)}</span>
      </div>
      <div class="det-metrics">${metrics}</div>
    </div>
    <span class="det-badge badge-${status}">${esc(badge)}</span>
  </li>`;
}

function renderUps() {
  const el = $("ups");
  const u = state.ups;
  if (!u || !u.monitored) { el.innerHTML = '<span class="empty">Ikke overvåket — ingen UPS tilkoblet</span>'; return; }
  const status = u.on_battery ? "PÅ BATTERI" : (u.online ? "på nett" : "utilgjengelig");
  const bad = u.on_battery || u.low_battery || !u.online;
  const runtime = u.runtime_s != null
    ? (u.runtime_s >= 60 ? `${Math.round(u.runtime_s / 60)} min` : `${u.runtime_s}s`)
    : null;
  el.innerHTML = `
    <div><span class="k">status </span><span class="${bad ? "ups-bad" : ""}">${status}</span></div>
    ${u.grid_voltage != null ? `<div><span class="k">nett </span>${u.grid_voltage.toFixed(0)} V</div>` : ""}
    ${u.load_pct != null ? `<div><span class="k">last </span>${u.load_pct}%</div>` : ""}
    ${u.charge_pct != null ? `<div><span class="k">lading </span>${u.charge_pct}%</div>` : ""}
    ${runtime != null ? `<div><span class="k">driftstid </span>${runtime}</div>` : ""}
    ${u.low_battery ? '<div class="ups-bad">LAVT BATTERI</div>' : ""}`;
}

function renderWan() {
  const el = $("wan");
  const w = state.wan;
  if (!w || !w.monitored || !w.adapters || !w.adapters.length) {
    el.innerHTML = '<span class="empty">Ingen data om internettlinjer</span>';
    return;
  }
  const rows = w.adapters.map((a) => {
    let cls, label;
    if (a.active) { cls = "wan-active"; label = "i bruk"; }
    else if (a.link) { cls = "wan-standby"; label = "reserve"; }
    else { cls = "wan-down"; label = "nede"; }
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
    ? '<div class="ups-bad">Ingen internett — alle linjer er nede</div>' : "";
  el.innerHTML = rows + none;
}

function renderBalance() {
  const el = $("balance");
  const b = state.balance;
  if (!b || b.credit == null) {
    el.innerHTML = '<span class="empty">Ikke sjekket</span>';
    return;
  }
  const cls = b.low ? "ups-bad" : "";
  el.innerHTML = `
    <div><span class="k">saldo </span><span class="${cls}">${b.credit.toFixed(2)}</span></div>
    ${b.low ? '<div class="ups-bad">LAV — fyll på SMS-kreditt</div>' : ""}
    ${b.last_checked ? `<div><span class="k">sjekket </span>${new Date(b.last_checked).toLocaleTimeString("no-NO")}</div>` : ""}`;
}

// ---- Watchdog heartbeat (polled separately; not part of the WS snapshot) ----
function renderWatchdog(s) {
  const el = $("watchdog");
  const pill = $("hbState");
  if (!s) { el.innerHTML = '<span class="empty">Ingen data</span>'; return; }
  if (!s.enabled) {
    pill.dataset.state = "warn"; pill.textContent = "av";
    el.innerHTML = '<span class="empty">Slått av (FW_HEARTBEAT_ENABLED=false)</span>';
    return;
  }
  // Healthy = a successful beat within ~1.5x the interval and last code 200.
  const okRecent = s.last_success_at &&
    (Date.now() - new Date(s.last_success_at).getTime()) <= s.interval * 1500;
  const healthy = !!okRecent && s.last_status_code === 200;
  pill.dataset.state = healthy ? "up" : "down";
  pill.textContent = healthy ? "ok" : "utdatert";

  const codeLine = s.last_status_code != null
    ? `${s.last_status_code}${s.last_response ? ` "${trunc(s.last_response, 24)}"` : ""}`
    : (s.last_error ? trunc(s.last_error, 36) : "—");
  const next = s.seconds_to_next_beat;
  const atTitle = s.last_success_at ? new Date(s.last_success_at).toLocaleString("no-NO") : "aldri lyktes";
  el.innerHTML = `
    <div title="${esc(atTitle)}"><span class="k">siste slag </span><span class="${healthy ? "" : "ups-bad"}">${esc(timeAgo(s.last_success_at))}</span>${s.last_success_at ? ` (${esc(new Date(s.last_success_at).toLocaleTimeString("no-NO"))})` : ""}</div>
    <div><span class="k">svar </span><span class="${s.last_status_code === 200 ? "" : "ups-bad"}">${esc(codeLine)}</span></div>
    <div><span class="k">mål </span>${esc(s.url)}</div>
    <div><span class="k">intervall </span>${esc(s.interval)} s${next != null ? `  ·  neste om ${esc(next)} s` : ""}</div>
    ${s.consecutive_failures ? `<div class="ups-bad">${esc(s.consecutive_failures)} feil på rad</div>` : ""}`;
}

async function loadWatchdog() {
  try {
    const r = await fetch("/api/watchdog/status", { cache: "no-store" });
    if (r.status === 401) { location.href = "/login"; return; }
    renderWatchdog(await r.json());
  } catch { /* keep the last render */ }
}

function sirensSounding() {
  return !!state.sirens_until && new Date(state.sirens_until).getTime() > Date.now();
}

function renderCountdown() {
  const el = $("countdown");
  if (countdownTimer) { clearInterval(countdownTimer); countdownTimer = null; }
  if (sirensSounding()) {
    el.hidden = false;
    const tick = () => {
      const ms = new Date(state.sirens_until).getTime() - Date.now();
      if (ms <= 0) { render(); return; }  // timer ran out: drop the SIRENS banner
      const m = Math.floor(ms / 60000), s = Math.floor((ms % 60000) / 1000);
      el.textContent = `brannsirenene stopper om ${m}m ${String(s).padStart(2, "0")}s`;
    };
    tick();
    countdownTimer = setInterval(tick, 1000);
    return;
  }
  if (state.mode !== "event" || !state.event_until) { el.hidden = true; return; }
  el.hidden = false;
  const tick = () => {
    const ms = new Date(state.event_until).getTime() - Date.now();
    if (ms <= 0) { el.textContent = "går tilbake til normal…"; return; }
    const m = Math.floor(ms / 60000), s = Math.floor((ms % 60000) / 1000);
    el.textContent = `tilbake til normal om ${m}m ${String(s).padStart(2, "0")}s`;
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
  const t = new Date(r.ts).toLocaleTimeString("no-NO");
  const text = summarize(r);
  return `<li><span class="a-ts">${t}</span><span class="${cls}">${esc(text)}</span></li>`;
}

function summarize(r) {
  const d = r.detail || {};
  switch (r.kind) {
    case "alarm": return `BRANN ${d.detector}${d.event_mode ? " (eventmodus: sirenelink avslått)" : d.silenced ? " (dempet)" : ""}`;
    case "alarm_replay": return `gammel alarm ignorert: ${d.detector} (siste melding ${new Date(d.last_seen).toLocaleString("no-NO")})`;
    case "mode_change": return `modus ${MODE_LABELS[d.from] || d.from} → ${MODE_LABELS[d.to] || d.to}`;
    case "sms": return `SMS ${d.policy}: ${trunc(d.text)}`;
    case "dlr": return `Leveringskvittering ${d.message_id}: ${d.status}`;
    case "sms_unrouted": return `IKKE SENDT (ingen mottakere for ${d.category}): ${trunc(d.text)}`;
    case "escalation": return `ESKALERT til ${(d.recipients || []).join(", ")}`;
    case "siren_interconnect": return `brannsirener startet (utløst av ${d.origin})`;
    case "siren_silence": return "brannsirener dempet";
    case "siren_test": return `sirenetest ${d.action === "on" ? "PÅ" : "AV"} (${d.sirens ?? "?"} sirener)`;
    case "fault": return `feil: ${trunc(JSON.stringify(d))}`;
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

async function silenceSirens() {
  if (!confirm("Demp alle brannsirener nå? Gjør dette bare når lokalet er bekreftet trygt.")) return;
  await fetch("/api/siren/silence", { method: "POST" });
}
$("silenceBtn").addEventListener("click", silenceSirens);
$("bannerSilenceBtn").addEventListener("click", silenceSirens);

document.querySelectorAll(".seg-btn").forEach((b) =>
  b.addEventListener("click", async () => {
    await fetch("/api/sms/policy", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ policy: b.dataset.policy, actor: "dashboard" }),
    });
  })
);

$("hbTestBtn").addEventListener("click", async () => {
  const btn = $("hbTestBtn"), msg = $("hbMsg");
  btn.disabled = true;
  msg.hidden = false; msg.className = "hb-msg"; msg.textContent = "Sender…";
  try {
    const r = await fetch("/api/watchdog/test", { method: "POST" });
    const s = await r.json();
    const ok = s.last_status_code === 200 && (s.last_response || "").trim() === "ok";
    msg.className = "hb-msg " + (ok ? "ok" : "err");
    msg.textContent = ok
      ? `OK — 200 "${trunc(s.last_response, 24)}"`
      : `Feilet — ${s.last_status_code != null ? s.last_status_code + " " : ""}${trunc(s.last_error || s.last_response || "ingen svar", 48)}`;
    renderWatchdog(s);
  } catch (e) {
    msg.className = "hb-msg err"; msg.textContent = "Forespørselen feilet: " + e;
  } finally {
    btn.disabled = false;
  }
});

// Watchdog status polls on its own ~15s cadence, separate from the WS snapshot.
loadWatchdog();
setInterval(loadWatchdog, 15000);

// Uptime ticks every second off the last snapshot's started_at.
setInterval(tickUptime, 1000);

// (The 🐝 Zigbee2MQTT link href is set inline in index.html so it survives a
// cached app.js.)

// ---- utils ----
function esc(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function trunc(s, n = 48) { s = String(s); return s.length > n ? s.slice(0, n) + "…" : s; }

connect();
