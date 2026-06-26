"""Internet uplink (WAN) monitoring for the dashboard + failover SMS.

Two sources, in order of preference:

  1. The root failover worker (`/usr/local/sbin/wan-failover.sh`, installed by
     setup-wan-failover.sh) writes a JSON status file. Only root can probe each
     interface accurately (SO_BINDTODEVICE), so when that file is fresh we trust
     it verbatim — including per-adapter "internet" health.

  2. If the file is missing/stale (e.g. the worker isn't installed yet), we
     derive what we can with zero privileges: which interface the kernel is
     using right now (`ip route get`), each candidate's carrier/IP, and whether
     the box has internet at all. Backup adapters can't be probed unprivileged,
     so their `internet` is left False and the UI shows them as "standby".

The poller mirrors run_ups_poller: it just feeds StateMachine.on_wan_update.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .models import WanAdapter, WanState, now

log = logging.getLogger("firewatch.wan")

# Friendly labels by interface-name prefix. First match wins.
_LABELS: list[tuple[str, str]] = [
    ("eno1", "Wired (primary)"),
    ("eth0", "Wired (secondary)"),
    ("eth1", "Wired (secondary)"),
    ("ww", "Cellular (TRM240)"),
]
_PROBE = ("1.1.1.1", 443)


def label_for(iface: str) -> str:
    for prefix, label in _LABELS:
        if iface.startswith(prefix):
            return label
    return iface


async def _run(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    out, _ = await proc.communicate()
    return out.decode(errors="replace")


# ---- source 1: the root worker's status file --------------------------------

def read_status_file(path: str, max_age_s: int) -> WanState | None:
    p = Path(path)
    try:
        raw = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    try:
        updated = _parse_ts(raw.get("updated"))
        if updated is not None:
            age = (now() - updated).total_seconds()
            if age > max_age_s:
                log.debug("wan status file is stale (%.0fs); falling back to local", age)
                return None
        adapters = [
            WanAdapter(
                iface=a["iface"],
                label=a.get("label") or label_for(a["iface"]),
                link=bool(a.get("link")),
                internet=bool(a.get("internet")),
                active=bool(a.get("active")),
                metric=a.get("metric"),
                ip=a.get("ip"),
            )
            for a in raw.get("adapters", [])
        ]
    except (KeyError, TypeError):
        return None
    return WanState(
        monitored=True, active=raw.get("active"), source="worker",
        adapters=adapters, updated=updated or now(),
    )


def _parse_ts(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---- source 2: root-free local derivation -----------------------------------

def _tcp_ok(source_ip: str | None = None, timeout: float = 3.0) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        if source_ip:
            s.bind((source_ip, 0))
        s.connect(_PROBE)
        return True
    except OSError:
        return False
    finally:
        s.close()


async def derive_local() -> WanState:
    # Default routes (dev + metric) — these are our candidate uplinks.
    routes = json.loads(await _run("ip", "-j", "route", "show", "default") or "[]")
    addrs = json.loads(await _run("ip", "-j", "addr") or "[]")

    ip_by_iface: dict[str, str] = {}
    carrier_by_iface: dict[str, bool] = {}
    for a in addrs:
        ifn = a.get("ifname")
        if not ifn or ifn == "lo":
            continue
        for info in a.get("addr_info", []):
            if info.get("family") == "inet":
                ip_by_iface[ifn] = info.get("local")
                break
        # Wired ports report UP/DOWN by carrier; wwan/point-to-point links sit at
        # UNKNOWN even when attached, so treat UNKNOWN-with-an-IP as up too.
        state = a.get("operstate")
        carrier_by_iface[ifn] = state == "UP" or (state == "UNKNOWN" and ifn in ip_by_iface)

    metric_by_iface: dict[str, int | None] = {}
    for r in routes:
        dev = r.get("dev")
        if dev:
            metric_by_iface.setdefault(dev, r.get("metric"))

    # Which interface is the kernel actually using for internet right now?
    active_iface = None
    parts = (await _run("ip", "route", "get", _PROBE[0])).split()
    if "dev" in parts:
        active_iface = parts[parts.index("dev") + 1]

    overall = await asyncio.to_thread(_tcp_ok)  # internet at all (via active path)

    # Candidate set: anything with a default route, plus known WAN names that
    # are present (so a cabled-but-no-route port still shows as "down/standby").
    candidates = set(metric_by_iface) | {
        i for i in carrier_by_iface if i.startswith(("eno", "eth", "ww"))
    }
    order = {"eno1": 0, "eth0": 1, "eth1": 2}
    adapters = []
    for ifn in sorted(candidates, key=lambda i: (order.get(i, 5), i)):
        is_active = ifn == active_iface
        adapters.append(WanAdapter(
            iface=ifn, label=label_for(ifn),
            link=carrier_by_iface.get(ifn, False),
            internet=bool(is_active and overall),  # only the active path is verifiable unprivileged
            active=is_active,
            metric=metric_by_iface.get(ifn),
            ip=ip_by_iface.get(ifn),
        ))
    return WanState(monitored=True, active=active_iface, source="local",
                    adapters=adapters, updated=now())


async def get_wan_state(settings: Settings) -> WanState:
    fromfile = read_status_file(settings.wan_status_path, settings.wan_status_max_age_s)
    if fromfile is not None:
        return fromfile
    return await derive_local()


async def run_wan_poller(settings: Settings, machine) -> None:
    while True:
        try:
            wan = await get_wan_state(settings)
            await machine.on_wan_update(wan)
        except Exception:  # never let monitoring kill the loop
            log.exception("wan poll failed")
        await asyncio.sleep(settings.wan_poll_seconds)
