"""The wall panel's static key opens only its four routes, nothing else.

Driven through the ASGI app without its lifespan, so no DB, MQTT or live
data is touched — only the auth middleware and the read-only state route run.
"""
import asyncio

import httpx

import app.main as main

KEY = "panel-test-key"


def _request(monkeypatch, method, path, headers=None, json=None):
    monkeypatch.setattr(main, "_panel_key", KEY)
    monkeypatch.setattr(main, "_auth_configured", lambda: True)

    async def go():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://fw") as c:
            return await c.request(method, path, headers=headers, json=json)

    return asyncio.run(go())


def test_panel_key_reads_state(monkeypatch):
    r = _request(monkeypatch, "GET", "/api/state", headers={"X-Panel-Key": KEY})
    assert r.status_code == 200 and "mode" in r.json()


def test_wrong_or_missing_panel_key_is_refused(monkeypatch):
    assert _request(monkeypatch, "GET", "/api/state").status_code == 401
    assert _request(monkeypatch, "GET", "/api/state", headers={"X-Panel-Key": "nope"}).status_code == 401


def test_panel_key_does_not_open_other_routes(monkeypatch):
    h = {"X-Panel-Key": KEY}
    assert _request(monkeypatch, "POST", "/api/sms/test", headers=h).status_code == 401
    assert _request(monkeypatch, "POST", "/api/siren/test/on", headers=h).status_code == 401
    assert _request(monkeypatch, "GET", "/api/audit", headers=h).status_code == 401
    # Right path, wrong method.
    assert _request(monkeypatch, "GET", "/api/siren/silence", headers=h).status_code == 401
