"""FastAPI application — wiring, HTTP control endpoints, and the live WebSocket.

Background tasks started at lifespan:
  - MQTT bridge (Z2M -> state machine)
  - UPS poller (NUT)
  - tick loop (EVENT auto-expiry + detector supervision + escalation)
  - balance check loop (GatewayAPI credit monitoring)
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from .config import Detector, get_settings, load_yaml_config
from .db import Database
from .models import BalanceState, Mode, SmsPolicy, SystemSnapshot, UpsState, now
from .mqtt_client import DeviceRegistry, MqttBridge
from .notify import Notifier
from .state import StateMachine
from .ups import run_ups_poller

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("firewatch")

settings = get_settings()
cfg = load_yaml_config()
db = Database(settings.db_path)
notifier = Notifier(settings)
machine = StateMachine(settings, cfg, db, notifier)

# Settings persisted in the kv table and editable from the dashboard.
_PW_HASH_KEY = "auth.password_hash"
_GATEWAYAPI_TOKEN_KEY = "secret.gatewayapi_token"
_DETECTORS_KEY = "config.detectors"
_UPS_NAME_KEY = "config.ups_name"
_UPS_POLL_KEY = "config.ups_poll_seconds"

# Devices discovered from Z2M's bridge/devices topic; shared with the bridge so
# the detector setup page can list what is actually paired.
registry = DeviceRegistry()

# Password hash loaded at startup; None means auth is not yet configured, which
# puts the dashboard into first-run setup mode (prompt to choose a password).
_auth_pw_hash: str | None = None
_active_tokens: dict[str, datetime] = {}

_PBKDF2_ROUNDS = 200_000


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return f"{salt.hex()}${dk.hex()}"


def _check_password(password: str, stored: str) -> bool:
    try:
        salt_hex, dk_hex = stored.split("$", 1)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), _PBKDF2_ROUNDS)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), dk_hex)


def _auth_configured() -> bool:
    return _auth_pw_hash is not None


def _verify_token(token: str | None) -> bool:
    if not _auth_configured():
        return True
    if not token:
        return False
    expiry = _active_tokens.get(token)
    if not expiry:
        return False
    if expiry <= now():
        _active_tokens.pop(token, None)
        return False
    return True


def _issue_token() -> str:
    token = secrets.token_urlsafe(32)
    _active_tokens[token] = now() + timedelta(hours=settings.auth_token_ttl_hours)
    return token


class _Clients:
    """Tracks connected dashboards and pushes snapshots to all of them."""

    def __init__(self) -> None:
        self._sockets: set[WebSocket] = set()

    def add(self, ws: WebSocket) -> None:
        self._sockets.add(ws)

    def remove(self, ws: WebSocket) -> None:
        self._sockets.discard(ws)

    async def push(self, snap: SystemSnapshot) -> None:
        dead = []
        data = snap.model_dump(mode="json")
        for ws in self._sockets:
            try:
                await ws.send_json({"type": "snapshot", "data": data})
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._sockets.discard(ws)


clients = _Clients()


async def _tick_loop() -> None:
    while True:
        await machine.tick()
        await machine.check_escalations()
        await asyncio.sleep(5)


async def _balance_loop() -> None:
    gw = notifier._primary
    interval = settings.balance_check_interval_minutes * 60
    while True:
        # Re-check each pass: the token may be added at runtime via /settings.
        if gw.configured:
            credit = await gw.check_balance()
            if credit is not None:
                low = credit < settings.balance_warn_threshold
                machine.balance = BalanceState(credit=credit, low=low, last_checked=now())
                if low:
                    log.warning("GatewayAPI balance low: %.2f", credit)
                    await db.audit("system", {"action": "balance_low", "credit": credit}, severity="warning", actor="balance")
        await asyncio.sleep(interval if gw.configured else 60)


async def _load_persisted_settings() -> None:
    """Pull dashboard-managed settings out of the kv store on startup.

    Precedence: a value saved from the dashboard overrides the matching env var.
    If only the env password is set (legacy), migrate it into a stored hash so
    the dashboard becomes the single source of truth from here on.
    """
    global _auth_pw_hash

    token = await db.get(_GATEWAYAPI_TOKEN_KEY)
    if token:
        settings.gatewayapi_token = token

    # UPS: the kv store is authoritative once set from the dashboard. Use
    # "key present" (not truthiness) so a deliberately-cleared name overrides
    # any FW_UPS_NAME env value across restarts.
    ups_name = await db.get(_UPS_NAME_KEY)
    if ups_name is not None:
        settings.ups_name = ups_name or None
    ups_poll = await db.get(_UPS_POLL_KEY)
    if ups_poll and ups_poll.isdigit():
        settings.ups_poll_seconds = int(ups_poll)

    _auth_pw_hash = await db.get(_PW_HASH_KEY)
    if _auth_pw_hash is None and settings.auth_password:
        _auth_pw_hash = _hash_password(settings.auth_password)
        await db.set(_PW_HASH_KEY, _auth_pw_hash)
        log.info("migrated env FW_AUTH_PASSWORD into the settings store")

    # Detectors: the DB store is authoritative once it exists. On first boot,
    # seed it from config.yaml so existing deployments carry over and the
    # dashboard becomes the single place to edit them from then on.
    raw_detectors = await db.get(_DETECTORS_KEY)
    if raw_detectors:
        try:
            detectors = [Detector(**d) for d in json.loads(raw_detectors)]
            await machine.set_detectors(detectors)
            log.info("loaded %d detector(s) from the settings store", len(detectors))
        except (ValueError, ValidationError):
            log.exception("stored detectors are corrupt; keeping config.yaml set")
    elif cfg.detectors:
        await db.set(_DETECTORS_KEY, json.dumps([d.model_dump() for d in cfg.detectors]))
        log.info("seeded detector store from config.yaml (%d detectors)", len(cfg.detectors))


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    await db.init()
    await _load_persisted_settings()
    await machine.restore()
    machine.add_listener(clients.push)

    bridge = MqttBridge(settings, cfg, machine, registry)
    tasks = [
        asyncio.create_task(bridge.run(), name="mqtt"),
        asyncio.create_task(run_ups_poller(settings, machine), name="ups"),
        asyncio.create_task(_tick_loop(), name="tick"),
        asyncio.create_task(_balance_loop(), name="balance"),
    ]
    log.info("FireWatch started (%d detectors, %d recipients, auth=%s)",
             len(cfg.detectors), len(cfg.recipients),
             "on" if _auth_configured() else "setup-required")
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="Venue FireWatch", version="0.2.0", lifespan=lifespan)


# ---- Auth middleware -----------------------------------------------------

# Paths reachable without a session: the login/setup surface plus the two
# inbound webhooks (GatewayAPI delivery receipts and recipient SMS acks).
_OPEN_PATHS = {"/api/login", "/api/setup", "/login", "/api/sms/dlr", "/api/sms/ack"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in _OPEN_PATHS or path.endswith((".css", ".js", ".ico", ".png", ".svg")):
        return await call_next(request)

    # Until a password exists, force everything to the setup page.
    if not _auth_configured():
        if path.startswith("/api/") or path == "/ws":
            return JSONResponse({"error": "setup_required"}, status_code=401)
        return RedirectResponse("/login")

    token = request.cookies.get("fw_token") or request.query_params.get("token")
    if not token:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

    if _verify_token(token):
        return await call_next(request)

    if path.startswith("/api/") or path == "/ws":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return RedirectResponse("/login")


# ---- API models ----------------------------------------------------------

class ArmEventRequest(BaseModel):
    until: datetime
    actor: str = "dashboard"


class PolicyRequest(BaseModel):
    policy: SmsPolicy
    actor: str = "dashboard"


class LoginRequest(BaseModel):
    password: str


class AckRequest(BaseModel):
    msisdn: str
    alert_key: str


class PasswordChangeRequest(BaseModel):
    current: str
    new: str


class GatewayApiTokenRequest(BaseModel):
    token: str = ""  # empty clears the token (disables GatewayAPI)


class UpsSettingsRequest(BaseModel):
    ups_name: str = ""              # NUT name e.g. "myups@localhost"; empty disables
    poll_seconds: int | None = None  # optional; unchanged if omitted


# ---- Auth endpoints ------------------------------------------------------

def _login_response(ok_payload: dict) -> JSONResponse:
    token = _issue_token()
    resp = JSONResponse(ok_payload)
    resp.set_cookie(
        "fw_token", token, httponly=True, samesite="strict",
        max_age=settings.auth_token_ttl_hours * 3600,
    )
    return resp


@app.post("/api/login")
async def login(req: LoginRequest) -> JSONResponse:
    if not _auth_configured():
        return JSONResponse({"error": "setup_required"}, status_code=409)
    if not _check_password(req.password, _auth_pw_hash):
        return JSONResponse({"error": "wrong password"}, status_code=401)
    return _login_response({"ok": True})


@app.post("/api/setup")
async def setup(req: LoginRequest) -> JSONResponse:
    """First-run: choose the dashboard password. Closed once one exists."""
    global _auth_pw_hash
    if _auth_configured():
        return JSONResponse({"error": "already configured"}, status_code=409)
    if len(req.password) < 8:
        return JSONResponse({"error": "password must be at least 8 characters"}, status_code=400)
    _auth_pw_hash = _hash_password(req.password)
    await db.set(_PW_HASH_KEY, _auth_pw_hash)
    await db.audit("system", {"action": "auth_configured"}, severity="info", actor="setup")
    log.info("dashboard password configured via first-run setup")
    return _login_response({"ok": True})


_LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FireWatch — {title}</title>
<link rel="stylesheet" href="/style.css">
<style>
.login{{max-width:340px;margin:15vh auto;padding:28px;background:var(--panel);border:1px solid var(--hairline);border-radius:14px}}
.login h1{{font-size:1.1rem;margin:0 0 6px;color:var(--live)}}
.login p.hint{{font-size:.82rem;color:var(--muted,#9aa);margin:0 0 18px}}
.login input{{width:100%;padding:10px;margin-bottom:12px;background:var(--raised);border:1px solid var(--hairline);color:var(--text);border-radius:8px;font-family:var(--mono);font-size:.9rem}}
.login button{{width:100%;padding:10px;background:color-mix(in srgb,var(--live) 22%,var(--raised));border:1px solid color-mix(in srgb,var(--live) 50%,transparent);color:var(--text);border-radius:8px;font-weight:600;cursor:pointer;font-size:.9rem}}
.login .err{{color:var(--alarm);font-size:.82rem;margin-top:8px;display:none}}
</style></head><body>
<div class="login"><h1>FireWatch</h1>
<p class="hint">{hint}</p>
<form id="f"><input type="password" id="pw" placeholder="{placeholder}" autofocus>
{confirm}
<button type="submit">{button}</button><div class="err" id="err"></div></form></div>
<script>
const SETUP={setup_flag};
document.getElementById("f").onsubmit=async e=>{{e.preventDefault();
const err=document.getElementById("err");err.style.display="none";
const pw=document.getElementById("pw").value;
if(SETUP){{const c=document.getElementById("pw2").value;
  if(pw.length<8){{err.textContent="Password must be at least 8 characters";err.style.display="block";return}}
  if(pw!==c){{err.textContent="Passwords do not match";err.style.display="block";return}}}}
const r=await fetch(SETUP?"/api/setup":"/api/login",{{method:"POST",
  headers:{{"Content-Type":"application/json"}},body:JSON.stringify({{password:pw}})}});
if(r.ok){{location.href="/"}}else{{const b=await r.json().catch(()=>({{}}));
  err.textContent=b.error||"Wrong password";err.style.display="block"}}}};
</script></body></html>"""


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    if _auth_configured():
        return _LOGIN_PAGE.format(
            title="Login", hint="Enter the dashboard password.",
            placeholder="Password", confirm="", button="Log in", setup_flag="false",
        )
    return _LOGIN_PAGE.format(
        title="Set up", hint="First run — choose a dashboard password (min 8 characters).",
        placeholder="New password",
        confirm='<input type="password" id="pw2" placeholder="Confirm password">',
        button="Set password", setup_flag="true",
    )


