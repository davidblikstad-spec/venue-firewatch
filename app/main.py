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
import logging
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import get_settings, load_yaml_config
from .db import Database
from .models import BalanceState, Mode, SmsPolicy, SystemSnapshot, now
from .mqtt_client import MqttBridge
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

_auth_enabled = bool(settings.auth_password)
_active_tokens: dict[str, datetime] = {}


def _verify_token(token: str | None) -> bool:
    if not _auth_enabled:
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
    if not gw.configured:
        log.info("balance check disabled (no GatewayAPI token)")
        return
    interval = settings.balance_check_interval_minutes * 60
    while True:
        credit = await gw.check_balance()
        if credit is not None:
            low = credit < settings.balance_warn_threshold
            machine.balance = BalanceState(credit=credit, low=low, last_checked=now())
            if low:
                log.warning("GatewayAPI balance low: %.2f", credit)
                await db.audit("system", {"action": "balance_low", "credit": credit}, severity="warning", actor="balance")
        await asyncio.sleep(interval)


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    await db.init()
    await machine.restore()
    machine.add_listener(clients.push)

    bridge = MqttBridge(settings, cfg, machine)
    tasks = [
        asyncio.create_task(bridge.run(), name="mqtt"),
        asyncio.create_task(run_ups_poller(settings, machine), name="ups"),
        asyncio.create_task(_tick_loop(), name="tick"),
        asyncio.create_task(_balance_loop(), name="balance"),
    ]
    log.info("FireWatch started (%d detectors, %d recipients, auth=%s)",
             len(cfg.detectors), len(cfg.recipients), "on" if _auth_enabled else "off")
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="Venue FireWatch", version="0.2.0", lifespan=lifespan)


# ---- Auth middleware -----------------------------------------------------

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if not _auth_enabled:
        return await call_next(request)

    path = request.url.path
    if path in ("/api/login", "/login", "/api/sms/dlr", "/api/sms/ack"):
        return await call_next(request)
    if path.endswith((".css", ".js", ".ico", ".png", ".svg")):
        return await call_next(request)

    token = request.cookies.get("fw_token") or request.query_params.get("token")
    if not token:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

    if _verify_token(token):
        return await call_next(request)

    if request.url.path.startswith("/api/") or request.url.path == "/ws":
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


# ---- Auth endpoints ------------------------------------------------------

@app.post("/api/login")
async def login(req: LoginRequest) -> JSONResponse:
    if not _auth_enabled:
        return JSONResponse({"token": "auth_disabled"})
    if not hmac.compare_digest(req.password, settings.auth_password):
        return JSONResponse({"error": "wrong password"}, status_code=401)
    token = _issue_token()
    resp = JSONResponse({"ok": True})
    resp.set_cookie("fw_token", token, httponly=True, samesite="strict", max_age=settings.auth_token_ttl_hours * 3600)
    return resp


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FireWatch — Login</title>
<link rel="stylesheet" href="/style.css">
<style>
.login{max-width:340px;margin:15vh auto;padding:28px;background:var(--panel);border:1px solid var(--hairline);border-radius:14px}
.login h1{font-size:1.1rem;margin:0 0 18px;color:var(--live)}
.login input{width:100%;padding:10px;margin-bottom:12px;background:var(--raised);border:1px solid var(--hairline);color:var(--text);border-radius:8px;font-family:var(--mono);font-size:.9rem}
.login button{width:100%;padding:10px;background:color-mix(in srgb,var(--live) 22%,var(--raised));border:1px solid color-mix(in srgb,var(--live) 50%,transparent);color:var(--text);border-radius:8px;font-weight:600;cursor:pointer;font-size:.9rem}
.login .err{color:var(--alarm);font-size:.82rem;margin-top:8px;display:none}
</style></head><body>
<div class="login"><h1>FireWatch</h1>
<form id="f"><input type="password" id="pw" placeholder="Password" autofocus>
<button type="submit">Log in</button><div class="err" id="err">Wrong password</div></form></div>
<script>
document.getElementById("f").onsubmit=async e=>{e.preventDefault();
const r=await fetch("/api/login",{method:"POST",headers:{"Content-Type":"application/json"},
body:JSON.stringify({password:document.getElementById("pw").value})});
if(r.ok){location.href="/"}else{document.getElementById("err").style.display="block"}};
</script></body></html>"""


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


# ---- WebSocket -----------------------------------------------------------

@app.websocket("/ws")
async def ws(socket: WebSocket, token: str | None = Query(default=None)) -> None:
    if _auth_enabled:
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
