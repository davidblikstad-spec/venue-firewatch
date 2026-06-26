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

from .config import Detector, Recipient, Settings, YamlConfig
from .db import Database
from . import templates as tmpl
from .models import (
    BalanceState,
    DetectorState,
    LinkState,
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


def _mains_present(grid_voltage: float | None) -> bool:
    """True when input.voltage looks like live mains (not the 0V an on-battery
    UPS reports). 50V is well below any real mains yet above the on-battery 0."""
    return grid_voltage is not None and grid_voltage > 50


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
        self.balance = BalanceState()
        self.link = LinkState()
        self._alert_counter = 0
        self._active_alerts: dict[str, dict] = {}
        # Operator-editable SMS text. Holds overrides only; missing keys fall
        # back to the built-in default. Populated from the kv store at startup.
        self.templates: dict[str, str] = {}
        self._primary_down = False  # True once GatewayAPI has failed and we're on the modem
        self._restore_pending = False  # mains came back; awaiting a real voltage to announce

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

    async def set_detectors(self, detectors: list[Detector]) -> None:
        """Replace the monitored-detector set at runtime (dashboard-driven).

        Live state (last_seen, alarm, battery…) is preserved for detectors that
        stay; new ones start fresh; removed ones are dropped. The MQTT bridge
        shares this `cfg` object and reads alarm properties live, so a new
        detector is monitored on the next message with no reconnect.
        """
        self._cfg.detectors = detectors
        rebuilt: dict[str, DetectorState] = {}
        for d in detectors:
            existing = self._detectors.get(d.friendly_name)
            if existing is not None:
                existing.label = d.label
                existing.zone = d.zone
                existing.kind = d.kind
                rebuilt[d.friendly_name] = existing
            else:
                rebuilt[d.friendly_name] = DetectorState(
                    friendly_name=d.friendly_name,
                    label=d.label,
                    zone=d.zone,
                    kind=d.kind,
                )
        self._detectors = rebuilt
        await self._publish()

    def set_recipients(self, recipients: list[Recipient]) -> None:
        """Replace the alert-recipient list at runtime (dashboard-driven).

        Read live by `_raise`/escalation off `self._cfg.recipients`, so an edit
        applies to the next alert with no reconnect or restart.
        """
        self._cfg.recipients = recipients

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

    def _next_alert_key(self) -> str:
        self._alert_counter += 1
        return f"alert-{self._alert_counter}-{int(now().timestamp())}"

    def snapshot(self) -> SystemSnapshot:
        return SystemSnapshot(
            mode=self.mode,
            sms_policy=self.sms_policy,
            event_until=self.event_until,
            detectors=list(self._detectors.values()),
            ups=self.ups,
            balance=self.balance,
            link=self.link,
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
                            self._msg("detector_offline", label=det.label, zone=det.zone),
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
        self.link.last_message = det.last_seen
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
            await self._raise(Severity.WARNING, self._msg("detector_low_battery", label=det.label, battery=det.battery), actor="supervision")

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
            self._msg("alarm", label=det.label, kind=det.kind, zone=det.zone, temperature=det.temperature),
            actor="detector",
        )

    # ---- UPS ----------------------------------------------------------

    # ---- MQTT / Zigbee pipeline health -------------------------------

    async def set_mqtt_connected(self, connected: bool) -> None:
        if self.link.mqtt_connected == connected:
            return
        self.link.mqtt_connected = connected
        if not connected:
            self.link.zigbee_online = False  # can't know Z2M state without the broker
        log.info("MQTT broker %s", "connected" if connected else "disconnected")
        await self._publish()

    async def set_zigbee_online(self, online: bool) -> None:
        if self.link.zigbee_online == online:
            return
        self.link.zigbee_online = online
        self.link.last_message = now()
        log.info("Zigbee2MQTT bridge %s", "online" if online else "offline")
        await self._publish()

    async def clear_ups(self) -> None:
        """Drop UPS state back to 'not monitored' when polling is disabled."""
        self.ups = UpsState()
        await self._publish()

    async def on_ups_update(self, ups: UpsState) -> None:
        prev_monitored = self.ups.monitored
        prev_battery = self.ups.on_battery
        prev_low = self.ups.low_battery
        self.ups = ups
        runtime_min = round(ups.runtime_s / 60) if ups.runtime_s is not None else None
        # Power events are NEVER silenced by EVENT mode.
        if ups.on_battery and not prev_battery:
            self._restore_pending = False  # a new outage cancels any pending restore
            await self._raise(
                Severity.WARNING,
                self._msg("ups_on_battery", charge=ups.charge_pct, runtime_min=runtime_min,
                          grid_voltage=ups.grid_voltage, load=ups.load_pct),
                actor="ups",
            )
        # Mains restored: arm on the recovery transition (prev_monitored guards the
        # cold start), but DON'T announce until the UPS reports a real mains voltage.
        # This APC reads input.voltage=0 while on battery and for a poll or two after
        # power returns, so firing immediately would send "restored (0V)".
        if prev_monitored and prev_battery and not ups.on_battery:
            self._restore_pending = True
        if self._restore_pending and not ups.on_battery and _mains_present(ups.grid_voltage):
            self._restore_pending = False
            await self._raise(
                Severity.INFO,
                self._msg("ups_restored", grid_voltage=round(ups.grid_voltage)),
                actor="ups",
            )
        if ups.low_battery and not prev_low:
            await self._raise(
                Severity.CRITICAL,
                self._msg("ups_low_battery", charge=ups.charge_pct, runtime_min=runtime_min),
                actor="ups",
            )
        await self._publish()

    # ---- alerting -----------------------------------------------------

    def _msg(self, key: str, **ctx) -> str:
        """Render an alert's SMS text from the operator template (or the default)."""
        template = self.templates.get(key) or tmpl.default_text(key)
        return tmpl.render(template, ctx)

    def set_templates(self, overrides: dict[str, str]) -> None:
        """Replace the live SMS-template overrides (kept persisted by the caller)."""
        self.templates = {k: v for k, v in overrides.items() if k in tmpl.SMS_TEMPLATES and v}

    def status_summary(self) -> str:
        """One SMS summarising every important status — used by the test message."""
        dets = list(self._detectors.values())
        online = sum(1 for d in dets if d.online)
        in_alarm = sum(1 for d in dets if d.alarm)
        low_batt = sum(1 for d in dets if d.battery is not None and d.battery <= 10)
        # Local time for the operator (now() is UTC); .astimezone() -> system tz.
        lines = [f"FireWatch TEST {now().astimezone():%Y-%m-%d %H:%M}"]
        lines.append(f"Mode: {self.mode.value.upper()}")
        lines.append(f"Detectors: {online}/{len(dets)} online, {in_alarm} in alarm, {low_batt} low battery")
        u = self.ups
        if u.monitored:
            bits = [u.raw_status or ("online" if u.online else "unreachable")]
            if u.grid_voltage is not None:
                bits.append(f"grid {u.grid_voltage:.0f}V")
            if u.load_pct is not None:
                bits.append(f"load {u.load_pct}%")
            if u.charge_pct is not None:
                bits.append(f"batt {u.charge_pct}%")
            if u.runtime_s is not None:
                bits.append(f"~{round(u.runtime_s / 60)}min")
            lines.append("UPS: " + ", ".join(bits))
        else:
            lines.append("UPS: not monitored")
        lines.append(
            f"Zigbee: {'online' if self.link.zigbee_online else 'OFFLINE'}, "
            f"broker {'up' if self.link.mqtt_connected else 'DOWN'}"
        )
        if self.balance.credit is not None:
            lines.append(f"SMS credit: {self.balance.credit:.0f}")
        lines.append("This is a test — no action needed.")
        return "\n".join(lines)

    async def send_test_message(self) -> dict:
        """Send the status summary to every recipient via the current policy.

        Deliberately bypasses the alert/escalation machinery — it's a delivery
        check, not an alarm, so no escalation timers are armed.
        """
        recips = sorted(self._cfg.recipients, key=lambda r: r.priority)
        if not recips:
            return {"ok": False, "error": "No receivers configured — add at least one phone number first."}
        text = self.status_summary()
        msisdns = [r.msisdn for r in recips]
        results = await self._notifier.broadcast(msisdns, text, self.sms_policy)
        sent = 0
        for msisdn, send_results in results.items():
            for sr in send_results:
                if sr.ok:
                    sent += 1
                    await self._db.track_sms(sr.message_id, msisdn, text, sr.transport, "test")
        await self._db.audit(
            "sms",
            {"text": text, "kind": "test",
             "results": {m: [r.__dict__ for r in rs] for m, rs in results.items()}},
            severity=Severity.INFO.value,
            actor="test",
        )
        log.info("test message sent to %d recipient(s); %d transport-sends ok", len(msisdns), sent)
        return {
            "ok": any(sr.ok for rs in results.values() for sr in rs),
            "recipients": len(msisdns),
            "sent": sent,
            "text": text,
            "detail": {m: [{"transport": r.transport, "ok": r.ok, "detail": r.detail} for r in rs]
                       for m, rs in results.items()},
        }

    async def _maybe_announce_failover(self, results: dict, msisdns: list[str]) -> None:
        """One-shot notice the first time GatewayAPI fails and we fall to the modem.

        Sent over the modem only (the primary path is down by definition) and
        deduped via _primary_down so a sustained outage doesn't spam operators.
        """
        primary_failed = any(
            any(r.transport == "gatewayapi" and not r.ok for r in rs)
            for rs in results.values()
        )
        if not primary_failed:
            self._primary_down = False
            return
        if self._primary_down:
            return
        self._primary_down = True
        text = self._msg("sms_failover")
        modem_results = await self._notifier.send_via_modem(msisdns, text)
        await self._db.audit(
            "sms",
            {"text": text, "kind": "failover_notice",
             "results": {m: [r.__dict__ for r in rs] for m, rs in modem_results.items()}},
            severity=Severity.WARNING.value,
            actor="notify",
        )

    async def _raise(self, severity: Severity, text: str, actor: str) -> None:
        alert_key = self._next_alert_key()
        sorted_recips = sorted(self._cfg.recipients, key=lambda r: r.priority)
        if not sorted_recips:
            log.error("alert with no recipients configured: %s", text)
            return

        first_priority = sorted_recips[0].priority
        initial_recips = [r for r in sorted_recips if r.priority == first_priority]
        msisdns = [r.msisdn for r in initial_recips]

        policy = self.sms_policy
        results = await self._notifier.broadcast(msisdns, text, policy)
        if policy is SmsPolicy.FAILOVER:
            await self._maybe_announce_failover(results, msisdns)

        for msisdn, send_results in results.items():
            for sr in send_results:
                if sr.ok:
                    await self._db.track_sms(sr.message_id, msisdn, text, sr.transport, alert_key)

        await self._db.audit(
            "sms",
            {
                "text": text,
                "policy": policy.value,
                "alert_key": alert_key,
                "results": {m: [r.__dict__ for r in rs] for m, rs in results.items()},
            },
            severity=severity.value,
            actor=actor,
        )

        if severity is Severity.CRITICAL and len(sorted_recips) > len(initial_recips):
            remaining = [r for r in sorted_recips if r.priority > first_priority]
            self._active_alerts[alert_key] = {
                "text": text,
                "severity": severity,
                "actor": actor,
                "remaining_recipients": remaining,
                "sent_at": now(),
                "escalation_round": 0,
            }

    async def check_escalations(self) -> None:
        """Escalate unacknowledged alerts to the next priority tier."""
        timeout = timedelta(minutes=self._s.escalation_timeout_minutes)
        expired_keys = []
        for alert_key, info in list(self._active_alerts.items()):
            if now() - info["sent_at"] < timeout:
                continue
            unacked = await self._db.unacked_alerts(alert_key)
            if not unacked:
                expired_keys.append(alert_key)
                continue

            remaining = info["remaining_recipients"]
            if not remaining:
                expired_keys.append(alert_key)
                continue

            next_priority = remaining[0].priority
            next_batch = [r for r in remaining if r.priority == next_priority]
            after = [r for r in remaining if r.priority > next_priority]

            msisdns = [r.msisdn for r in next_batch]
            text = f"[ESCALATION] {info['text']}"
            results = await self._notifier.broadcast(msisdns, text, self.sms_policy)

            for msisdn, send_results in results.items():
                for sr in send_results:
                    if sr.ok:
                        await self._db.track_sms(sr.message_id, msisdn, text, sr.transport, alert_key)

            await self._db.audit(
                "escalation",
                {
                    "alert_key": alert_key,
                    "round": info["escalation_round"] + 1,
                    "recipients": msisdns,
                    "results": {m: [r.__dict__ for r in rs] for m, rs in results.items()},
                },
                severity=info["severity"].value,
                actor="escalation",
            )
            log.warning("escalated alert %s to %s", alert_key, msisdns)

            if after:
                info["remaining_recipients"] = after
                info["sent_at"] = now()
                info["escalation_round"] += 1
            else:
                expired_keys.append(alert_key)

        for k in expired_keys:
            self._active_alerts.pop(k, None)

    async def ack_alert(self, msisdn: str, alert_key: str) -> bool:
        """Acknowledge an alert — stops escalation for this recipient."""
        found = await self._db.record_ack(msisdn, alert_key)
        if found:
            await self._db.audit("system", {"action": "ack", "alert_key": alert_key, "msisdn": msisdn}, actor="recipient")
            unacked = await self._db.unacked_alerts(alert_key)
            if not unacked:
                self._active_alerts.pop(alert_key, None)
        return found
