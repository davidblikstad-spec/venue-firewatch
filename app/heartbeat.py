"""Off-site watchdog heartbeat.

An external Node-RED watchdog at HEARTBEAT_URL SMS-alerts if it stops hearing
from us — it tolerates one missed beat and alerts after two (~11 min of
silence). Emitting the beat from inside firewatch's own event loop, rather than
from a cron job, means "beats arriving" proves firewatch is actually running —
not merely that the box has power.

Mirrors the resilient-loop style of run_ups_poller/run_wan_poller: a single
exception must never escape and kill the task.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import httpx
from pydantic import BaseModel

from .config import Settings
from .models import now

log = logging.getLogger("firewatch.heartbeat")

_TIMEOUT_S = 10.0
_OK_BODY = "ok"
_MIN_INTERVAL_S = 5  # floor so a misconfigured interval can't busy-loop


class HeartbeatStatus(BaseModel):
    """Small, JSON-able status of the heartbeat emitter. Deliberately holds NO
    token — it is surfaced verbatim by GET /api/watchdog/status."""

    enabled: bool
    url: str
    interval: int
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_status_code: int | None = None
    last_response: str | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    seconds_to_next_beat: int | None = None


class HeartbeatMonitor:
    """Beats every `heartbeat_interval` seconds and remembers the last result."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._next_beat_at: datetime | None = None
        self.status = HeartbeatStatus(
            enabled=settings.heartbeat_enabled,
            url=settings.heartbeat_url,
            interval=settings.heartbeat_interval,
        )

    def snapshot(self) -> dict:
        """Status as a JSON-able dict with a live countdown to the next beat.
        Never contains the token."""
        self.status.enabled = self._s.heartbeat_enabled
        self.status.url = self._s.heartbeat_url
        self.status.interval = self._s.heartbeat_interval
        secs = None
        if self._next_beat_at is not None:
            secs = max(0, round((self._next_beat_at - now()).total_seconds()))
        data = self.status.model_dump(mode="json")
        data["seconds_to_next_beat"] = secs
        return data

    async def beat_once(self) -> dict:
        """Send a single heartbeat and fold the outcome into the status.

        Success = HTTP 200 with body "ok"; HTTP 403 means the token is wrong.
        Catches everything so a failed beat is recorded, never raised.
        """
        self.status.last_attempt_at = now()
        headers = {"X-Token": self._s.heartbeat_token or ""}
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
                resp = await client.post(self._s.heartbeat_url, headers=headers)
            body = (resp.text or "").strip()
            self.status.last_status_code = resp.status_code
            self.status.last_response = body[:200]
            if resp.status_code == 200 and body == _OK_BODY:
                self.status.last_success_at = now()
                self.status.last_error = None
                self.status.consecutive_failures = 0
            else:
                self.status.consecutive_failures += 1
                if resp.status_code == 403:
                    self.status.last_error = "403 — token rejected by watchdog"
                else:
                    self.status.last_error = (
                        f"unexpected response: {resp.status_code} {body[:60]!r}"
                    )
                log.warning("heartbeat unhealthy: %s", self.status.last_error)
        except Exception as exc:  # timeout, DNS, refused… must never crash firewatch
            self.status.last_status_code = None
            self.status.last_response = None
            self.status.consecutive_failures += 1
            self.status.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("heartbeat failed: %s", self.status.last_error)
        return self.snapshot()

    async def run(self) -> None:
        """Background task: beat immediately, then every interval. Stops when
        firewatch's event loop stops (the task is cancelled at lifespan exit)."""
        if self._s.heartbeat_enabled and not self._s.heartbeat_token:
            log.warning(
                "heartbeat enabled but FW_HEARTBEAT_TOKEN is unset — the watchdog "
                "will likely reject beats with 403"
            )
        while True:
            interval = max(_MIN_INTERVAL_S, self._s.heartbeat_interval)
            if not self._s.heartbeat_enabled:
                self.status.enabled = False
                self._next_beat_at = None
                # Re-check periodically in case it is toggled on at runtime.
                await asyncio.sleep(min(60, interval))
                continue
            await self.beat_once()
            self._next_beat_at = now() + timedelta(seconds=interval)
            await asyncio.sleep(interval)
