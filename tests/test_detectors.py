"""Tests for the detector-discovery framework and runtime detector reload.

The pure logic here (parsing Z2M's bridge/devices, and reconciling the live
detector set) is the part that's easy to get subtly wrong, so it's worth
pinning down without standing up MQTT or the web layer.
"""
import asyncio
import json

from app.config import Detector, Settings, YamlConfig
from app.db import Database
from app.mqtt_client import DeviceRegistry, _summarize_device
from app.notify import Notifier
from app.state import StateMachine

# A trimmed-but-realistic bridge/devices payload: coordinator, a smoke alarm
# (nested exposes), and a non-alarm device.
_SMOKE_EXPOSES = [
    {"type": "binary", "name": "smoke", "property": "smoke", "value_on": True, "value_off": False},
    {"type": "binary", "name": "tamper", "property": "tamper"},
    {"type": "numeric", "name": "battery", "property": "battery"},
    {"type": "composite", "features": [
        {"type": "binary", "name": "battery_low", "property": "battery_low"},
    ]},
]


def test_summarize_smoke_detector():
    s = _summarize_device({
        "friendly_name": "smoke_lobby", "type": "EndDevice",
        "definition": {"vendor": "Frient", "model": "SMSZB-120",
                       "description": "Smoke detector", "exposes": _SMOKE_EXPOSES},
    })
    assert s["is_alarm_device"] is True
    assert s["suggested_alarm_property"] == "smoke"
    assert s["suggested_kind"] == "smoke"
    assert "battery_low" in s["binary_properties"]  # nested feature was flattened


def test_summarize_detects_settable_siren():
    # `alarm` is settable (access bit 2) -> a siren; `smoke` is the trip prop;
    # a read-only `warning` (access 1) must NOT be offered as a siren.
    s = _summarize_device({
        "friendly_name": "smoke_x", "type": "EndDevice",
        "definition": {"vendor": "Develco", "model": "SMSZB-120", "exposes": [
            {"type": "binary", "name": "smoke", "property": "smoke", "access": 1},
            {"type": "binary", "name": "alarm", "property": "alarm", "access": 2},
            {"type": "binary", "name": "warning", "property": "warning", "access": 1},
            {"type": "numeric", "name": "max_duration", "property": "max_duration", "access": 7},
        ]},
    })
    assert s["siren_properties"] == ["alarm"]
    assert s["suggested_siren_property"] == "alarm"
    assert s["alarm_properties"] == ["smoke"]  # siren prop never pollutes alarm list


def _develco_siren_bridge(devices):
    from app.mqtt_client import MqttBridge
    registry = DeviceRegistry()
    registry.update(json.dumps(devices).encode())
    bridge = MqttBridge.__new__(MqttBridge)  # no broker: only the mapping is under test
    bridge._registry = registry
    sent: list[tuple[str, dict]] = []

    async def fake_publish_set(name, payload):
        sent.append((name, payload))
        return True

    bridge.publish_set = fake_publish_set
    return bridge, sent


def test_publish_siren_uses_device_on_off_values():
    # Regression: Z2M's SMSZB-120 converter treats anything but "OFF" as START,
    # so a JSON false re-triggered every siren when the operator hit Silence.
    bridge, sent = _develco_siren_bridge([{
        "friendly_name": "smoke_x", "type": "EndDevice",
        "definition": {"vendor": "Develco", "model": "SMSZB-120", "exposes": [
            {"type": "binary", "name": "smoke", "property": "smoke", "access": 1},
            {"type": "binary", "name": "alarm", "property": "alarm", "access": 2,
             "value_on": "START", "value_off": "OFF"},
        ]},
    }])

    async def go():
        await bridge.publish_siren("smoke_x", {"alarm": True, "max_duration": 300})
        await bridge.publish_siren("smoke_x", {"alarm": False})

    asyncio.run(go())
    assert sent == [("smoke_x", {"alarm": "START", "max_duration": 300}),
                    ("smoke_x", {"alarm": "OFF"})]


def test_publish_siren_falls_back_for_unknown_device():
    # Before bridge/devices arrives, `alarm` must still never go out as false.
    bridge, sent = _develco_siren_bridge([])

    async def go():
        await bridge.publish_siren("not_yet_known", {"alarm": False})

    asyncio.run(go())
    assert sent == [("not_yet_known", {"alarm": "OFF"})]