# ---- REST endpoints ------------------------------------------------------

@app.get("/api/state")
async def get_state() -> JSONResponse:
    return JSONResponse(machine.snapshot().model_dump(mode="json"))


@app.get("/api/audit")
async def get_audit(limit: int = 100) -> JSONResponse:
    return JSONResponse(await db.recent_audit(limit))


@app.post("/api/event/arm")
async def arm_event(req: ArmEventRequest) -> JSONResponse:
    effective = await machine.arm_event(req.until, actor=req.actor)
    return JSONResponse({"mode": Mode.EVENT.value, "event_until": effective.isoformat()})


@app.post("/api/event/end")
async def end_event(actor: str = "dashboard") -> JSONResponse:
    await machine.end_event(actor=actor)
    return JSONResponse({"mode": Mode.NORMAL.value})


@app.post("/api/sms/policy")
async def set_policy(req: PolicyRequest) -> JSONResponse:
    await machine.set_policy(req.policy, actor=req.actor)
    return JSONResponse({"sms_policy": req.policy.value})


# ---- DLR webhook (GatewayAPI posts delivery receipts here) ---------------

@app.post("/api/sms/dlr")
async def sms_dlr(request: Request) -> JSONResponse:
    """GatewayAPI delivery receipt webhook. Configure the callback URL in your
    GatewayAPI dashboard as: https://<your-host>/api/sms/dlr"""
    try:
        body = await request.json()
    except Exception:
        body = dict(request.query_params)

    msg_id = str(body.get("id", body.get("message_id", "")))
    status = body.get("status", body.get("dlr_status", "unknown"))

    if msg_id:
        found = await db.update_dlr(msg_id, status)
        await db.audit(
            "dlr",
            {"message_id": msg_id, "status": status, "matched": found},
            actor="gatewayapi",
        )
        log.info("DLR: msg_id=%s status=%s matched=%s", msg_id, status, found)
    return JSONResponse({"ok": True})


