"""SMS notification.

Two transports:
  - GatewayApiTransport  — primary. HTTPS POST to GatewayAPI. Works over the
    venue WAN, or automatically over the TRM240's cellular *data* if the OS
    has failed the route over. A 200 means "accepted", not "delivered".
    Delivery receipts arrive via the /api/sms/dlr webhook.
  - ModemTransport       — secondary. Native cellular SMS via the TRM240.
    Needs no IP at all, so it is the true last resort.

Policy (operator-selectable, persisted):
  - FAILOVER: try GatewayAPI; on any error/timeout, send via the modem.
  - BOTH:     fire both at once (recipients may get two texts; that is the
              accepted trade for redundancy).

Every attempt and outcome is written to the audit log by the caller.
"""
from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass, field

import httpx

from .config import Settings
from .models import SmsPolicy

log = logging.getLogger("firewatch.notify")


@dataclass
class SendResult:
    transport: str
    ok: bool
    detail: str
    message_id: str | None = None


class GatewayApiTransport:
    name = "gatewayapi"

    def __init__(self, settings: Settings) -> None:
        self._s = settings

    @property
    def configured(self) -> bool:
        return bool(self._s.gatewayapi_token)

    async def send(self, msisdn: str, text: str) -> SendResult:
        if not self.configured:
            return SendResult(self.name, False, "no token configured")
        url = f"{self._s.gatewayapi_base_url.rstrip('/')}/rest/mtsms"
        payload = {
            "sender": self._s.gatewayapi_sender,
            "message": text,
            "recipients": [{"msisdn": int(msisdn)}],
        }
        headers = {"Authorization": f"Token {self._s.gatewayapi_token}"}
        try:
            async with httpx.AsyncClient(timeout=self._s.gatewayapi_timeout_s) as client:
                resp = await client.post(url, json=payload, headers=headers)
            if 200 <= resp.status_code < 300:
                body = resp.json() if resp.text else {}
                msg_id = str(body.get("ids", [None])[0] or body.get("id", ""))
                return SendResult(self.name, True, f"accepted ({resp.status_code})", message_id=msg_id or None)
            return SendResult(self.name, False, f"http {resp.status_code}: {resp.text[:200]}")
        except (httpx.HTTPError, ValueError) as exc:
            return SendResult(self.name, False, f"error: {exc}")

    async def check_balance(self) -> float | None:
        """Return remaining SMS credit balance, or None on failure."""
        if not self.configured:
            return None
        url = f"{self._s.gatewayapi_base_url.rstrip('/')}/rest/me"
        headers = {"Authorization": f"Token {self._s.gatewayapi_token}"}
        try:
            async with httpx.AsyncClient(timeout=self._s.gatewayapi_timeout_s) as client:
                resp = await client.get(url, headers=headers)
            if 200 <= resp.status_code < 300:
                data = resp.json()
                credit = data.get("credit", data.get("balance"))
                return float(credit) if credit is not None else None
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            log.warning("balance check failed: %s", exc)
        return None


class ModemTransport:
    name = "trm240"

    def __init__(self, settings: Settings) -> None:
        self._s = settings

    @property
    def configured(self) -> bool:
        return self._s.modem_enabled

    async def send(self, msisdn: str, text: str) -> SendResult:
        if not self.configured:
            return SendResult(self.name, False, "modem disabled")
        if self._s.modem_send_cmd:
            return await self._send_via_cmd(msisdn, text)
        return await self._send_via_at(msisdn, text)

    async def _send_via_at(self, msisdn: str, text: str) -> SendResult:
        """Built-in path: talk AT/PDU to the modem's serial port (no gammu)."""
        from . import modem_sms
        try:
            detail = await asyncio.wait_for(
                asyncio.to_thread(modem_sms.send_sms, msisdn, text, self._s.modem_port),
                timeout=60,
            )
            return SendResult(self.name, True, detail)
        except asyncio.TimeoutError:
            return SendResult(self.name, False, "modem timeout")
        except (modem_sms.ModemError, OSError) as exc:
            return SendResult(self.name, False, f"error: {exc}")

    async def _send_via_cmd(self, msisdn: str, text: str) -> SendResult:
        """Override path: shell out to an operator-configured command."""
        cmd = self._s.modem_send_cmd.format(to=shlex.quote(msisdn), text=text)
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=45)
            if proc.returncode == 0:
                return SendResult(self.name, True, "sent")
            return SendResult(self.name, False, f"rc={proc.returncode}: {err.decode()[:200]}")
        except asyncio.TimeoutError:
            return SendResult(self.name, False, "modem timeout")
        except OSError as exc:
            return SendResult(self.name, False, f"error: {exc}")


class Notifier:
    def __init__(self, settings: Settings) -> None:
        self._primary = GatewayApiTransport(settings)
        self._secondary = ModemTransport(settings)

    async def send_one(self, msisdn: str, text: str, policy: SmsPolicy) -> list[SendResult]:
        if policy is SmsPolicy.BOTH:
            results = await asyncio.gather(
                self._primary.send(msisdn, text),
                self._secondary.send(msisdn, text),
            )
            return list(results)

        # FAILOVER
        first = await self._primary.send(msisdn, text)
        if first.ok:
            return [first]
        log.warning("GatewayAPI failed (%s); falling back to modem", first.detail)
        second = await self._secondary.send(msisdn, text)
        return [first, second]

    async def broadcast(
        self, recipients: list[str], text: str, policy: SmsPolicy
    ) -> dict[str, list[SendResult]]:
        out: dict[str, list[SendResult]] = {}
        for msisdn in recipients:
            out[msisdn] = await self.send_one(msisdn, text, policy)
        return out

    async def send_via_modem(self, recipients: list[str], text: str) -> dict[str, list[SendResult]]:
        """Send to everyone over the TRM240 modem only — used for the failover
        notice, where the primary GatewayAPI path is already known to be down."""
        return {msisdn: [await self._secondary.send(msisdn, text)] for msisdn in recipients}
