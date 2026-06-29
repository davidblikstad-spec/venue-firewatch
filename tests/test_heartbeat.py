"""Tests for the off-site watchdog heartbeat emitter.

httpx is faked so nothing leaves the box; we assert the status object reflects
each outcome (success / wrong token / bad body / network error) and that the
token is sent as a header but never surfaced in the status snapshot.
"""
import asyncio

import httpx

import app.heartbeat as hb
from app.config import Settings
from app.heartbeat import HeartbeatMonitor


class _Resp:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


class _FakeClient:
    def __init__(self, resp=None, exc=None, capture=None):
        self._resp, self._exc, self._capture = resp, exc, capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None):
        if self._capture is not None:
            self._capture["url"] = url
            self._capture["headers"] = headers
        if self._exc:
            raise self._exc
        return self._resp


def _patch(monkeypatch, *, resp=None, exc=None, capture=None):
    monkeypatch.setattr(
        hb.httpx, "AsyncClient",
        lambda *a, **k: _FakeClient(resp=resp, exc=exc, capture=capture),
    )


def _mon(**kw):
    return HeartbeatMonitor(Settings(heartbeat_token="secret123", **kw))


def test_heartbeat_success_sends_token_and_records(monkeypatch):
    cap = {}
    _patch(monkeypatch, resp=_Resp(200, "ok\n"), capture=cap)
    m = _mon()

    async def go():
        s = await m.beat_once()
        assert cap["headers"]["X-Token"] == "secret123"   # token IS sent…
        assert s["last_status_code"] == 200
        assert s["last_success_at"] is not None
        assert s["last_error"] is None
        assert s["consecutive_failures"] == 0
        assert "token" not in s and "secret123" not in str(s)  # …but never returned

    asyncio.run(go())


def test_heartbeat_403_flags_token(monkeypatch):
    _patch(monkeypatch, resp=_Resp(403, "forbidden"))
    m = _mon()

    async def go():
        s = await m.beat_once()
        assert s["last_status_code"] == 403
        assert "403" in s["last_error"]
        assert s["last_success_at"] is None
        assert s["consecutive_failures"] == 1

    asyncio.run(go())


def test_heartbeat_unexpected_body_is_failure(monkeypatch):
    _patch(monkeypatch, resp=_Resp(200, "maintenance"))
    m = _mon()

    async def go():
        s = await m.beat_once()
        assert s["last_success_at"] is None
        assert s["consecutive_failures"] == 1
        assert "unexpected" in s["last_error"]

    asyncio.run(go())


def test_heartbeat_network_error_never_raises(monkeypatch):
    _patch(monkeypatch, exc=httpx.ConnectError("no route"))
    m = _mon()

    async def go():
        s = await m.beat_once()  # must not raise
        assert s["last_status_code"] is None
        assert s["consecutive_failures"] == 1
        assert "ConnectError" in s["last_error"]

    asyncio.run(go())


def test_consecutive_failures_reset_on_recovery(monkeypatch):
    m = _mon()

    async def go():
        _patch(monkeypatch, exc=httpx.ConnectError("down"))
        await m.beat_once()
        await m.beat_once()
        assert m.status.consecutive_failures == 2
        _patch(monkeypatch, resp=_Resp(200, "ok"))
        s = await m.beat_once()
        assert s["consecutive_failures"] == 0 and s["last_error"] is None

    asyncio.run(go())


def test_disabled_snapshot_has_no_token_and_no_countdown():
    m = HeartbeatMonitor(Settings(heartbeat_enabled=False, heartbeat_token="secret123"))
    s = m.snapshot()
    assert s["enabled"] is False
    assert s["seconds_to_next_beat"] is None
    assert "secret123" not in str(s)