# ---- Acknowledgment endpoint (recipient confirms they saw the alert) -----

@app.post("/api/sms/ack")
async def ack_alert(req: AckRequest) -> JSONResponse:
    found = await machine.ack_alert(req.msisdn, req.alert_key)
    return JSONResponse({"ok": found})


# ---- Balance endpoint ----------------------------------------------------

@app.get("/api/balance")
async def get_balance() -> JSONResponse:
    return JSONResponse(machine.balance.model_dump(mode="json") if machine.balance else {})


# ---- Settings (dashboard-managed config) ---------------------------------

def _mask_token(token: str | None) -> str:
    if not token:
        return ""
    return f"…{token[-4:]}" if len(token) > 4 else "…"


@app.get("/api/settings")
async def get_settings_status() -> JSONResponse:
    return JSONResponse({
        "auth_configured": _auth_configured(),
        "gatewayapi": {
            "configured": bool(settings.gatewayapi_token),
            "masked": _mask_token(settings.gatewayapi_token),
            "sender": settings.gatewayapi_sender,
        },
        "ups": {
            "name": settings.ups_name or "",
            "poll_seconds": settings.ups_poll_seconds,
            "monitored": bool(machine.ups and machine.ups.monitored),
            "status": (machine.ups.raw_status if machine.ups else None),
            "online": bool(machine.ups and machine.ups.online),
        },
    })


