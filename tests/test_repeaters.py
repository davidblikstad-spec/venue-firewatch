"""Tests for mesh-repeater (smart plug) availability alerting.

Repeaters are surfaced on the dashboard and, once Z2M availability is enabled,
raise an SMS when they drop off the mesh. The tricky parts — not alerting on the
cold-start baseline, and only sending a recovery notice for a repeater we
actually reported down — are pinned here without standing up MQTT.
"""
import asyncio
import json

from app.config import Recipient, Settings, YamlConfig
from app.db import Database
from app.mqtt_client import DeviceRegistry, MqttBridge, _parse_online
from app.notify import Notifier
from app.state import StateMachine


def _machine(tmp_path):
    settings = Settings(db_path=str(tmp_path / "t.db"))
    cfg = YamlConfig(recipients=[Recipient(name="A", msisdn="4790000000", priority=0)])
    machine = StateMachine(settings, cfg, Database(settings.db_path), Notifier(settings))
    sent: list[str] = []

    async def fake_broadcast(msisdns, text, policy):
        sent.append(text)
        return {}

    machine._notifier.broadcast = fake_broadcast
    return machine, sent


def _age_pending(machine, name, seconds=600):
    from datetime import timedelta
    machine._mesh_pending_down[name] -= timedelta(seconds=seconds)


def test_parse_online_both_forms():
    assert _parse_online(b'{"state":"online"}') is True
    assert _parse_online(b'{"state":"offline"}') is False
    assert _parse_online(b"online") is True
    assert _parse_online(b'"offline"') is False
    assert _parse_online(b"garbage") is False


def test_offline_then_recovery_sends_sms(tmp_path):
    machine, sent = _machine(tmp_path)

    async def go():
        await machine._db.init()
        # First reading is the baseline — never alerts.
        await machine.on_mesh_availability("plug1", True)
        assert sent == []
        # Going down raises a warning naming the repeater — once it has stayed
        # down past the grace period.
        await machine.on_mesh_availability("plug1", False)
        await machine.tick()
        assert sent == []
        _age_pending(machine, "plug1")
        await machine.tick()
        assert len(sent) == 1 and "plug1" in sent[0]
        assert not machine.snapshot().mesh[0].online
        # Coming back sends exactly one recovery notice.
        await machine.on_mesh_availability("plug1", True)
        assert len(sent) == 2 and "plug1" in sent[1]
        assert machine.snapshot().mesh[0].online

    asyncio.run(go())


def test_no_alert_on_coldstart_offline(tmp_path):
    machine, sent = _machine(tmp_path)

    async def go():
        await machine._db.init()
        # A repeater already down when we first hear about it is the baseline:
        # no alert, and no spurious recovery when the same state repeats.
        await machine.on_mesh_availability("plug1", False)
        await machine.on_mesh_availability("plug1", False)
        assert sent == []

    asyncio.run(go())


def test_availability_ignores_non_router_and_disables_last_seen_heuristic(tmp_path):
    machine, _ = _machine(tmp_path)
    bridge = MqttBridge(machine._s, machine._cfg, machine, DeviceRegistry())
    inv = [
        {"friendly_name": "plug1", "type": "Router", "power_source": "Mains (single phase)",
         "definition": {"vendor": "Namron", "model": "4512766", "exposes": []}},
        {"friendly_name": "smoke1", "type": "EndDevice", "power_source": "Battery",
         "definition": {"vendor": "Develco", "model": "SMSZB-120",
                        "exposes": [{"type": "binary", "property": "smoke", "access": 1}]}},
    ]

    async def go():
        await machine._db.init()
        await bridge._handle("zigbee2mqtt/bridge/devices", json.dumps(inv).encode())
        # Detector availability must NOT create a mesh entry (detectors have their
        # own supervision). Only the router shows on the mesh card.
        await bridge._handle("zigbee2mqtt/smoke1/availability", b'{"state":"offline"}')
        assert [d.friendly_name for d in machine.snapshot().mesh] == ["plug1"]
        # An availability baseline means the last_seen tick() fallback is skipped
        # for this device (availability owns the online flag).
        await bridge._handle("zigbee2mqtt/plug1/availability", b'{"state":"online"}')
        assert "plug1" in machine._mesh_avail_seen

    asyncio.run(go())


def test_brief_offline_blip_sends_nothing(tmp_path):
    # Z2M flaps every repeater offline->online at startup; that must stay silent.
    machine, sent = _machine(tmp_path)

    async def go():
        await machine._db.init()
        await machine.on_mesh_availability("plug1", True)
        await machine.on_mesh_availability("plug1", False)
        await machine.on_mesh_availability("plug1", True)
        await machine.tick()
        assert sent == [] and machine._mesh_pending_down == {}
        assert machine.snapshot().mesh[0].online

    asyncio.run(go())