def test_summarize_no_siren_when_none_settable():
    s = _summarize_device({
        "friendly_name": "smoke_lobby", "type": "EndDevice",
        "definition": {"vendor": "Frient", "model": "SMSZB-120", "exposes": _SMOKE_EXPOSES},
    })
    assert s["siren_properties"] == []
    assert s["suggested_siren_property"] is None


def test_summarize_skips_coordinator():
    assert _summarize_device({"type": "Coordinator", "friendly_name": "Coordinator"}) is None


def test_registry_lists_alarm_devices_first():
    reg = DeviceRegistry()
    payload = [
        {"type": "Coordinator", "friendly_name": "Coordinator"},
        {"friendly_name": "plug", "type": "Router",
         "definition": {"exposes": [{"type": "binary", "name": "state", "property": "state"}]}},
        {"friendly_name": "smoke_lobby", "type": "EndDevice",
         "definition": {"vendor": "Frient", "model": "SMSZB-120", "exposes": _SMOKE_EXPOSES}},
    ]
    reg.update(json.dumps(payload).encode())
    devices = reg.devices()
    assert [d["friendly_name"] for d in devices] == ["smoke_lobby", "plug"]  # alarm first
    assert reg.last_seen is not None


def test_registry_ignores_garbage():
    reg = DeviceRegistry()
    reg.update(b"not json")
    assert reg.devices() == []


def _machine(tmp_path):
    settings = Settings(db_path=str(tmp_path / "t.db"))
    cfg = YamlConfig(detectors=[Detector(friendly_name="heat_a", label="Heat A", alarm_property="heat")])
    db = Database(settings.db_path)
    return StateMachine(settings, cfg, db, Notifier(settings)), cfg


def test_bridge_state_updates_link(tmp_path):
    machine, _ = _machine(tmp_path)
    from app.mqtt_client import MqttBridge
    bridge = MqttBridge(machine._s, machine._cfg, machine)

    async def go():
        # Z2M may send bare "online", a quoted string, or {"state": ...}.
        for payload, expect in [
            (b'{"state":"online"}', True), (b'{"state":"offline"}', False),
            (b"online", True), (b'"offline"', False),
        ]:
            await bridge._handle("zigbee2mqtt/bridge/state", payload)
            assert machine.link.zigbee_online is expect, payload

    asyncio.run(go())


def test_broker_drop_clears_zigbee_online(tmp_path):
    machine, _ = _machine(tmp_path)

    async def go():
        await machine.set_mqtt_connected(True)
        await machine.set_zigbee_online(True)
        assert machine.link.mqtt_connected and machine.link.zigbee_online
        # Losing the broker means we can no longer vouch for Z2M either.
        await machine.set_mqtt_connected(False)
        assert not machine.link.mqtt_connected and not machine.link.zigbee_online

    asyncio.run(go())


def test_set_detectors_preserves_live_state(tmp_path):
    machine, cfg = _machine(tmp_path)

    async def go():
        # Give the existing detector some live state.
        await machine.on_detector_update("heat_a", {"battery": 80, "heat": False}, "heat")
        # Replace the set: keep heat_a (renamed label), add smoke_b, drop nothing else.
        await machine.set_detectors([
            Detector(friendly_name="heat_a", label="Heat A (kitchen)", alarm_property="heat"),
            Detector(friendly_name="smoke_b", label="Smoke B", kind="smoke", alarm_property="smoke"),
        ])
        snap = machine.snapshot()
        by_name = {d.friendly_name: d for d in snap.detectors}
        assert set(by_name) == {"heat_a", "smoke_b"}
        # heat_a kept its battery reading but picked up the new label.
        assert by_name["heat_a"].battery == 80
        assert by_name["heat_a"].label == "Heat A (kitchen)"
        # The bridge reads alarm props off the shared cfg, which was updated.
        assert {d.friendly_name for d in cfg.detectors} == {"heat_a", "smoke_b"}

    asyncio.run(go())


def _siren_machine(tmp_path, *, interconnect=True):
    """A machine with two siren-capable detectors (in different zones) and
    a fake siren publisher that records every /set command."""
    settings = Settings(db_path=str(tmp_path / "t.db"))
    cfg = YamlConfig(
        siren_interconnect=interconnect,
        siren_max_duration_s=300,
        detectors=[
            Detector(friendly_name="smoke_foh", label="FoH", kind="smoke",
                     alarm_property="smoke", siren_property="alarm", zone="front_of_house"),
            Detector(friendly_name="smoke_stage", label="Stage", kind="smoke",
                     alarm_property="smoke", siren_property="alarm", zone="stage"),
            Detector(friendly_name="sensor_only", label="Sensor", kind="smoke",
                     alarm_property="smoke", zone="technical"),  # no siren_property
        ],
    )
    machine = StateMachine(settings, cfg, Database(settings.db_path), Notifier(settings))
    sent: list[tuple[str, dict]] = []

    async def fake_siren(name, payload):
        sent.append((name, payload))
        return True

    machine.set_siren(fake_siren)
    return machine, sent


