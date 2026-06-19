"""MQTT bridge to Zigbee2MQTT.

Z2M publishes each device's state as JSON to `<base_topic>/<friendly_name>`
and the full device inventory (retained) to `<base_topic>/bridge/devices`.

We subscribe with a wildcard rather than to a fixed list of topics. Two reasons:
  - the set of monitored detectors is now edited live from the dashboard, so a
    static subscription list would go stale without a reconnect;
  - the retained `bridge/devices` message lets the setup page *discover* what is
    actually paired to the coordinator (and which exposes look like an alarm),
    so you can pick a detector from a list instead of typing friendly_names.

Z2M owns the radio and the Zigbee protocol; this is still just a thin
subscriber, which is exactly why a restart of this app doesn't disturb the mesh.
"""
from __future__ import annotations

import asyncio
import json
import logging

import aiomqtt

from .config import Settings, YamlConfig
from .state import StateMachine

log = logging.getLogger("firewatch.mqtt")

# Binary exposes that plausibly represent "this detector has tripped". Ordered
# by how likely we are to want it as the alarm property when auto-suggesting.
ALARM_PROPERTIES = ("smoke", "heat", "gas", "carbon_monoxide", "co", "water_leak")
# Other binary exposes worth surfacing but not treated as the primary alarm.
_SUPERVISORY_PROPERTIES = ("tamper", "battery_low")


def _walk_exposes(exposes: list) -> list[dict]:
    """Flatten Z2M's (possibly nested) exposes tree into leaf features."""
    leaves: list[dict] = []
    for ex in exposes or []:
        if not isinstance(ex, dict):
            continue
        if "features" in ex and isinstance(ex["features"], list):
            leaves.extend(_walk_exposes(ex["features"]))
        elif ex.get("property"):
            leaves.append(ex)
    return leaves


def _summarize_device(entry: dict) -> dict | None:
    """Turn one `bridge/devices` entry into a compact record for the UI.

    Returns None for the coordinator and anything without a friendly_name.
    """
    if entry.get("type") == "Coordinator":
        return None
    friendly = entry.get("friendly_name")
    if not friendly:
        return None
    definition = entry.get("definition") or {}
    leaves = _walk_exposes(definition.get("exposes") or [])

    binary_props = [f["property"] for f in leaves if f.get("type") == "binary"]
    alarm_props = [p for p in ALARM_PROPERTIES if p in binary_props]
    suggested = alarm_props[0] if alarm_props else None
    kind = "smoke" if "smoke" in binary_props else "heat" if "heat" in binary_props else "other"

    return {
        "friendly_name": friendly,
        "vendor": definition.get("vendor"),
        "model": definition.get("model"),
        "description": definition.get("description"),
        "binary_properties": binary_props,
        "alarm_properties": alarm_props,
        "suggested_alarm_property": suggested,
        "suggested_kind": kind,
        "is_alarm_device": bool(alarm_props),
    }


class DeviceRegistry:
    """Holds the devices discovered from Z2M's retained `bridge/devices` topic.

    Shared with the web layer so the detector setup page can list paired devices
    and pre-fill the alarm property. Updated whenever Z2M republishes.
    """

    def __init__(self) -> None:
        self._devices: dict[str, dict] = {}
        self.last_seen = None  # datetime of the last bridge/devices message

    def update(self, raw: bytes) -> None:
        from .models import now

        try:
            entries = json.loads(raw)
        except (ValueError, TypeError):
            log.debug("non-JSON bridge/devices payload")
            return
        if not isinstance(entries, list):
            return
        found: dict[str, dict] = {}
        for entry in entries:
            summary = _summarize_device(entry) if isinstance(entry, dict) else None
            if summary:
                found[summary["friendly_name"]] = summary
        self._devices = found
        self.last_seen = now()
        log.info("discovered %d Zigbee device(s) from bridge/devices", len(found))

    def devices(self) -> list[dict]:
        # Alarm-capable devices first, then alphabetical.
        return sorted(
            self._devices.values(),
            key=lambda d: (not d["is_alarm_device"], d["friendly_name"]),
        )


class MqttBridge:
    def __init__(
        self,
        settings: Settings,
        cfg: YamlConfig,
        machine: StateMachine,
        registry: DeviceRegistry | None = None,
    ) -> None:
        self._s = settings
        self._cfg = cfg
        self._machine = machine
        self._registry = registry or DeviceRegistry()

    async def run(self) -> None:
        """Connect-and-subscribe loop with automatic reconnect."""
        base = self._s.mqtt_base_topic
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self._s.mqtt_host,
                    port=self._s.mqtt_port,
                    username=self._s.mqtt_username,
                    password=self._s.mqtt_password,
                ) as client:
                    # bridge/# for Z2M state + device inventory; +/ for every
                    # device's state. Wildcard so detectors added from the
                    # dashboard are picked up without reconnecting.
                    await client.subscribe(f"{base}/bridge/#")
                    await client.subscribe(f"{base}/+")
                    log.info("subscribed to %s/+ and bridge/#", base)
                    await self._machine.set_mqtt_connected(True)
                    async for message in client.messages:
                        await self._handle(str(message.topic), message.payload)
            except aiomqtt.MqttError as exc:
                log.warning("MQTT connection lost (%s); retrying in 5s", exc)
                await self._machine.set_mqtt_connected(False)
                await asyncio.sleep(5)

    async def _handle(self, topic: str, raw: bytes) -> None:
        base = self._s.mqtt_base_topic
        sub = topic[len(base) + 1 :] if topic.startswith(base + "/") else topic

        if sub == "bridge/devices":
            self._registry.update(raw)
            return
        if sub == "bridge/state":
            # Z2M publishes either a bare "online"/"offline" or {"state": ...}.
            text = raw.decode(errors="ignore").strip()
            try:
                parsed = json.loads(text)
                state = parsed.get("state") if isinstance(parsed, dict) else parsed
            except (ValueError, TypeError):
                state = text.strip('"')
            await self._machine.set_zigbee_online(state == "online")
            return
        if sub.startswith("bridge/"):
            return  # other bridge info/logging — not a detector

        # Only forward updates for currently-configured detectors. Look the
        # alarm property up live so dashboard edits take effect immediately.
        det = next((d for d in self._cfg.detectors if d.friendly_name == sub), None)
        if det is None:
            return

        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            log.debug("non-JSON payload on %s", topic)
            return
        await self._machine.on_detector_update(sub, payload, det.alarm_property)