@app.post("/api/settings/password")
async def change_password(req: PasswordChangeRequest) -> JSONResponse:
    global _auth_pw_hash
    if not _check_password(req.current, _auth_pw_hash):
        return JSONResponse({"error": "current password is incorrect"}, status_code=401)
    if len(req.new) < 8:
        return JSONResponse({"error": "password must be at least 8 characters"}, status_code=400)
    _auth_pw_hash = _hash_password(req.new)
    await db.set(_PW_HASH_KEY, _auth_pw_hash)
    await db.audit("system", {"action": "password_changed"}, severity="info", actor="settings")
    # Invalidate every existing session, then re-issue one for this caller.
    _active_tokens.clear()
    return _login_response({"ok": True})


@app.post("/api/settings/gatewayapi")
async def set_gatewayapi_token(req: GatewayApiTokenRequest) -> JSONResponse:
    token = req.token.strip()
    if token:
        await db.set(_GATEWAYAPI_TOKEN_KEY, token)
        settings.gatewayapi_token = token
        action = "gatewayapi_token_set"
    else:
        await db.set(_GATEWAYAPI_TOKEN_KEY, "")
        settings.gatewayapi_token = None
        action = "gatewayapi_token_cleared"
    await db.audit("system", {"action": action}, severity="info", actor="settings")
    log.info("GatewayAPI token updated via settings (configured=%s)", bool(token))
    return JSONResponse({"ok": True, "configured": bool(token), "masked": _mask_token(token or None)})