def test_interconnect_sounds_every_siren_capable_detector(tmp_path):
    machine, sent = _siren_machine(tmp_path)

    async def go():
        await machine._db.init()
        await machine.on_detector_update("smoke_foh", {"smoke": True}, "smoke")
        # Both siren-capable detectors sound (incl. origin); the sensor-only one
        # is skipped. Each carries the property True + the duration backstop.
        assert {n for n, _ in sent} == {"smoke_foh", "smoke_stage"}
        assert all(p["alarm"] is True and p["max_duration"] == 300 for _, p in sent)
        assert machine._interconnect_active

    asyncio.run(go())


def test_event_mode_sends_sms_but_holds_interconnect(tmp_path):
    # EVENT keeps the venue quiet: the SMS still goes out (we always want to
    # know), but no remote sirens — only the tripped detector's own buzzer.
    machine, sent = _siren_machine(tmp_path)
    texts: list[str] = []

    async def fake_broadcast(msisdns, text, policy):
        texts.append(text)
        return {}

    machine._notifier.broadcast = fake_broadcast
    from app.config import Recipient
    machine.set_recipients([Recipient(name="A", msisdn="4790000000")])

    async def go():
        from datetime import timedelta
        from app.models import now
        await machine._db.init()
        await machine.arm_event(now() + timedelta(hours=1), actor="test")  # -> EVENT
        await machine.on_detector_update("smoke_stage", {"smoke": True}, "smoke")
        assert sent == []
        assert len(texts) == 1 and "Stage" in texts[0]
        assert machine._detectors["smoke_stage"].alarm is True

    asyncio.run(go())


def test_interconnect_disabled_sounds_nothing(tmp_path):
    machine, sent = _siren_machine(tmp_path, interconnect=False)

    async def go():
        await machine._db.init()
        await machine.on_detector_update("smoke_foh", {"smoke": True}, "smoke")
        assert sent == []

    asyncio.run(go())


def test_silence_interconnect_clears_all_sirens(tmp_path):
    machine, sent = _siren_machine(tmp_path)

    async def go():
        await machine._db.init()
        await machine.on_detector_update("smoke_foh", {"smoke": True}, "smoke")
        sent.clear()
        await machine.silence_interconnect(actor="test")
        # Every siren-capable detector gets an explicit off; sensor-only skipped.
        assert {n for n, _ in sent} == {"smoke_foh", "smoke_stage"}
        assert all(p["alarm"] is False for _, p in sent)
        assert not machine._interconnect_active

    asyncio.run(go())


def test_interconnect_shows_sirens_in_snapshot_until_silenced(tmp_path):
    machine, sent = _siren_machine(tmp_path)

    async def go():
        await machine._db.init()
        await machine.on_detector_update("smoke_foh", {"smoke": True}, "smoke")
        await machine.on_detector_update("smoke_foh", {"smoke": False}, "smoke")
        # Detector cleared, but the sirens are still sounding — the dashboard
        # must not read plain "normal".
        snap = machine.snapshot()
        assert snap.sirens_until is not None and snap.sirens_origin == "FoH"
        await machine.silence_interconnect(actor="test")
        assert machine.snapshot().sirens_until is None

    asyncio.run(go())


def test_silence_sends_off_even_without_known_interconnect(tmp_path):
    # After a restart FireWatch forgets it sounded the sirens; Silence must
    # still reach them.
    machine, sent = _siren_machine(tmp_path)

    async def go():
        await machine._db.init()
        await machine.silence_interconnect(actor="test")
        assert {n for n, _ in sent} == {"smoke_foh", "smoke_stage"}

    asyncio.run(go())


def test_silence_repeats_off_for_sleepy_devices(tmp_path):
    machine, sent = _siren_machine(tmp_path)
    machine.SILENCE_RETRY_S = (0.01, 0.02)

    async def go():
        await machine._db.init()
        await machine.silence_interconnect(actor="test")
        await machine._silence_task
        # Immediate OFF plus two retries, to both sirens.
        assert len(sent) == 6 and all(p == {"alarm": False} for _, p in sent)

    asyncio.run(go())


