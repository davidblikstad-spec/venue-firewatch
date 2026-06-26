"""Shared domain types."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


def now() -> datetime:
    return datetime.now(timezone.utc)


class Mode(str, Enum):
    NORMAL = "normal"   # detection -> SMS alert
    EVENT = "event"     # silenced zones logged only; fire watch is the detection


class Severity(str, Enum):
    CRITICAL = "critical"   # confirmed alarm — see SMS policy
    WARNING = "warning"     # fault / low battery / offline / power event
    INFO = "info"           # heartbeat, mode changes, "system OK"


class SmsPolicy(str, Enum):
    FAILOVER = "failover"   # GatewayAPI first, TRM only if it fails
    BOTH = "both"           # send via both at once


class DetectorState(BaseModel):
    friendly_name: str
    label: str
    zone: str
    kind: str
    alarm: bool = False
    temperature: float | None = None
    battery: int | None = None
    linkquality: int | None = None   # Z2M LQI (0–255); mesh signal strength
    fault: bool = False
    last_seen: datetime | None = None
    online: bool = True


class UpsState(BaseModel):
    # False until the NUT poller actually reads the UPS. Distinguishes
    # "no UPS configured / not wired" from a UPS we've successfully reached,
    # so the dashboard doesn't claim "online" for a UPS that isn't there.
    monitored: bool = False
    online: bool = True
    on_battery: bool = False
    low_battery: bool = False
    charge_pct: int | None = None
    grid_voltage: float | None = None   # input.voltage — mains/grid feeding the UPS
    load_pct: int | None = None         # ups.load
    runtime_s: int | None = None        # battery.runtime, seconds remaining on battery
    raw_status: str | None = None
    last_seen: datetime | None = None


class BalanceState(BaseModel):
    credit: float | None = None
    low: bool = False
    last_checked: datetime | None = None


class LinkState(BaseModel):
    """Health of the Zigbee pipeline upstream of this app (broker + Z2M).

    Distinct from the dashboard's own WebSocket: this is whether FireWatch can
    actually receive detector data, which is what the operator cares about.
    """
    mqtt_connected: bool = False   # connected to the MQTT broker
    zigbee_online: bool = False    # Z2M reports bridge state "online"
    last_message: datetime | None = None


class WanAdapter(BaseModel):
    iface: str
    label: str                      # friendly name, e.g. "Wired (primary)"
    link: bool = False              # carrier present (cable in / radio attached)
    internet: bool = False          # reachable upstream (best-effort; accurate only from the root probe)
    active: bool = False            # the path the box is currently using for internet
    metric: int | None = None       # default-route metric (lower = preferred)
    ip: str | None = None


class WanState(BaseModel):
    """Internet uplinks and which one is in use. Fed by the wan poller, which
    prefers the root failover worker's status file and falls back to a
    read-only local derivation when that file is absent."""
    monitored: bool = False         # False until we have any data
    active: str | None = None       # iface currently carrying internet, or None
    source: str = "local"           # "worker" (root status file) or "local" (derived)
    adapters: list[WanAdapter] = Field(default_factory=list)
    updated: datetime | None = None


class SystemSnapshot(BaseModel):
    """Everything the dashboard needs in one push."""

    mode: Mode = Mode.NORMAL
    sms_policy: SmsPolicy = SmsPolicy.FAILOVER
    event_until: datetime | None = None
    detectors: list[DetectorState] = Field(default_factory=list)
    ups: UpsState | None = None
    balance: BalanceState | None = None
    link: LinkState = Field(default_factory=LinkState)
    wan: WanState | None = None
    updated_at: datetime = Field(default_factory=now)
