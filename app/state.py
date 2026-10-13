"""Core state machine.

Holds live detector/UPS state and the NORMAL/EVENT mode. Decides, when a
detector trips, the SMS alert and whether to sound the siren interconnect
(not in EVENT mode). Owns the EVENT-mode auto-expiry — the single most important
safety feature here: EVENT is never an open-ended toggle, it always has an
end time and reverts to NORMAL on its own.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from .config import Detector, Recipient, Settings, YamlConfig
from .db import Database
from . import templates as tmpl
from .models import (
    BalanceState,
    DetectorState,
    LinkState,
    MeshDeviceState,
    Mode,
    Severity,
    SmsPolicy,
    SystemSnapshot,
    UpsState,
    WanState,
    now,
)
from .notify import Notifier

log = logging.getLogger("firewatch.state")

# Hours without a report before a mesh router (plug) is shown offline. Routers
# beacon far more often than this; the window is generous to avoid flapping and
# is only a display hint — mesh devices never raise SMS alerts.
_MESH_OFFLINE_HOURS = 2

# A detector update whose Z2M `last_seen` (time of the device's last real radio
# message) is older than this is a cached-state replay, not a fresh detection.
# Z2M republishes its state cache on startup, so a stale `smoke: true` that
# never got its clear would otherwise re-trip the alarm on every reboot. A real
# report is published within milliseconds of reception, so this is generous.
_REPLAY_MAX_AGE_S = 60

# Z2M flaps every repeater offline→online within a second or two while it
# starts up. Only alert if a repeater is still down after this grace period.
_REPEATER_OFFLINE_GRACE_S = 120


def _parse_last_seen(value) -> datetime | None:
    """Z2M `last_seen`: ISO 8601 (UTC or local offset) or epoch milliseconds."""
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        if isinstance(value, str):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else None
    except (ValueError, OverflowError, OSError):
        pass
    return None

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

        self.venue: str = (cfg.venue_name or "").strip()
        self.started_at: datetime | None = None  # set by main at lifespan startup
        self.mode: Mode = Mode.NORMAL
        self.sms_policy: SmsPolicy = SmsPolicy.FAILOVER
        self.event_until: datetime | None = None
        self.ups = UpsState()
        self.balance = BalanceState()
        self.link = LinkState()
        self.wan = WanState()
        self._alert_counter = 0
        self._active_alerts: dict[str, dict] = {}
        # Operator-editable SMS text. Holds overrides only; missing keys fall
        # back to the built-in default. Populated from the kv store at startup.
        self.templates: dict[str, str] = {}
        self._primary_down = False  # True once GatewayAPI has failed and we're on the modem
        self._restore_pending = False  # mains came back; awaiting a real voltage to announce
        # Publisher to a detector's /set topic (wired to the MQTT bridge in
        # main). None until wired; commands are no-ops without it.
        self._siren: Callable[[str, dict], Awaitable[bool]] | None = None
        self._interconnect_active = False  # True once we've sounded remote sirens
        # When the interconnect sirens self-stop and who tripped them, for the
        # dashboard. Z2M's SMSZB-120 converter runs each start for 300 s.
        self.sirens_until: datetime | None = None
        self.sirens_origin: str | None = None
        self._silence_task: asyncio.Task | None = None
        # The fire alarm in progress, so an all-clear SMS can follow it to the
        # same people: started, label, zone, alert_keys, msisdns, sirens, silenced.
        self._fire_event: dict | None = None
        self._all_clear_since: datetime | None = None  # when everything first read quiet

        self._detectors: dict[str, DetectorState] = {
            d.friendly_name: DetectorState(
                friendly_name=d.friendly_name,
                label=d.label,
                zone=d.zone,
                kind=d.kind,
            )
            for d in cfg.detectors
        }
        # Mains-powered routers (smart plugs/relays) that aren't detectors,
        # discovered live from their Z2M state messages. Keyed by friendly_name.
        self._mesh: dict[str, MeshDeviceState] = {}
        # Repeaters we've had a Z2M availability reading for (so we don't alert
        # on the cold-start baseline) and those we've alerted as down (so the
        # recovery notice only fires for a repeater we actually reported down).
        self._mesh_avail_seen: set[str] = set()
        self._mesh_down: set[str] = set()
        # Repeaters seen going offline but not yet alerted (debounce), and when.
        self._mesh_pending_down: dict[str, datetime] = {}
        # When we started watching each detector. Supervision measures silence
        # from here for a detector that hasn't reported since FireWatch started,
        # so one that went quiet before a restart still gets flagged offline.
        self._supervise_from: dict[str, datetime] = {name: now() for name in self._detectors}
        self._listeners: list[Listener] = []

    # ---- wiring -------------------------------------------------------

    def add_listener(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def set_siren(self, fn: Callable[[str, dict], Awaitable[bool]]) -> None:
        """Wire the publisher used to sound/silence remote sirens."""
        self._siren = fn

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
                self._supervise_from[d.friendly_name] = now()
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
            venue=self.venue,
            mode=self.mode,
            sms_policy=self.sms_policy,
            event_until=self.event_until,
            detectors=list(self._detectors.values()),
            mesh=sorted(self._mesh.values(), key=lambda d: d.friendly_name),
            ups=self.ups,
            balance=self.balance,
            link=self.link,
            wan=self.wan,
            started_at=self.started_at,
            sirens_until=self.sirens_until if self.sirens_until and self.sirens_until > now() else None,
            sirens_origin=self.sirens_origin,
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

        await self._maybe_all_clear()

        changed = False
        for det in self._detectors.values():
            cfg = next((d for d in self._cfg.detectors if d.friendly_name == det.friendly_name), None)
            since = det.last_seen or self._supervise_from.get(det.friendly_name)
            if cfg and since:
                offline = (now() - since) > timedelta(hours=cfg.offline_after_hours)
                if offline != (not det.online):
                    det.online = not offline
                    changed = True
                    if offline:
                        await self._raise(
                            Severity.WARNING,
                            self._msg("detector_offline", label=det.label, zone=det.zone),
                            actor="supervision", category="technical",
                        )
        for name, down_since in list(self._mesh_pending_down.items()):
            if (now() - down_since).total_seconds() < _REPEATER_OFFLINE_GRACE_S:
                continue
            del self._mesh_pending_down[name]
            dev = self._mesh.get(name)
            if dev is not None and not dev.online:
                self._mesh_down.add(name)
                await self._raise(
                    Severity.WARNING, self._msg("repeater_offline", label=name),
                    actor="supervision", category="technical",
                )
        # Mesh routers: fall back to a last_seen heuristic (display only, no SMS)
        # ONLY for repeaters we get no Z2M availability signal for. When
        # availability is enabled, on_mesh_availability owns the online flag and
        # the SMS, so we must not second-guess it here.
        for name, dev in self._mesh.items():
            if name in self._mesh_avail_seen:
                continue
            if dev.last_seen:
                offline = (now() - dev.last_seen) > timedelta(hours=_MESH_OFFLINE_HOURS)
                if offline == dev.online:
                    dev.online = not offline
                    changed = True
        if changed:
            await self._publish()

    # ---- detector ingestion ------------------------------------------

    async def on_detector_update(self, friendly_name: str, payload: dict, alarm_prop: str) -> None:
        det = self._detectors.get(friendly_name)
        if det is None:
            return  # unknown device; ignore (configure it in config.yaml to track)

        # Z2M's last_seen is when the device last actually transmitted; a
        # cached-state replay at Z2M startup carries an old one. Use it so a
        # replay can't make a silent detector look freshly checked in.
        reported = _parse_last_seen(payload.get("last_seen"))
        if reported is not None:
            reported = min(reported, now())
            if det.last_seen is None or reported > det.last_seen:
                det.last_seen = reported
        else:
            det.last_seen = now()
        cfg = next((d for d in self._cfg.detectors if d.friendly_name == friendly_name), None)
        if cfg is None or now() - det.last_seen <= timedelta(hours=cfg.offline_after_hours):
            det.online = True  # else leave it to tick() to flag (and alert) offline
        self.link.last_message = now()
        if "temperature" in payload:
            det.temperature = payload["temperature"]
        if "battery" in payload:
            det.battery = payload["battery"]
        if "linkquality" in payload:
            det.linkquality = payload["linkquality"]
        if "tamper" in payload or "fault" in payload:
            det.fault = bool(payload.get("tamper") or payload.get("fault"))

        was_alarm = det.alarm
        det.alarm = bool(payload.get(alarm_prop, det.alarm))

        if det.alarm and not was_alarm:
            # Without last_seen (Z2M option disabled) we can't tell a replay
            # from a detection, so fall through and alarm — fail safe.
            age = (now() - reported).total_seconds() if reported else None
            if age is not None and age > _REPLAY_MAX_AGE_S:
                det.alarm = False
                if not det.alarm_unconfirmed:
                    det.alarm_unconfirmed = True
                    log.warning(
                        "ignoring replayed %s=true for %s (device last reported %s, %.0fs ago)",
                        alarm_prop, det.label, reported.isoformat(), age,
                    )
                    await self._db.audit(
                        "alarm_replay",
                        {"detector": det.friendly_name, "zone": det.zone, "last_seen": reported.isoformat()},
                        severity=Severity.WARNING.value,
                        actor="detector",
                    )
            else:
                det.alarm_unconfirmed = False
                await self._handle_alarm(det)
        elif det.battery is not None and det.battery <= 10:
            await self._raise(
                Severity.WARNING,
                self._msg("detector_low_battery", label=det.label, battery=det.battery),
                actor="supervision", category="technical",
            )
        if payload.get(alarm_prop) is False:
            det.alarm_unconfirmed = False  # device (or cache) now reports clear

        await self._publish()

    async def sync_mesh_devices(self, routers: list[dict]) -> None:
        """Seed/prune the mesh card from Z2M's device inventory.

        Routers report state only on change and Z2M doesn't retain it, so a plug
        that's been idle since startup would never appear. Seeding from the
        (retained) inventory makes every repeater visible right away; live
        fields (last_seen, linkquality, load) fill in as reports arrive.
        Configured detectors are skipped — they belong on the detector card.
        """
        configured = {d.friendly_name for d in self._cfg.detectors}
        desired = {r["friendly_name"] for r in routers if r["friendly_name"] not in configured}
        changed = False
        for r in routers:
            fn = r["friendly_name"]
            if fn in configured:
                continue
            dev = self._mesh.get(fn)
            if dev is None:
                # online=False until it actually reports; the card shows "no
                # data yet" (last_seen is None) rather than a false "online".
                self._mesh[fn] = MeshDeviceState(
                    friendly_name=fn, vendor=r.get("vendor"), model=r.get("model"), online=False
                )
                changed = True
            elif (dev.vendor, dev.model) != (r.get("vendor"), r.get("model")):
                dev.vendor, dev.model = r.get("vendor"), r.get("model")
                changed = True
        for fn in [fn for fn in self._mesh if fn not in desired]:
            del self._mesh[fn]
            changed = True
        if changed:
            await self._publish()

    async def on_mesh_update(
        self, friendly_name: str, payload: dict, vendor: str | None = None, model: str | None = None
    ) -> None:
        """Record a report from a mains router (plug) so it shows on the mesh card.

        These are repeaters, not detectors: we track reachability and link
        quality for network visibility only — they never trigger alerts.
        """
        dev = self._mesh.get(friendly_name)
        if dev is None:
            dev = MeshDeviceState(friendly_name=friendly_name, vendor=vendor, model=model)
            self._mesh[friendly_name] = dev
        dev.last_seen = now()
        dev.online = True
        self.link.last_message = dev.last_seen
        if "linkquality" in payload:
            dev.linkquality = payload["linkquality"]
        if isinstance(payload.get("state"), str):
            dev.state = payload["state"]
        if isinstance(payload.get("power"), (int, float)):
            dev.power = payload["power"]
        await self._publish()

    async def on_mesh_availability(
        self, friendly_name: str, online: bool, vendor: str | None = None, model: str | None = None
    ) -> None:
        """Authoritative reachability for a mesh repeater, from Z2M availability.

        Sends a WARNING SMS when a repeater stays down past a short grace period
        (see tick) and an INFO when it comes back — but never on the first
        reading (the cold-start baseline), so a FireWatch or Z2M restart doesn't
        fire a spurious alert.
        """
        dev = self._mesh.get(friendly_name)
        if dev is None:
            dev = MeshDeviceState(friendly_name=friendly_name, vendor=vendor, model=model)
            self._mesh[friendly_name] = dev
        prev_online = dev.online
        dev.online = online
        if online:
            dev.last_seen = now()

        first = friendly_name not in self._mesh_avail_seen
        self._mesh_avail_seen.add(friendly_name)
        if first:
            await self._publish()  # establish baseline silently
            return

        if not online and prev_online:
            # Debounced: tick() alerts if it's still down after the grace period.
            self._mesh_pending_down.setdefault(friendly_name, now())
        elif online and self._mesh_pending_down.pop(friendly_name, None) is not None:
            pass  # brief blip (e.g. Z2M restart) — never alerted, nothing to recover
        elif online and not prev_online and friendly_name in self._mesh_down:
            self._mesh_down.discard(friendly_name)
            await self._raise(
                Severity.INFO, self._msg("repeater_online", label=friendly_name),
                actor="supervision", category="technical",
            )
        await self._publish()

    async def _handle_alarm(self, det: DetectorState) -> None:
        # The SMS always goes out — we always want to know a detector sensed
        # smoke. EVENT mode only keeps the venue quiet: no interconnect, so the
        # tripped detector's own buzzer is the only noise while staff investigate.
        event = self.mode is Mode.EVENT
        await self._db.audit(
            "alarm",
            {"detector": det.friendly_name, "zone": det.zone, "event_mode": event, "temperature": det.temperature},
            severity=Severity.CRITICAL.value,
            actor="detector",
        )
        alert_key, msisdns = await self._raise(
            Severity.CRITICAL,
            self._msg("alarm", label=det.label, kind=det.kind, zone=det.zone, temperature=det.temperature),
            actor="detector", category="fire",
        )
        if self._fire_event is None:
            self._fire_event = {
                "started": now(), "label": det.label, "zone": det.zone,
                "alert_keys": set(), "msisdns": set(), "sirens": False, "silenced": False,
            }
        self._fire_event["alert_keys"].add(alert_key)
        self._fire_event["msisdns"].update(msisdns)
        self._all_clear_since = None
        if event:
            log.warning("ALARM during EVENT mode: %s — SMS sent, interconnect sirens held", det.label)
            return
        await self._sound_interconnect(origin=det)

    async def _sound_interconnect(self, origin: DetectorState) -> None:
        """Sound every siren-capable detector when one trips (opt-in).

        Never called in EVENT mode (see _handle_alarm). Each siren self-stops
        after `siren_max_duration_s` so a dropped broker can't latch the buzzers on.
        """
        if not self._cfg.siren_interconnect or self._siren is None:
            return
        dur = self._cfg.siren_max_duration_s
        self._cancel_silence_retries()  # a fresh trip must not be undone by a stale OFF
        for d in self._cfg.detectors:
            if d.siren_property is None:
                continue  # device exposes no settable siren
            payload = {d.siren_property: True, "max_duration": dur}
            if await self._siren(d.friendly_name, payload):
                self._interconnect_active = True
        if self._interconnect_active:
            self.sirens_until = now() + timedelta(seconds=dur)
            self.sirens_origin = origin.label
            if self._fire_event is not None:
                self._fire_event["sirens"] = True
        await self._db.audit(
            "siren_interconnect", {"origin": origin.friendly_name}, actor="detector",
        )

    # Seconds after Silence to re-send OFF. Battery detectors are sleepy end
    # devices that only poll their parent every few seconds, so a single OFF
    # can expire in the mesh (ROUTE_ERROR_INDIRECT_TRANSACTION_EXPIRY).
    SILENCE_RETRY_S = (5, 10, 20, 30)

    async def silence_interconnect(self, actor: str = "operator") -> None:
        """Operator 'all clear' — drop every remote siren before it times out.

        Always sends OFF, even if we don't think we sounded anything (e.g. after
        a restart), then repeats it in the background for sleepy devices.
        """
        if self._siren is None:
            return
        self._cancel_silence_retries()
        await self._send_siren_off()
        self._interconnect_active = False
        self.sirens_until = None
        self.sirens_origin = None
        await self._db.audit("siren_silence", {}, actor=actor)
        await self._publish()
        self._silence_task = asyncio.create_task(self._silence_retries())
        if self._fire_event is not None:
            # Silence is the operator's "venue confirmed clear": no hold-off.
            self._fire_event["silenced"] = True
            await self._maybe_all_clear(confirmed=True)

    # Quiet time (no detector in alarm, sirens stopped) before an unconfirmed
    # all-clear goes out, so a detector flapping true/false doesn't trigger it.
    ALL_CLEAR_HOLD_S = 60

    async def _maybe_all_clear(self, confirmed: bool = False) -> None:
        """Send the all-clear SMS once the fire event is over: no detector in
        alarm and the interconnect sirens stopped (silenced or timed out)."""
        ev = self._fire_event
        if ev is None:
            return
        if any(d.alarm for d in self._detectors.values()) or (
            self.sirens_until is not None and self.sirens_until > now()
        ):
            self._all_clear_since = None
            return
        if not confirmed:
            if self._all_clear_since is None:
                self._all_clear_since = now()
                return
            if (now() - self._all_clear_since).total_seconds() < self.ALL_CLEAR_HOLD_S:
                return
        self._fire_event = None
        self._all_clear_since = None
        # The fire is over: stop escalating it to further tiers.
        for key in ev["alert_keys"]:
            self._active_alerts.pop(key, None)

        if ev["silenced"]:
            how = "sirens silenced from the dashboard"
        elif ev["sirens"]:
            how = "sirens stopped on their own timer (nobody pressed Silence)"
        else:
            how = "detector back to normal"
        secs = int((now() - ev["started"]).total_seconds())
        duration = f"{secs // 60}m {secs % 60:02d}s"
        text = self._msg("all_clear", label=ev["label"], zone=ev["zone"], how=how, duration=duration)
        log.warning("fire event over (%s); sending all-clear", how)
        await self._send_direct(sorted(ev["msisdns"]), text, Severity.INFO, actor="all_clear")

    async def _send_direct(self, msisdns: list[str], text: str, severity: Severity, actor: str) -> None:
        """SMS a fixed list of numbers (no category routing, no escalation)."""
        if not msisdns:
            await self._db.audit("sms_unrouted", {"text": text}, severity=severity.value, actor=actor)
            return
        alert_key = self._next_alert_key()
        results = await self._notifier.broadcast(msisdns, text, self.sms_policy)
        for msisdn, send_results in results.items():
            for sr in send_results:
                if sr.ok:
                    await self._db.track_sms(sr.message_id, msisdn, text, sr.transport, alert_key)
        await self._db.audit(
            "sms",
            {
                "text": text,
                "policy": self.sms_policy.value,
                "alert_key": alert_key,
                "results": {m: [r.__dict__ for r in rs] for m, rs in results.items()},
            },
            severity=severity.value,
            actor=actor,
        )

    async def _send_siren_off(self) -> None:
        for d in self._cfg.detectors:
            if d.siren_property is not None:
                await self._siren(d.friendly_name, {d.siren_property: False})

    async def _silence_retries(self) -> None:
        elapsed = 0
        for at in self.SILENCE_RETRY_S:
            await asyncio.sleep(at - elapsed)
            elapsed = at
            await self._send_siren_off()

    def _cancel_silence_retries(self) -> None:
        if self._silence_task and not self._silence_task.done():
            self._silence_task.cancel()
        self._silence_task = None

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
                actor="ups", category="power",
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
                actor="ups", category="power",
            )
        if ups.low_battery and not prev_low:
            await self._raise(
                Severity.CRITICAL,
                self._msg("ups_low_battery", charge=ups.charge_pct, runtime_min=runtime_min),
                actor="ups", category="power",
            )
        await self._publish()

    async def on_wan_update(self, wan: WanState) -> None:
        """Refresh the internet-uplink view and SMS recipients if the active
        path changed (e.g. wired WAN died and we're now on cellular)."""
        prev_active = self.wan.active
        had_data = self.wan.monitored
        self.wan = wan
        # Only announce a *change*, never the first reading after boot.
        if wan.monitored and had_data and wan.active != prev_active:
            await self._announce_wan_change(prev_active)
        await self._publish()

    async def _announce_wan_change(self, prev_active: str | None) -> None:
        """Notify all recipients that the internet path changed, listing links."""
        active_label = "no internet"
        for a in self.wan.adapters:
            if a.active:
                active_label = a.label
                break
        summary = "; ".join(
            f"{a.label}: " + ("IN USE" if a.active else "up" if a.link else "down")
            for a in self.wan.adapters
        ) or "none"
        text = self._msg("wan_changed", active=active_label, summary=summary)

        recips = sorted(
            (r for r in self._cfg.recipients if "technical" in r.categories), key=lambda r: r.priority
        )
        msisdns = [r.msisdn for r in recips]
        log.warning("WAN path changed (%s -> %s); notifying %d recipient(s)",
                    prev_active, self.wan.active, len(msisdns))
        results = (await self._notifier.broadcast(msisdns, text, self.sms_policy)
                   if msisdns else {})
        for msisdn, send_results in results.items():
            for sr in send_results:
                if sr.ok:
                    await self._db.track_sms(sr.message_id, msisdn, text, sr.transport, "wan_change")
        await self._db.audit(
            "wan",
            {"text": text, "kind": "wan_change", "from": prev_active, "to": self.wan.active,
             "adapters": [a.model_dump() for a in self.wan.adapters]},
            severity=Severity.WARNING.value,
            actor="wan",
        )

    # ---- alerting -----------------------------------------------------

    def brand(self) -> str:
        """Name that identifies this site in messages; falls back to FireWatch."""
        return self.venue or "FireWatch"

    def _msg(self, key: str, **ctx) -> str:
        """Render an alert's SMS text from the operator template (or the default),
        prefixed with the venue/brand so recipients know which site it's from."""
        template = self.templates.get(key) or tmpl.default_text(key)
        return f"{self.brand()}: {tmpl.render(template, ctx)}"

    def set_templates(self, overrides: dict[str, str]) -> None:
        """Replace the live SMS-template overrides (kept persisted by the caller)."""
        self.templates = {k: v for k, v in overrides.items() if k in tmpl.SMS_TEMPLATES and v}

    async def set_venue(self, name: str) -> None:
        """Set the site name shown in the header and prefixed to SMS; publishes
        so the dashboard heading updates live."""
        self.venue = (name or "").strip()
        await self._publish()

    def status_summary(self) -> str:
        """One SMS summarising every important status — used by the test message."""
        dets = list(self._detectors.values())
        online = sum(1 for d in dets if d.online)
        in_alarm = sum(1 for d in dets if d.alarm)
        low_batt = sum(1 for d in dets if d.battery is not None and d.battery <= 10)
        # Local time for the operator (now() is UTC); .astimezone() -> system tz.
        lines = [f"{self.brand()} TEST {now().astimezone():%Y-%m-%d %H:%M}"]
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
        if self.wan.monitored:
            active = next((a.label for a in self.wan.adapters if a.active), None)
            up = [a.label for a in self.wan.adapters if a.link]
            lines.append(f"Internet: {active or 'DOWN'}" +
                         (f" (links up: {', '.join(up)})" if up else ""))
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

    async def _raise(
        self, severity: Severity, text: str, actor: str, category: str,
    ) -> tuple[str, list[str]]:
        """Send an alert to the category's first priority tier (escalating later
        if critical). Returns the alert key and the numbers it went to."""
        alert_key = self._next_alert_key()
        sorted_recips = sorted(
            (r for r in self._cfg.recipients if category in r.categories), key=lambda r: r.priority
        )
        if not sorted_recips:
            log.error("%s alert with no recipients subscribed: %s", category, text)
            await self._db.audit(
                "sms_unrouted", {"text": text, "category": category},
                severity=severity.value, actor=actor,
            )
            return alert_key, []

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
        return alert_key, msisdns

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
            if self._fire_event is not None and alert_key in self._fire_event["alert_keys"]:
                self._fire_event["msisdns"].update(msisdns)  # they get the all-clear too

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