def test_new_trip_cancels_pending_silence_retries(tmp_path):
    machine, sent = _siren_machine(tmp_path)
    machine.SILENCE_RETRY_S = (0.05,)

    async def go():
        await machine._db.init()
        await machine.silence_interconnect(actor="test")
        task = machine._silence_task
        await machine.on_detector_update("smoke_stage", {"smoke": True}, "smoke")
        await asyncio.sleep(0.1)
        assert task.cancelled()
        assert sent[-1][1]["alarm"] is True  # no stale OFF after the new START

    asyncio.run(go())


def _all_clear_machine(tmp_path):
    """Siren machine with a fire recipient and a recorder for every SMS."""
    from app.config import Recipient
    machine, sent = _siren_machine(tmp_path)
    texts: list[tuple[list[str], str]] = []

    async def fake_broadcast(msisdns, text, policy):
        texts.append((list(msisdns), text))
        return {}

    machine._notifier.broadcast = fake_broadcast
    machine.set_recipients([Recipient(name="A", msisdn="4790000000")])
    return machine, texts


def test_all_clear_sent_when_silenced_after_fire(tmp_path):
    machine, texts = _all_clear_machine(tmp_path)

    async def go():
        await machine._db.init()
        await machine.on_detector_update("smoke_foh", {"smoke": True}, "smoke")
        await machine.on_detector_update("smoke_foh", {"smoke": False}, "smoke")
        await machine.tick()
        assert len(texts) == 1  # sirens still sounding: no all-clear yet
        await machine.silence_interconnect(actor="dashboard")
        assert len(texts) == 2
        msisdns, text = texts[1]
        assert msisdns == ["4790000000"]
        assert "ALL CLEAR" in text and "FoH" in text and "silenced" in text
        await machine.tick()
        assert len(texts) == 2  # only once

    asyncio.run(go())


def test_all_clear_waits_while_detector_still_in_alarm(tmp_path):
    machine, texts = _all_clear_machine(tmp_path)

    async def go():
        await machine._db.init()
        await machine.on_detector_update("smoke_foh", {"smoke": True}, "smoke")
        await machine.silence_interconnect(actor="dashboard")
        assert len(texts) == 1  # silenced, but still smoke: not clear
        machine.ALL_CLEAR_HOLD_S = 0
        await machine.on_detector_update("smoke_foh", {"smoke": False}, "smoke")
        await machine.tick()  # starts the quiet hold
        await machine.tick()
        assert len(texts) == 2 and "ALL CLEAR" in texts[1][1]

    asyncio.run(go())


def test_all_clear_after_sirens_time_out(tmp_path):
    from datetime import timedelta
    from app.models import now
    machine, texts = _all_clear_machine(tmp_path)

    async def go():
        await machine._db.init()
        await machine.on_detector_update("smoke_foh", {"smoke": True}, "smoke")
        await machine.on_detector_update("smoke_foh", {"smoke": False}, "smoke")
        machine.sirens_until = now() - timedelta(seconds=1)  # timer ran out
        machine._all_clear_since = now() - timedelta(seconds=machine.ALL_CLEAR_HOLD_S + 1)
        await machine.tick()
        assert len(texts) == 2 and "own timer" in texts[1][1]

    asyncio.run(go())


def test_all_clear_reaches_escalated_recipients_and_stops_escalation(tmp_path):
    from datetime import timedelta
    from app.config import Recipient
    machine, texts = _all_clear_machine(tmp_path)
    machine.set_recipients([
        Recipient(name="A", msisdn="4790000001", priority=1),
        Recipient(name="B", msisdn="4790000002", priority=2),
        Recipient(name="C", msisdn="4790000003", priority=3),
    ])

    async def go():
        await machine._db.init()
        await machine.on_detector_update("smoke_foh", {"smoke": True}, "smoke")
        (key,) = machine._active_alerts
        machine._db.unacked_alerts = lambda k: asyncio.sleep(0, result=["4790000001"])
        machine._active_alerts[key]["sent_at"] -= timedelta(hours=1)
        await machine.check_escalations()  # -> B
        await machine.on_detector_update("smoke_foh", {"smoke": False}, "smoke")
        await machine.silence_interconnect(actor="dashboard")
        assert texts[-1][0] == ["4790000001", "4790000002"]  # not C: never alerted
        assert machine._active_alerts == {}  # C will not be escalated to now

    asyncio.run(go())


