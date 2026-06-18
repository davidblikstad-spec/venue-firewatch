"""FastAPI application — wiring, HTTP control endpoints, and the live WebSocket.

Background tasks started at lifespan:
  - MQTT bridge (Z2M -> state machine)
  - UPS poller (NUT)
  - tick loop (EVENT auto-expiry + detector supervision)
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import get_settings, load_yaml_config
from .db import Database
from .models import Mode, SmsPolicy, SystemSnapshot
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
        await asyncio.sleep(5)


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
    ]
    log.info("FireWatch started (%d detectors, %d recipients)", len(cfg.detectors), len(cfg.recipients))
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="Venue FireWatch", version="0.1.0", lifespan=lifespan)


# ---- API models ------------------------------------------------------

class ArmEventRequest(BaseModel):
    until: datetime
    actor: str = "dashboard"


class PolicyRequest(BaseModel):
    policy: SmsPolicy
    actor: str = "dashboard"


# ---- REST endpoints --------------------------------------------------

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


@app.websocket("/ws")
async def ws(socket: WebSocket) -> None:
    await socket.accept()
    clients.add(socket)
    # Send current state immediately on connect.
    await socket.send_json({"type": "snapshot", "data": machine.snapshot().model_dump(mode="json")})
    try:
        while True:
            await socket.receive_text()  # we don't expect inbound; keeps it open
    except WebSocketDisconnect:
        pass
    finally:
        clients.remove(socket)


# ---- static frontend (mounted last so /api and /ws take precedence) --
_web_dir = Path(__file__).parent / "web"
app.mount("/", StaticFiles(directory=str(_web_dir), html=True), name="web")
