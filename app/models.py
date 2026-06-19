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
    raw_status: str | None = None
    last_seen: datetime | None = None


class BalanceState(BaseModel):
    credit: float | None = None
    low: bool = False
    last_checked: datetime | None = None


class SystemSnapshot(BaseModel):
    """Everything the dashboard needs in one push."""

    mode: Mode = Mode.NORMAL
    sms_policy: SmsPolicy = SmsPolicy.FAILOVER
    event_until: datetime | None = None
    detectors: list[DetectorState] = Field(default_factory=list)
    ups: UpsState | None = None
    balance: BalanceState | None = None
    updated_at: datetime = Field(default_factory=now)
