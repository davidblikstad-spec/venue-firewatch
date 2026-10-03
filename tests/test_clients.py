"""Tests for the dashboard websocket fan-out (_Clients.push).

Every send_json awaits, so a dashboard that disconnects mid-broadcast runs its
handler's remove() while push is still iterating. Pushing over a snapshot of the
set keeps that from aborting the broadcast for the dashboards still connected.
"""
import asyncio

from app.main import _Clients


class _FakeSocket:
    """Records what it was sent. on_send runs mid-broadcast, like a handler
    waking up on the other side of the await."""

    def __init__(self, on_send=None, fail=False):
        self.sent = []
        self._on_send, self._fail = on_send, fail

    async def send_json(self, msg):
        await asyncio.sleep(0)  # yield, as a real send does
        if self._on_send:
            self._on_send()
        if self._fail:
            raise RuntimeError("socket closed")
        self.sent.append(msg)


class _FakeSnap:
    def model_dump(self, mode="json"):
        return {"mode": "normal"}


def test_push_survives_disconnect_mid_broadcast():
    c = _Clients()
    quitter = _FakeSocket()
    survivors = [_FakeSocket() for _ in range(3)]
    # The first socket to be sent to drops itself from the set mid-loop.
    quitter._on_send = lambda: c.remove(quitter)
    for ws in [quitter, *survivors]:
        c.add(ws)

    asyncio.run(c.push(_FakeSnap()))

    # No RuntimeError, and everyone still connected got the snapshot.
    for ws in survivors:
        assert len(ws.sent) == 1
        assert ws.sent[0]["type"] == "snapshot"
        assert ws.sent[0]["data"] == {"mode": "normal"}


def test_push_drops_failed_sockets():
    c = _Clients()
    good, bad = _FakeSocket(), _FakeSocket(fail=True)
    c.add(good)
    c.add(bad)

    asyncio.run(c.push(_FakeSnap()))
    assert len(good.sent) == 1

    # The broken one is gone, so the next push only reaches the good socket.
    asyncio.run(c.push(_FakeSnap()))
    assert len(good.sent) == 2
    assert bad.sent == []
