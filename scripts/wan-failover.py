#!/usr/bin/env python3
"""Unified WAN failover worker for the FireWatch box (runs as root via timer).

Manages three internet uplinks in priority order:
    1. eno1  — wired primary
    2. eth0  — wired secondary (auto-joins when a cable is plugged; dhcpcd)
    3. TRM240 cellular (Quectel EC21, QMI on /dev/cdc-wdm0) — last resort

What it does each cycle (~every 30s):
  * Ensures the cellular data session is up (QMI) and has an IP/route.
  * Probes REAL internet on each interface using SO_BINDTODEVICE — only root
    can do this accurately, which is the whole reason this runs privileged.
  * Fails over to cellular when no wired uplink has working internet (by
    lowering the cellular route metric below the wired ones), and fails back
    when a wired uplink recovers. It only ever touches the cellular route it
    owns — dhcpcd remains the authority for the wired routes, so the LAN/
    dashboard is never at risk.
  * Writes /run/wan-failover/status.json for the dashboard to display and to
    drive the "internet path changed" SMS.

Wired link-down failover (cable unplugged) is handled natively by dhcpcd via
the per-interface metrics set in /etc/dhcpcd.conf by setup-wan-failover.sh.
Stdlib only; no third-party packages.
"""
from __future__ import annotations

import datetime
import json
import os
import socket
import subprocess
import sys

WDM = "/dev/cdc-wdm0"
APN = "telia"
CELL_BACKUP = 4000      # cellular route metric while a wired uplink is healthy
CELL_PRIMARY = 500      # cellular route metric while failed over (beats wired 1000/2000)
WIRED = ["eno1", "eth0"]            # priority order
PROBE_HOSTS = [("1.1.1.1", 443), ("8.8.8.8", 443)]
STATUS_PATH = "/run/wan-failover/status.json"
LABELS = {"eno1": "Wired (primary)", "eth0": "Wired (secondary)",
          "eth1": "Wired (secondary)"}


def sh(*args: str, timeout: int = 25) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout).stdout
    except (subprocess.SubprocessError, OSError):
        return ""


def label_for(iface: str) -> str:
    if iface.startswith("ww"):
        return "Cellular (TRM240)"
    return LABELS.get(iface, iface)


def cell_iface() -> str | None:
    for name in sorted(os.listdir("/sys/class/net")):
        if name.startswith("ww"):
            return name
    return None


def probe(iface: str, timeout: float = 3.0) -> bool:
    """True if we can open a TCP connection to a probe host *through* iface."""
    for host, port in PROBE_HOSTS:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, iface.encode())
            s.settimeout(timeout)
            s.connect((host, port))
            return True
        except OSError:
            continue
        finally:
            s.close()
    return False


def ip_json(*args: str):
    out = sh("ip", "-j", *args)
    try:
        return json.loads(out) if out.strip() else []
    except ValueError:
        return []


def iface_ip(iface: str) -> str | None:
    for a in ip_json("addr", "show", iface):
        for info in a.get("addr_info", []):
            if info.get("family") == "inet":
                return info.get("local")
    return None


def iface_link(iface: str) -> bool:
    try:
        st = open(f"/sys/class/net/{iface}/operstate").read().strip()
    except OSError:
        return False
    # Wired ports use up/down by carrier; wwan sits at "unknown" when attached.
    return st == "up" or (st == "unknown" and iface_ip(iface) is not None)


def default_metric(iface: str) -> int | None:
    for r in ip_json("route", "show", "default", "dev", iface):
        return r.get("metric")
    return None


def active_iface() -> str | None:
    parts = sh("ip", "route", "get", "1.1.1.1").split()
    if "dev" in parts:
        return parts[parts.index("dev") + 1]
    return None


# ---- cellular bring-up (QMI) -------------------------------------------------

def ensure_cellular(iface: str) -> None:
    raw = f"/sys/class/net/{iface}/qmi/raw_ip"
    try:
        if open(raw).read().strip() != "Y":
            sh("ip", "link", "set", iface, "down")
            with open(raw, "w") as f:
                f.write("Y")
    except OSError:
        pass
    sh("ip", "link", "set", iface, "up")

    status = sh("qmi-network", WDM, "status")
    if "connected" not in status.lower():
        sh("qmi-network", WDM, "start", timeout=40)

    settings = sh("qmicli", "-d", WDM, "--device-open-proxy",
                  "--wds-get-current-settings")
    ip = mask = gw = None
    for line in settings.splitlines():
        line = line.strip()
        if line.startswith("IPv4 address:"):
            ip = line.split(":", 1)[1].strip()
        elif line.startswith("IPv4 subnet mask:"):
            mask = line.split(":", 1)[1].strip()
        elif line.startswith("IPv4 gateway address:"):
            gw = line.split(":", 1)[1].strip()
    if not ip or not gw:
        return None
    prefix = sum(bin(int(o)).count("1") for o in (mask or "255.255.255.0").split("."))

    if iface_ip(iface) != ip:
        sh("ip", "addr", "flush", "dev", iface)
        sh("ip", "addr", "add", f"{ip}/{prefix}", "dev", iface)
    return gw


def set_cell_route(iface: str, gw: str, metric: int) -> None:
    if default_metric(iface) == metric:
        return
    # Add the new one before removing the old so there's never a gap.
    sh("ip", "route", "replace", "default", "via", gw, "dev", iface, "metric", str(metric))
    for r in ip_json("route", "show", "default", "dev", iface):
        if r.get("metric") not in (metric, None):
            sh("ip", "route", "del", "default", "via", gw, "dev", iface,
               "metric", str(r["metric"]))


# ---- main --------------------------------------------------------------------

def main() -> int:
    cell = cell_iface()
    cell_gw = ensure_cellular(cell) if cell else None

    adapters = []
    wired_internet = False
    for ifn in WIRED:
        if not os.path.exists(f"/sys/class/net/{ifn}"):
            continue
        link = iface_link(ifn)
        net = probe(ifn) if link else False
        wired_internet = wired_internet or net
        adapters.append({"iface": ifn, "label": label_for(ifn), "link": link,
                         "internet": net, "metric": default_metric(ifn),
                         "ip": iface_ip(ifn)})

    # Decide cellular's role and set its metric accordingly.
    cell_net = False
    if cell:
        cell_link = iface_link(cell)
        cell_net = probe(cell) if cell_link else False
        if cell_gw:
            desired = CELL_BACKUP if wired_internet or not cell_net else CELL_PRIMARY
            set_cell_route(cell, cell_gw, desired)
        adapters.append({"iface": cell, "label": label_for(cell), "link": cell_link,
                         "internet": cell_net, "metric": default_metric(cell),
                         "ip": iface_ip(cell)})

    active = active_iface()
    for a in adapters:
        a["active"] = (a["iface"] == active)

    payload = {
        "updated": datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds").replace("+00:00", "Z"),
        "active": active,
        "adapters": adapters,
    }
    os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
    tmp = STATUS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, STATUS_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
