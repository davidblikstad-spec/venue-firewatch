"""Tests for WAN status parsing and active-path change notification."""
import asyncio
import json

from app import wan
from app.models import WanAdapter, WanState


def _write(tmp_path, obj):
    p = tmp_path / "status.json"
    p.write_text(json.dumps(obj))
    return str(p)


FRESH = {
    "updated": "2099-01-01T00:00:00Z",
    "active": "eno1",
    "adapters": [
        {"iface": "eno1", "label": "Wired (primary)", "link": True, "internet": True,
         "active": True, "metric": 1000, "ip": "192.168.1.6"},
        {"iface": "wwp0s21f0u3i4", "label": "Cellular (TRM240)", "link": True,
         "internet": True, "active": False, "metric": 4000, "ip": "100.105.132.78"},
    ],
}


def test_label_for():
    assert wan.label_for("eno1") == "Wired (primary)"
    assert wan.label_for("eth0") == "Wired (secondary)"
    assert wan.label_for("wwp0s21f0u3i4") == "Cellular (TRM240)"
    assert wan.label_for("tun9") == "tun9"  # unknown -> raw name


def test_read_status_file_fresh(tmp_path):
    st = wan.read_status_file(_write(tmp_path, FRESH), 120)
    assert st is not None and st.source == "worker"
    assert st.active == "eno1" and len(st.adapters) == 2
    assert st.adapters[0].active and st.adapters[0].internet


def test_read_status_file_stale_rejected(tmp_path):
    old = dict(FRESH, updated="2000-01-01T00:00:00Z")
    assert wan.read_status_file(_write(tmp_path, old), 120) is None


def test_read_status_file_missing():
    assert wan.read_status_file("/no/such/file.json", 120) is None


def test_read_status_file_garbage(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("{ not json")
    assert wan.read_status_file(str(p), 120) is None


# ---- change-detection notifications ----

class _Notifier:
    def __init__(self):
        self.sent = []

    async def broadcast(self, msisdns, text, policy):
        self.sent.append((tuple(msisdns), text))
        return {m: [] for m in msisdns}


def _machine():
    from app.state import StateMachine
    from app.config import Recipient, Settings, YamlConfig

    class _DB:
        async def track_sms(self, *a, **k): pass
        async def audit(self, *a, **k): pass

    cfg = YamlConfig(detectors=[], recipients=[Recipient(name="On-call", msisdn="4711111111")])
    return StateMachine(Settings(), cfg, _DB(), _Notifier())


def _state(active, adapters):
    return WanState(monitored=True, active=active,
                    adapters=[WanAdapter(**a) for a in adapters])


def test_first_reading_does_not_notify():
    m = _machine()
    asyncio.run(m.on_wan_update(_state("eno1", [
        {"iface": "eno1", "label": "Wired (primary)", "link": True, "active": True},
    ])))
    assert m._notifier.sent == []  # first observation is a baseline, not a change


def test_active_change_notifies_all_recipients():
    m = _machine()
    base = [
        {"iface": "eno1", "label": "Wired (primary)", "link": True, "active": True},
        {"iface": "wwp0", "label": "Cellular (TRM240)", "link": True, "active": False},
    ]
    failover = [
        {"iface": "eno1", "label": "Wired (primary)", "link": False, "active": False},
        {"iface": "wwp0", "label": "Cellular (TRM240)", "link": True, "active": True},
    ]

    async def go():
        await m.on_wan_update(_state("eno1", base))      # baseline
        await m.on_wan_update(_state("wwp0", failover))  # failover
    asyncio.run(go())

    assert len(m._notifier.sent) == 1
    msisdns, text = m._notifier.sent[0]
    assert msisdns == ("4711111111",)
    assert "Cellular (TRM240)" in text and "now using" in text


def test_same_active_does_not_notify():
    m = _machine()
    a = [{"iface": "eno1", "label": "Wired (primary)", "link": True, "active": True}]

    async def go():
        await m.on_wan_update(_state("eno1", a))
        await m.on_wan_update(_state("eno1", a))
    asyncio.run(go())
    assert m._notifier.sent == []