@app.post("/api/settings/ups")
async def set_ups(req: UpsSettingsRequest) -> JSONResponse:
    if req.poll_seconds is not None:
        if req.poll_seconds < 5:
            return JSONResponse({"error": "poll interval must be at least 5 seconds"}, status_code=400)
        settings.ups_poll_seconds = req.poll_seconds
        await db.set(_UPS_POLL_KEY, str(req.poll_seconds))

    name = req.ups_name.strip()
    if name:
        settings.ups_name = name
        await db.set(_UPS_NAME_KEY, name)
        action = "ups_configured"
    else:
        settings.ups_name = None
        await db.set(_UPS_NAME_KEY, "")
        await machine.clear_ups()  # poller stops; reset the dashboard display
        action = "ups_cleared"
    await db.audit("system", {"action": action, "ups_name": name or None}, severity="info", actor="settings")
    log.info("UPS settings updated (name=%s, poll=%ss)", name or "(none)", settings.ups_poll_seconds)
    return JSONResponse({"ok": True, "name": name, "poll_seconds": settings.ups_poll_seconds})


# ---- Detectors (dashboard-managed fire/heat alarm config) ----------------

_DETECTORS_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FireWatch — Detectors</title>
<link rel="stylesheet" href="/style.css">
<style>
.wrap{max-width:920px;margin:5vh auto;padding:0 16px}
.wrap h1{font-size:1.2rem;color:var(--live);margin:0 0 4px}
.wrap a.back{font-size:.82rem;color:var(--muted,#9aa);text-decoration:none}
.card{margin-top:20px;padding:20px;background:var(--panel);border:1px solid var(--hairline);border-radius:14px}
.card h2{font-size:1rem;margin:0 0 4px}
.card p.hint{font-size:.8rem;color:var(--muted,#9aa);margin:0 0 14px}
table{width:100%;border-collapse:collapse;font-size:.84rem}
th{text-align:left;font-weight:600;color:var(--muted,#9aa);font-size:.72rem;text-transform:uppercase;letter-spacing:.04em;padding:6px 6px}
td{padding:4px 6px;vertical-align:middle}
tr+tr td{border-top:1px solid var(--hairline)}
input,select{width:100%;padding:7px;background:var(--raised);border:1px solid var(--hairline);color:var(--text);border-radius:7px;font-family:var(--mono);font-size:.82rem;box-sizing:border-box}
td.num input{width:64px}
button{cursor:pointer;font-family:inherit}
.iconbtn{background:transparent;border:1px solid var(--hairline);color:var(--alarm);border-radius:7px;padding:6px 9px;font-size:.8rem}
.addbtn{margin-top:12px;padding:8px 14px;background:var(--raised);border:1px solid var(--hairline);color:var(--text);border-radius:8px;font-size:.84rem}
.savebtn{margin-top:16px;padding:10px 18px;background:color-mix(in srgb,var(--live) 22%,var(--raised));border:1px solid color-mix(in srgb,var(--live) 50%,transparent);color:var(--text);border-radius:8px;font-weight:600;font-size:.9rem}
.msg{font-size:.82rem;margin-top:10px;display:none}
.msg.ok{color:var(--live)} .msg.err{color:var(--alarm)}
.disc{display:flex;flex-wrap:wrap;gap:8px}
.dev{flex:1 1 240px;padding:10px 12px;background:var(--raised);border:1px solid var(--hairline);border-radius:10px;display:flex;justify-content:space-between;align-items:center;gap:8px}
.dev .meta{font-size:.7rem;color:var(--muted,#9aa)}
.dev .nm{font-family:var(--mono);font-size:.84rem}
.dev .tag{font-size:.66rem;text-transform:uppercase;letter-spacing:.04em;color:var(--live)}
.dev button{padding:6px 10px;background:transparent;border:1px solid color-mix(in srgb,var(--live) 50%,transparent);color:var(--text);border-radius:7px;font-size:.78rem}
.dev button:disabled{opacity:.45;cursor:default;border-color:var(--hairline)}
.empty{color:var(--muted,#9aa);font-size:.82rem}
</style></head><body>
<div class="wrap">
<a class="back" href="/">← back to dashboard</a>
<h1>Detectors</h1>

<div class="card">
<h2>Discovered devices</h2>
<p class="hint">Paired Zigbee devices reported by Zigbee2MQTT. Alarm-capable ones are listed first; “Add” pre-fills a row below.</p>
<div class="disc" id="disc"><span class="empty">Loading…</span></div>
</div>

<div class="card">
<h2>Monitored detectors</h2>
<p class="hint">Each <code>friendly_name</code> must match Zigbee2MQTT exactly. <code>alarm_property</code> is the boolean field that goes true on detection (e.g. “smoke”, “heat”). EVENT mode silences only the zones you list as silent.</p>
<table><thead><tr>
<th>Label</th><th>Friendly name</th><th>Kind</th><th>Alarm prop</th><th>Zone</th><th title="Hours without a check-in before it is treated as a fault">Offline h</th><th></th>
</tr></thead><tbody id="rows"></tbody></table>
<button class="addbtn" id="addBtn">+ Add detector</button>
<div><button class="savebtn" id="saveBtn">Save detectors</button></div>
<p class="msg" id="msg"></p>
</div>
</div>
<script>
const KINDS=["smoke","heat","gas","other"];
const rows=document.getElementById("rows");
function cell(cls,inner){const td=document.createElement("td");if(cls)td.className=cls;td.appendChild(inner);return td}
function inp(val,ph){const i=document.createElement("input");i.value=val??"";if(ph)i.placeholder=ph;return i}
function addRow(d){d=d||{};const tr=document.createElement("tr");
  const label=inp(d.label,"Stage left"),fn=inp(d.friendly_name,"heat_stage_left");
  const kind=document.createElement("select");for(const k of KINDS){const o=document.createElement("option");o.value=o.textContent=k;if((d.kind||"heat")===k)o.selected=true;kind.appendChild(o)}
  const prop=inp(d.alarm_property||"heat"),zone=inp(d.zone||"default");
  const off=inp(d.offline_after_hours??6);off.type="number";off.step="0.5";off.min="0";
  tr.append(cell("",label),cell("",fn),cell("",kind),cell("",prop),cell("",zone),cell("num",off));
  const del=document.createElement("button");del.className="iconbtn";del.textContent="✕";del.title="Remove";del.onclick=()=>tr.remove();
  tr.append(cell("",del));
  tr._get=()=>({label:label.value.trim(),friendly_name:fn.value.trim(),kind:kind.value,
    alarm_property:prop.value.trim(),zone:zone.value.trim()||"default",
    offline_after_hours:parseFloat(off.value)||6});
  rows.appendChild(tr);return tr}
function show(ok,text){const m=document.getElementById("msg");m.textContent=text;m.className="msg "+(ok?"ok":"err");m.style.display="block"}
document.getElementById("addBtn").onclick=()=>addRow();
document.getElementById("saveBtn").onclick=async()=>{
  const data=[...rows.children].map(tr=>tr._get());
  const r=await fetch("/api/detectors",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)});
  if(r.ok){show(true,"Saved "+data.length+" detector(s). Now monitored live — no restart needed.");loadDevices()}
  else{const b=await r.json().catch(()=>({}));show(false,b.error||"Failed to save")}};
async function loadDetectors(){const r=await fetch("/api/detectors");const d=await r.json();
  rows.innerHTML="";(d.length?d:[{}]).forEach(addRow)}
function configuredNames(){return new Set([...rows.children].map(tr=>tr._get().friendly_name))}
async function loadDevices(){const el=document.getElementById("disc");
  const r=await fetch("/api/devices");const d=await r.json();
  if(!d.devices.length){el.innerHTML='<span class="empty">No devices reported yet — Z2M must be running and publishing bridge/devices.</span>';return}
  const have=configuredNames();el.innerHTML="";
  for(const dev of d.devices){const box=document.createElement("div");box.className="dev";
    const left=document.createElement("div");
    const nm=document.createElement("div");nm.className="nm";nm.textContent=dev.friendly_name;
    const meta=document.createElement("div");meta.className="meta";
    meta.textContent=[dev.vendor,dev.model].filter(Boolean).join(" ")||"unknown";
    left.append(nm,meta);
    if(dev.is_alarm_device){const t=document.createElement("div");t.className="tag";t.textContent=dev.alarm_properties.join(" / ");left.append(t)}
    box.append(left);
    const btn=document.createElement("button");
    if(have.has(dev.friendly_name)||dev.configured){btn.textContent="added";btn.disabled=true}
    else{btn.textContent="Add";btn.onclick=()=>{addRow({friendly_name:dev.friendly_name,
      label:dev.description||dev.friendly_name,kind:dev.suggested_kind||"heat",
      alarm_property:dev.suggested_alarm_property||"heat"});btn.textContent="added";btn.disabled=true}}
    box.append(btn);el.append(box)}}
loadDetectors().then(loadDevices);
</script></body></html>"""


@app.get("/api/detectors")
async def get_detectors() -> JSONResponse:
    return JSONResponse([d.model_dump() for d in cfg.detectors])


@app.put("/api/detectors")
async def put_detectors(detectors: list[Detector]) -> JSONResponse:
    """Replace the full detector set. The dashboard saves the whole table."""
    names = [d.friendly_name.strip() for d in detectors]
    if any(not n for n in names):
        return JSONResponse({"error": "every detector needs a friendly_name"}, status_code=400)
    if len(set(names)) != len(names):
        return JSONResponse({"error": "duplicate friendly_name"}, status_code=400)
    if any(not d.alarm_property.strip() for d in detectors):
        return JSONResponse({"error": "every detector needs an alarm_property"}, status_code=400)

    await db.set(_DETECTORS_KEY, json.dumps([d.model_dump() for d in detectors]))
    await machine.set_detectors(detectors)
    await db.audit(
        "system",
        {"action": "detectors_updated", "count": len(detectors), "names": names},
        severity="info",
        actor="settings",
    )
    log.info("detector set updated via dashboard (%d detectors)", len(detectors))
    return JSONResponse([d.model_dump() for d in detectors])


@app.get("/api/devices")
async def get_devices() -> JSONResponse:
    """Devices discovered from Z2M, annotated with whether each is already
    monitored. Empty until Z2M publishes its retained bridge/devices topic."""
    configured = {d.friendly_name for d in cfg.detectors}
    devices = registry.devices()
    for d in devices:
        d["configured"] = d["friendly_name"] in configured
    return JSONResponse({
        "last_seen": registry.last_seen.isoformat() if registry.last_seen else None,
        "devices": devices,
    })


@app.get("/detectors", response_class=HTMLResponse)
async def detectors_page():
    return _DETECTORS_PAGE


@app.get("/settings", response_class=HTMLResponse)
async def settings_page():
    return """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FireWatch — Settings</title>
<link rel="stylesheet" href="/style.css">
<style>
.wrap{max-width:520px;margin:6vh auto;padding:0 16px}
.wrap h1{font-size:1.2rem;color:var(--live);margin:0 0 4px}
.wrap a.back{font-size:.82rem;color:var(--muted,#9aa);text-decoration:none}
.card{margin-top:20px;padding:22px;background:var(--panel);border:1px solid var(--hairline);border-radius:14px}
.card h2{font-size:1rem;margin:0 0 14px}
.card label{display:block;font-size:.8rem;color:var(--muted,#9aa);margin:10px 0 4px}
.card input{width:100%;padding:10px;background:var(--raised);border:1px solid var(--hairline);color:var(--text);border-radius:8px;font-family:var(--mono);font-size:.9rem;box-sizing:border-box}
.card button{margin-top:14px;padding:10px 16px;background:color-mix(in srgb,var(--live) 22%,var(--raised));border:1px solid color-mix(in srgb,var(--live) 50%,transparent);color:var(--text);border-radius:8px;font-weight:600;cursor:pointer;font-size:.9rem}
.status{font-size:.82rem;color:var(--muted,#9aa);margin:0}
.msg{font-size:.82rem;margin-top:10px;display:none}
.msg.ok{color:var(--live)} .msg.err{color:var(--alarm)}
</style></head><body>
<div class="wrap">
<a class="back" href="/">← back to dashboard</a>
<h1>Settings</h1>

<div class="card">
<h2>Dashboard password</h2>
<label for="cur">Current password</label><input type="password" id="cur">
<label for="np">New password</label><input type="password" id="np">
<label for="np2">Confirm new password</label><input type="password" id="np2">
<button id="pwBtn">Update password</button>
<p class="msg" id="pwMsg"></p>
</div>

<div class="card">
<h2>GatewayAPI (SMS)</h2>
<p class="status" id="gwStatus">Loading…</p>
<label for="tok">API token</label><input type="password" id="tok" placeholder="paste token, or leave blank to clear">
<button id="gwBtn">Save token</button>
<p class="msg" id="gwMsg"></p>
</div>

<div class="card">
<h2>UPS (NUT)</h2>
<p class="status" id="upsStatus">Loading…</p>
<label for="upsName">UPS name</label><input type="text" id="upsName" placeholder="e.g. myups@localhost — blank to disable">
<label for="upsPoll">Poll interval (seconds)</label><input type="number" id="upsPoll" min="5" step="5">
<button id="upsBtn">Save UPS</button>
<p class="msg" id="upsMsg"></p>
</div>
</div>
<script>
function show(el,ok,text){el.textContent=text;el.className="msg "+(ok?"ok":"err");el.style.display="block"}
async function refresh(){const r=await fetch("/api/settings");const d=await r.json();
  const g=d.gatewayapi;document.getElementById("gwStatus").textContent=
    g.configured?("Configured (token "+g.masked+"), sender “"+g.sender+"”"):("Not configured — SMS via GatewayAPI is disabled. Sender “"+g.sender+"”.");
  const u=d.ups;const us=document.getElementById("upsStatus");
  if(!u.name){us.textContent="Not configured — no UPS monitored."}
  else{us.textContent="Monitoring “"+u.name+"” — "+(u.monitored?(u.online?("reachable"+(u.status?" ("+u.status+")"):"")):"not reachable yet")+", every "+u.poll_seconds+"s."}
  if(document.activeElement!==document.getElementById("upsName"))document.getElementById("upsName").value=u.name||"";
  if(document.activeElement!==document.getElementById("upsPoll"))document.getElementById("upsPoll").value=u.poll_seconds;}
document.getElementById("pwBtn").onclick=async()=>{const m=document.getElementById("pwMsg");
  const cur=document.getElementById("cur").value,np=document.getElementById("np").value,np2=document.getElementById("np2").value;
  if(np.length<8){show(m,false,"New password must be at least 8 characters");return}
  if(np!==np2){show(m,false,"New passwords do not match");return}
  const r=await fetch("/api/settings/password",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({current:cur,new:np})});
  if(r.ok){show(m,true,"Password updated. Other sessions were signed out.");
    document.getElementById("cur").value="";document.getElementById("np").value="";document.getElementById("np2").value="";}
  else{const b=await r.json().catch(()=>({}));show(m,false,b.error||"Failed")}};
document.getElementById("gwBtn").onclick=async()=>{const m=document.getElementById("gwMsg");
  const tok=document.getElementById("tok").value;
  const r=await fetch("/api/settings/gatewayapi",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({token:tok})});
  if(r.ok){show(m,true,tok.trim()?"Token saved.":"Token cleared.");document.getElementById("tok").value="";refresh();}
  else{const b=await r.json().catch(()=>({}));show(m,false,b.error||"Failed")}};
document.getElementById("upsBtn").onclick=async()=>{const m=document.getElementById("upsMsg");
  const name=document.getElementById("upsName").value.trim();
  const poll=parseInt(document.getElementById("upsPoll").value,10);
  const body={ups_name:name};if(!isNaN(poll))body.poll_seconds=poll;
  const r=await fetch("/api/settings/ups",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  if(r.ok){show(m,true,name?"Saved — polling “"+name+"”.":"UPS monitoring disabled.");refresh();}
  else{const b=await r.json().catch(()=>({}));show(m,false,b.error||"Failed")}};
refresh();
</script></body></html>"""


# ---- WebSocket -----------------------------------------------------------

@app.websocket("/ws")
async def ws(socket: WebSocket, token: str | None = Query(default=None)) -> None:
    if _auth_configured():
        cookie_token = socket.cookies.get("fw_token")
        effective = token or cookie_token
        if not _verify_token(effective):
            await socket.close(code=4001, reason="unauthorized")
            return
    await socket.accept()
    clients.add(socket)
    await socket.send_json({"type": "snapshot", "data": machine.snapshot().model_dump(mode="json")})
    try:
        while True:
            await socket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        clients.remove(socket)


# ---- static frontend (mounted last so /api and /ws take precedence) ------
_web_dir = Path(__file__).parent / "web"
app.mount("/", StaticFiles(directory=str(_web_dir), html=True), name="web")
