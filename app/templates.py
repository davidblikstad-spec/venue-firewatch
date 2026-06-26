"""Operator-editable SMS message templates.

Each alert scenario renders its text from a template. Defaults live here; the
dashboard can override any of them (stored in the kv table as ``template.<key>``
and held live on the StateMachine, so edits take effect with no restart).

Templates use ``{placeholder}`` fields filled from the event context. Unknown
placeholders are left visible (``{foo}``) rather than crashing, so a typo in the
UI is obvious instead of silently dropping the message.
"""
from __future__ import annotations

# key -> metadata. Order here is the order shown in the Settings UI.
SMS_TEMPLATES: dict[str, dict] = {
    "alarm": {
        "label": "Fire alarm triggered",
        "placeholders": ["label", "kind", "zone", "temperature"],
        "default": "FIRE ALARM: {label} ({kind}) in {zone}. Temp {temperature}C. Investigate immediately.",
    },
    "detector_offline": {
        "label": "Detector offline (no check-in)",
        "placeholders": ["label", "zone"],
        "default": "FireWatch: detector {label} in {zone} went offline (no check-in). Check the device.",
    },
    "detector_low_battery": {
        "label": "Detector battery low",
        "placeholders": ["label", "battery"],
        "default": "FireWatch: low battery on detector {label} ({battery}%). Replace the battery soon.",
    },
    "ups_on_battery": {
        "label": "Mains lost — UPS on backup power",
        "placeholders": ["charge", "runtime_min", "grid_voltage", "load"],
        "default": "FireWatch: MAINS POWER LOST. UPS on backup ({charge}% battery, ~{runtime_min} min runtime).",
    },
    "ups_low_battery": {
        "label": "UPS battery critically low",
        "placeholders": ["charge", "runtime_min"],
        "default": "FireWatch: UPS battery CRITICALLY LOW ({charge}%) — shutdown imminent.",
    },
    "ups_restored": {
        "label": "Mains power restored",
        "placeholders": ["grid_voltage"],
        "default": "FireWatch: mains power restored ({grid_voltage}V). UPS back on line power.",
    },
    "sms_failover": {
        "label": "Failover to TRM240 cellular",
        "placeholders": [],
        "default": "FireWatch: primary SMS path (GatewayAPI) is down — now sending via TRM240 cellular network.",
    },
}


class _SafeDict(dict):
    # Leave unknown placeholders visible so UI typos are caught, not swallowed.
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render(template: str, ctx: dict) -> str:
    safe = _SafeDict((k, "" if v is None else v) for k, v in ctx.items())
    try:
        return template.format_map(safe)
    except (ValueError, IndexError, KeyError):
        return template  # malformed template — send it verbatim rather than nothing


def default_text(key: str) -> str:
    spec = SMS_TEMPLATES.get(key)
    return spec["default"] if spec else ""