def _iso(seconds_ago: float) -> str:
    from datetime import timedelta
    from app.models import now
    return (now() - timedelta(seconds=seconds_ago)).isoformat().replace("+00:00", "Z")


def test_replayed_cached_alarm_is_ignored(tmp_path):
    # Z2M republishes its state cache on startup; a stale smoke=true whose
    # last_seen is old must not trip SMS or sirens — only flag "unconfirmed".
    machine, sent = _siren_machine(tmp_path)

    async def go():
        await machine._db.init()
        await machine.on_detector_update(
            "smoke_foh", {"smoke": True, "last_seen": _iso(3 * 86400)}, "smoke")
        det = machine._detectors["smoke_foh"]
        assert det.alarm is False and det.alarm_unconfirmed is True
        assert sent == []
        kinds = [r["kind"] for r in await machine._db.recent_audit(10)]
        assert "alarm_replay" in kinds and "alarm" not in kinds

        # A fresh report from the device itself is a real alarm.
        await machine.on_detector_update(
            "smoke_foh", {"smoke": True, "last_seen": _iso(0.2)}, "smoke")
        assert det.alarm is True and det.alarm_unconfirmed is False
        assert {n for n, _ in sent} == {"smoke_foh", "smoke_stage"}

    asyncio.run(go())


def test_replay_flag_clears_when_device_reports_clear(tmp_path):
    machine, _ = _siren_machine(tmp_path)

    async def go():
        await machine._db.init()
        await machine.on_detector_update(
            "smoke_foh", {"smoke": True, "last_seen": 1_600_000_000_000}, "smoke")  # epoch ms
        det = machine._detectors["smoke_foh"]
        assert det.alarm_unconfirmed is True
        await machine.on_detector_update("smoke_foh", {"smoke": False, "last_seen": _iso(0)}, "smoke")
        assert det.alarm is False and det.alarm_unconfirmed is False

    asyncio.run(go())


def test_alarm_without_last_seen_still_trips(tmp_path):
    # No last_seen (Z2M option disabled): can't tell a replay apart -> fail safe.
    machine, sent = _siren_machine(tmp_path)

    async def go():
        await machine._db.init()
        await machine.on_detector_update("smoke_foh", {"smoke": True}, "smoke")
        assert machine._detectors["smoke_foh"].alarm is True
        assert sent

    asyncio.run(go())


def _sms_machine(tmp_path):
    machine, _ = _siren_machine(tmp_path)
    texts: list[str] = []

    async def fake_broadcast(msisdns, text, policy):
        texts.append(text)
        return {}

    machine._notifier.broadcast = fake_broadcast
    from app.config import Recipient
    machine.set_recipients([Recipient(name="A", msisdn="4790000000", priority=0)])
    return machine, texts


def test_never_reporting_detector_goes_offline(tmp_path):
    # A detector silent since before FireWatch started must still be flagged
    # once offline_after_hours has passed since we began watching it.
    from datetime import timedelta
    machine, texts = _sms_machine(tmp_path)

    async def go():
        await machine._db.init()
        await machine.tick()
        assert machine._detectors["smoke_foh"].online and texts == []
        machine._supervise_from["smoke_foh"] -= timedelta(hours=25)
        await machine.tick()
        assert machine._detectors["smoke_foh"].online is False
        assert any("FoH" in t and "offline" in t for t in texts)

    asyncio.run(go())


def test_stale_replay_does_not_refresh_last_seen(tmp_path):
    # A Z2M cache replay carrying a 3-day-old last_seen must not make the
    # detector look freshly checked in.
    from datetime import timedelta
    from app.models import now
    machine, texts = _sms_machine(tmp_path)

    async def go():
        await machine._db.init()
        await machine.on_detector_update(
            "smoke_foh", {"smoke": False, "last_seen": _iso(3 * 86400)}, "smoke")
        det = machine._detectors["smoke_foh"]
        assert now() - det.last_seen > timedelta(days=2)
        await machine.tick()
        assert det.online is False
        assert any("FoH" in t and "offline" in t for t in texts)
        # A fresh report brings it straight back online.
        await machine.on_detector_update("smoke_foh", {"smoke": False, "last_seen": _iso(0)}, "smoke")
        assert det.online is True

    asyncio.run(go())
