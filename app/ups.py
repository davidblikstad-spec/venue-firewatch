"""UPS monitoring via NUT (Network UPS Tools).

Polls `upsc <ups_name>` and maps the result into a UpsState. Disabled when no
ups_name is configured. Power events raised from here are never silenced by
EVENT mode — a power failure during a show is its own emergency.
"""
from __future__ import annotations

import asyncio
import logging

from .config import Settings
from .models import UpsState, now
from .state import StateMachine

log = logging.getLogger("firewatch.ups")


async def _read_ups(ups_name: str) -> UpsState | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "upsc", ups_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=10)
    except (OSError, asyncio.TimeoutError) as exc:
        log.warning("upsc failed: %s", exc)
        return UpsState(monitored=True, online=False, last_seen=now())

    if proc.returncode != 0:
        log.warning("upsc rc=%s: %s", proc.returncode, err.decode()[:200])
        return UpsState(monitored=True, online=False, last_seen=now())

    fields: dict[str, str] = {}
    for line in out.decode().splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fields[k.strip()] = v.strip()

    status = fields.get("ups.status", "")
    charge = fields.get("battery.charge")
    return UpsState(
        monitored=True,
        online=True,
        on_battery="OB" in status.split(),
        low_battery="LB" in status.split(),
        charge_pct=int(charge) if charge and charge.isdigit() else None,
        raw_status=status or None,
        last_seen=now(),
    )


async def run_ups_poller(settings: Settings, machine: StateMachine) -> None:
    if not settings.ups_name:
        log.info("UPS polling disabled (no FW_UPS_NAME set)")
        return
    while True:
        ups = await _read_ups(settings.ups_name)
        if ups is not None:
            await machine.on_ups_update(ups)
        await asyncio.sleep(settings.ups_poll_seconds)
