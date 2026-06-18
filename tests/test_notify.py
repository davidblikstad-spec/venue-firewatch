"""Tests for the SMS policy logic — the part worth getting right.

Run: pytest -q
These use a fake Notifier so no real SMS or network is touched.
"""
import asyncio

from app.models import SmsPolicy
from app.notify import Notifier, SendResult


def _patch(notifier, primary_ok, secondary_ok):
    async def p(msisdn, text):
        return SendResult("gatewayapi", primary_ok, "ok" if primary_ok else "fail")

    async def s(msisdn, text):
        return SendResult("trm240", secondary_ok, "ok" if secondary_ok else "fail")

    notifier._primary.send = p
    notifier._secondary.send = s


def _notifier():
    # Settings not used by the patched transports, so pass a minimal stand-in.
    class _S:
        gatewayapi_token = "x"; gatewayapi_base_url = "http://x"
        gatewayapi_sender = "T"; gatewayapi_timeout_s = 1
        modem_enabled = True; modem_send_cmd = "true"
    n = Notifier.__new__(Notifier)
    from app.notify import GatewayApiTransport, ModemTransport
    n._primary = GatewayApiTransport(_S()); n._secondary = ModemTransport(_S())
    return n


def test_failover_stops_when_primary_succeeds():
    n = _notifier(); _patch(n, primary_ok=True, secondary_ok=True)
    res = asyncio.run(n.send_one("4799999999", "hi", SmsPolicy.FAILOVER))
    assert len(res) == 1 and res[0].transport == "gatewayapi" and res[0].ok


def test_failover_uses_modem_when_primary_fails():
    n = _notifier(); _patch(n, primary_ok=False, secondary_ok=True)
    res = asyncio.run(n.send_one("4799999999", "hi", SmsPolicy.FAILOVER))
    assert len(res) == 2 and res[1].transport == "trm240" and res[1].ok


def test_both_always_sends_twice():
    n = _notifier(); _patch(n, primary_ok=True, secondary_ok=True)
    res = asyncio.run(n.send_one("4799999999", "hi", SmsPolicy.BOTH))
    assert {r.transport for r in res} == {"gatewayapi", "trm240"}
