"""Core state machine.

Holds live detector/UPS state and the NORMAL/EVENT mode. Decides, when a
detector trips, whether to raise an SMS alert or merely log it (EVENT mode,
silenced zone). Owns the EVENT-mode auto-expiry — the single most important
safety feature here: EVENT is never an open-ended toggle, it always has an
end time and reverts to NORMAL on its own.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from .config import Settings, YamlConfig
from .db import Database
from .models import (
    DetectorState,
    Mode,
    Severity,
    SmsPolicy,
    SystemSnapshot,
    UpsState,
    now,
)
from .notify import Notifier

log = logging.getLogger("firewatch.state")

# Called whenever state changes so the web layer can push to connected clients.
Listener = Callable[[SystemSnapshot], Awaitable[None]]


class StateMachine:
    def __init__(
        self,
        settings: Settings,
        cfg: YamlConfig,
        db: Database,
        notifier: Notifier,
    ) -> None:
        self._s = settings
        self._cfg = cfg
        self._db = db
        self._notifier = notifier

        self.mode: Mode = Mode.NORMAL
        self.sms_policy: SmsPolicy = SmsPolicy.FAILOVER
        self.event_until: datetime | None = None
        self.ups = UpsState()

        self._detectors: dict[str, DetectorState] = {
            d.friendly_name: DetectorState(
                friendly_name=d.friendly_name,
                label=d.label,
                zone=d.zone,
                kind=d.kind,
            )
            for d in cfg.detectors
        }
        self._listeners: list[Listener] = []

    # ---- wiring -------------------------------------------------------

    def add_listener(self, fn: Listener) -> None:
        self._listeners.append(fn)

    async def restore(self) -> None:
        """Resume mode/policy from the DB after a restart."""
        self.mode = Mode(await self._db.get("mode", Mode.NORMAL.value))
        self.sms_policy = SmsPolicy(await self._db.get("sms_policy", SmsPolicy.FAILOVER.value))
        until = await self._db.get("event_until")
        self.event_until = datetime.fromisoformat(until) if until else None
        # If we restored into EVENT but the window already passed, revert now.
        if self.mode is Mode.EVENT and (not self.event_until or self.event_until <= now()):
            await self.set_mode(Mode.NORMAL, actor="restore (expired)")
        log.info("restored mode=%s policy=%s until=%s", self.mode, self.sms_policy, self.event_until)

    def snapshot(self) -> SystemSnapshot:
        return SystemSnapshot(
            mode=self.mode,
            sms_policy=self.sms_policy,
            event_until=self.event_until,
            detectors=list(self._detectors.values()),
            ups=self.ups,
        )

    async def _publish(self) -> None:
        snap = self.snapshot()
        for fn in self._listeners:
            try:
                await fn(snap)
            except Exception:  # never let a dead client break the core
                log.exception("listener failed")

    # ---- mode control -------------------------------------------------

    async def arm_event(self, until: datetime, actor: str) -> datetime:
        """Enter EVENT (silent) mode until `until`, capped by event_max_hours."""
        ceiling = now() + timedelta(hours=self._s.event_max_hours)
        effective = min(until, ceiling)
        self.event_until = effective
        await self._db.set("event_until", effective.isoformat())
        await self.set_mode(Mode.EVENT, actor=actor)
        log.warning("EVENT mode armed by %s until %s", actor, effective.isoformat())
        return effective

    async def end_event(self, actor: str) -> None:
        self.event_until = None
        await self._db.set("event_until", "")
        await self.set_mode(Mode.NORMAL, actor=actor)

    async def set_mode(self, mode: Mode, actor: str) -> None:
        previous = self.mode
        self.mode = mode
        await self._db.set("mode", mode.value)
        if mode is Mode.NORMAL:
            self.event_until = None
            await self._db.set("event_until", "")
        await self._db.audit(
            "mode_change",
            {"from": previous.value, "to": mode.value, "until": self.event_until.isoformat() if self.event_until else None},
            actor=actor,
        )
        await self._publish()

    async def set_policy(self, policy: SmsPolicy, actor: str) -> None:
        self.sms_policy = policy
        await self._db.set("sms_policy", policy.value)
        await self._db.audit("system", {"sms_policy": policy.value}, actor=actor)
        await self._publish()

    async def tick(self) -> None:
        """Called periodically: expire EVENT mode and flag offline detectors."""
        if self.mode is Mode.EVENT and self.event_until and self.event_until <= now():
            log.warning("EVENT mode auto-expired; reverting to NORMAL")
            await self.set_mode(Mode.NORMAL, actor="auto-expiry")

        changed = False
        for det in self._detectors.values():
            cfg = next((d for d in self._cfg.detectors if d.friendly_name == det.friendly_name), None)
            if cfg and det.last_seen:
                offline = (now() - det.last_seen) > timedelta(hours=cfg.offline_after_hours)
                if offline != (not det.online):
                    det.online = not offline
                    changed = True
                    if offline:
                        await self._raise(
                            Severity.WARNING,
                            f"Detector offline: {det.label} (no check-in)",
                            actor="supervision",
                        )
        if changed:
            await self._publish()

    # ---- detector ingestion ------------------------------------------

    async def on_detector_update(self, friendly_name: str, payload: dict, alarm_prop: str) -> None:
        det = self._detectors.get(friendly_name)
        if det is None:
            return  # unknown device; ignore (configure it in config.yaml to track)

        det.last_seen = now()
        det.online = True
        if "temperature" in payload:
            det.temperature = payload["temperature"]
        if "battery" in payload:
            det.battery = payload["battery"]
        if "tamper" in payload or "fault" in payload:
            det.fault = bool(payload.get("tamper") or payload.get("fault"))

        was_alarm = det.alarm
        det.alarm = bool(payload.get(alarm_prop, det.alarm))

        if det.alarm and not was_alarm:
            await self._handle_alarm(det)
        elif det.battery is not None and det.battery <= 10:
            await self._raise(Severity.WARNING, f"Low battery: {det.label} ({det.battery}%)", actor="supervision")

        await self._publish()

    async def _handle_alarm(self, det: DetectorState) -> None:
        silenced = (
            self.mode is Mode.EVENT and det.zone in self._cfg.silent_zones_in_event
        )
        await self._db.audit(
            "alarm",
            {"detector": det.friendly_name, "zone": det.zone, "silenced": silenced, "temperature": det.temperature},
            severity=Severity.CRITICAL.value,
            actor="detector",
        )
        if silenced:
            log.warning("ALARM (silenced, EVENT mode): %s zone=%s", det.label, det.zone)
            return
        await self._raise(
            Severity.CRITICAL,
            f"ALARM: {det.label} ({det.kind}) detected. Temp {det.temperature}C.",
            actor="detector",
        )

    # ---- UPS ----------------------------------------------------------

    async def on_ups_update(self, ups: UpsState) -> None:
        prev_battery = self.ups.on_battery
        prev_low = self.ups.low_battery
        self.ups = ups
        # Power events are NEVER silenced by EVENT mode.
        if ups.on_battery and not prev_battery:
            await self._raise(Severity.WARNING, "Mains power lost — UPS on battery.", actor="ups")
        if ups.low_battery and not prev_low:
            await self._raise(Severity.CRITICAL, "UPS battery LOW — shutdown imminent.", actor="ups")
        await self._publish()

    # ---- alerting -----------------------------------------------------

    async def _raise(self, severity: Severity, text: str, actor: str) -> None:
        recipients = [r.msisdn for r in self._cfg.recipients]
        if not recipients:
            log.error("alert with no recipients configured: %s", text)
            return
        # The operator-selected policy governs all alerts. FAILOVER adds at most
        # the GatewayAPI timeout before the modem takes over — acceptable for a
        # secondary system. Switch to BOTH from the dashboard when redundancy
        # matters more than the odd duplicate text.
        policy = self.sms_policy
        results = await self._notifier.broadcast(recipients, text, policy)
        await self._db.audit(
            "sms",
            {
                "text": text,
                "policy": policy.value,
                "results": {m: [r.__dict__ for r in rs] for m, rs in results.items()},
            },
            severity=severity.value,
            actor=actor,
        )
