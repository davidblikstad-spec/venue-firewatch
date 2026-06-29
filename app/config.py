"""Configuration.

Two sources, deliberately split:
  - Secrets and tuning come from environment variables / .env  (Settings)
  - Structural config — detectors, SMS recipients — comes from config.yaml
    so it can be edited and version-tracked without touching code or secrets.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Secrets and runtime tuning. Loaded from environment / .env."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="FW_", extra="ignore")

    # --- Web ---
    host: str = "0.0.0.0"
    port: int = 8080

    # --- Storage ---
    db_path: str = "data/firewatch.db"

    # --- MQTT broker (Mosquitto, on localhost) ---
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    mqtt_base_topic: str = "zigbee2mqtt"

    # --- GatewayAPI (primary SMS) ---
    # Pull the exact endpoint from your GatewayAPI dashboard. The token is
    # normally set from the dashboard Settings page (stored in the db) — a value
    # saved there overrides this env var. Leave unset to manage it via the UI.
    gatewayapi_base_url: str = "https://gatewayapi.com"
    gatewayapi_token: str | None = None
    gatewayapi_sender: str = "FireWatch"  # max 11 chars alphanumeric
    gatewayapi_timeout_s: float = 9.0

    # --- TRM240 modem (secondary SMS) ---
    # By default the built-in AT/PDU sender (app.modem_sms) talks to the modem's
    # serial AT port directly — no gammu/ModemManager needed. Override only if
    # you'd rather shell out: set modem_send_cmd to a command template where
    # {to} and {text} are filled in, e.g. "gammu sendsms TEXT {to} -text {text!r}".
    modem_port: str = "/dev/ttyUSB2"  # Quectel EC21 AT-command port on the TRM240
    modem_send_cmd: str | None = None  # None -> use the built-in AT/PDU sender
    modem_enabled: bool = True

    # --- UPS (NUT) ---
    ups_name: str | None = None  # e.g. "myups@localhost"; None disables polling
    ups_poll_seconds: int = 30

    # --- WAN / internet failover ---
    # Status file written by the root failover worker (setup-wan-failover.sh).
    # If fresh, it's authoritative; otherwise the dashboard derives WAN status
    # itself (read-only, no root). See app/wan.py.
    wan_status_path: str = "/run/wan-failover/status.json"
    wan_status_max_age_s: int = 120
    wan_poll_seconds: int = 5

    # --- Event mode ---
    # Hard ceiling on how long EVENT (silent) mode may stay armed, regardless of
    # the end time chosen by the operator. A safety backstop against "left on".
    event_max_hours: int = 8

    # --- Dashboard auth ---
    # The password is normally chosen on first run via the dashboard and stored
    # as a salted hash in the db. This env var is a legacy/bootstrap option: if
    # set, it is migrated into the store on first startup. Leave empty to use the
    # first-run setup flow instead.
    auth_password: str | None = None
    auth_token_ttl_hours: int = 24

    # --- Balance check ---
    balance_check_interval_minutes: int = 60
    balance_warn_threshold: float = 10.0  # warn when credit below this

    # --- Escalation ---
    escalation_timeout_minutes: int = 5  # re-alert after this many minutes with no ack


class Recipient(BaseModel):
    name: str
    msisdn: str  # international format, e.g. 4799999999 (no +)
    priority: int = 0  # lower = alerted first; same priority = alerted together


class Detector(BaseModel):
    """A Zigbee detector exposed by Zigbee2MQTT.

    `friendly_name` must match the name in your Z2M frontend.
    `alarm_property` is the boolean field that goes true on detection — confirm
    the exact name in Z2M's exposed values (e.g. "heat" for HESZB-120,
    "smoke" for a smoke alarm). `zone` lets EVENT mode silence only the haze
    area while still alerting elsewhere.
    """

    friendly_name: str
    label: str
    kind: str = "heat"  # "heat" | "smoke" | other
    alarm_property: str = "heat"
    zone: str = "default"
    # Hours after which a missing check-in is treated as a fault (supervision).
    offline_after_hours: float = 6.0
    # Settable Z2M property that sounds this device's buzzer. None = can't be
    # remotely sounded (a sensor with no siren). "alarm" for Develco SMSZB-120.
    siren_property: str | None = None


class YamlConfig(BaseModel):
    # Site name shown in the dashboard header and prefixed to every SMS so
    # recipients know which venue an alert is from. Editable from the dashboard
    # (kv-stored, authoritative once set); this only seeds the first boot.
    venue_name: str = ""
    recipients: list[Recipient] = Field(default_factory=list)
    detectors: list[Detector] = Field(default_factory=list)
    # Zones that EVENT mode silences. Detectors in other zones still alert.
    silent_zones_in_event: list[str] = Field(default_factory=lambda: ["stage"])
    # When true, a real (non-silenced) alarm sounds every siren-capable detector.
    siren_interconnect: bool = False
    # Seconds each remote siren self-stops after — the safety backstop so a
    # dropped broker or missed clear can't latch the buzzers on forever.
    siren_max_duration_s: int = 300


@lru_cache
def get_settings() -> Settings:
    return Settings()


def load_yaml_config(path: str = "config.yaml") -> YamlConfig:
    p = Path(path)
    if not p.exists():
        return YamlConfig()
    data = yaml.safe_load(p.read_text()) or {}
    return YamlConfig(**data)
