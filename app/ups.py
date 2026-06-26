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

    def _int(key: str) -> int | None:
        v = fields.get(key)
        try:
            return int(float(v)) if v is not None else None
        except ValueError:
            return None

    def _float(key: str) -> float | None:
        v = fields.get(key)
        try:
            return float(v) if v is not None else None
        except ValueError:
            return None

    status = fields.get("ups.status", "")
    return UpsState(
        monitored=True,
        online=True,
        on_battery="OB" in status.split(),
        low_battery="LB" in status.split(),
        charge_pct=_int("battery.charge"),
        grid_voltage=_float("input.voltage"),
        load_pct=_int("ups.load"),
        runtime_s=_int("battery.runtime"),
        raw_status=status or None,
        last_seen=now(),
    )


async def scan_ups(host: str = "localhost") -> dict:
    """List UPSes a NUT server is serving via `upsc -l <host>`.

    Returns names already qualified as ``name@host`` so they drop straight into
    the poller. ``installed`` is False when the `upsc` binary is missing (NUT not
    installed yet); ``reachable`` is False when upsd isn't answering on `host`.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "upsc", "-l", host,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=10)
    except FileNotFoundError:
        return {"installed": False, "reachable": False, "ups": [], "error": "NUT (upsc) is not installed on this host."}
    except (OSError, asyncio.TimeoutError) as exc:
        return {"installed": True, "reachable": False, "ups": [], "error": f"scan failed: {exc}"}

    if proc.returncode != 0:
        return {"installed": True, "reachable": False, "ups": [],
                "error": err.decode().strip()[:200] or f"upsc rc={proc.returncode}"}

    names = [ln.strip() for ln in out.decode().splitlines() if ln.strip()]
    return {"installed": True, "reachable": True,
            "ups": [f"{n}@{host}" for n in names]}


async def run_ups_poller(settings: Settings, machine: StateMachine) -> None:
    # Re-read settings.ups_name each pass instead of bailing out once: the name
    # can be set or cleared at runtime from the dashboard. While unconfigured we
    # idle and re-check, so enabling it starts polling within a few seconds.
    while True:
        name = settings.ups_name
        if name:
            ups = await _read_ups(name)
            if ups is not None:
                await machine.on_ups_update(ups)
            await asyncio.sleep(settings.ups_poll_seconds)
        else:
            await asyncio.sleep(5)
