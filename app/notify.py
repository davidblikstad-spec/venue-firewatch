"""SMS notification.

Two transports:
  - GatewayApiTransport  — primary. HTTPS POST to GatewayAPI. Works over the
    venue WAN, or automatically over the TRM240's cellular *data* if the OS
    has failed the route over. A 200 means "accepted", not "delivered".
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
from dataclasses import dataclass

import httpx

from .config import Settings
from .models import SmsPolicy

log = logging.getLogger("firewatch.notify")


@dataclass
class SendResult:
    transport: str
    ok: bool
    detail: str


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
        # NOTE: confirm the exact path/payload from your GatewayAPI dashboard,
        # which shows a pre-filled snippet for your account. This uses the
        # token-in-Authorization-header style.
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
                return SendResult(self.name, True, f"accepted ({resp.status_code})")
            return SendResult(self.name, False, f"http {resp.status_code}: {resp.text[:200]}")
        except (httpx.HTTPError, ValueError) as exc:
            return SendResult(self.name, False, f"error: {exc}")


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
