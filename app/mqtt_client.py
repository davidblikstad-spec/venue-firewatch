"""MQTT bridge to Zigbee2MQTT.

Z2M publishes each device's state as JSON to `<base_topic>/<friendly_name>`.
We subscribe to the configured detectors and forward updates to the state
machine. Z2M owns the radio and the Zigbee protocol; this is just a thin
subscriber, which is exactly why a restart of this app doesn't disturb the
mesh.
"""
from __future__ import annotations

import asyncio
import json
import logging

import aiomqtt

from .config import Settings, YamlConfig
from .state import StateMachine

log = logging.getLogger("firewatch.mqtt")


class MqttBridge:
    def __init__(self, settings: Settings, cfg: YamlConfig, machine: StateMachine) -> None:
        self._s = settings
        self._cfg = cfg
        self._machine = machine
        self._alarm_props = {d.friendly_name: d.alarm_property for d in cfg.detectors}

    async def run(self) -> None:
        """Connect-and-subscribe loop with automatic reconnect."""
        base = self._s.mqtt_base_topic
        topics = [f"{base}/{d.friendly_name}" for d in self._cfg.detectors]
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self._s.mqtt_host,
                    port=self._s.mqtt_port,
                    username=self._s.mqtt_username,
                    password=self._s.mqtt_password,
                ) as client:
                    for t in topics:
                        await client.subscribe(t)
                    log.info("subscribed to %d detector topic(s)", len(topics))
                    async for message in client.messages:
                        await self._handle(str(message.topic), message.payload)
            except aiomqtt.MqttError as exc:
                log.warning("MQTT connection lost (%s); retrying in 5s", exc)
                await asyncio.sleep(5)

    async def _handle(self, topic: str, raw: bytes) -> None:
        friendly = topic.split("/", 1)[-1]
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            log.debug("non-JSON payload on %s", topic)
            return
        alarm_prop = self._alarm_props.get(friendly, "alarm")
        await self._machine.on_detector_update(friendly, payload, alarm_prop)
