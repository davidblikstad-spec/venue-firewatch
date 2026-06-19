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
