# Venue FireWatch

A small, self-hosted **secondary** monitoring system for a venue: it watches
Zigbee smoke/heat detectors via Zigbee2MQTT, shows live status on a web
dashboard, and sends SMS alerts — with a manual **event mode** that silences
alerts for haze-prone zones while a human fire watch is on duty.

> ## Scope — read this first
> This is **not** a fire alarm system and **not** a certified life-safety
> device. The detectors it uses are smart-home grade, not EN 54 certified.
> It runs *alongside* the building's certified fire alarm panel and the
> authorized fire watch — never as a replacement. "Event mode" silences only
> this system's SMS layer; it must never be used to silence or bypass the
> certified panel. Treat every alert as advisory.

## How it fits together

```
 frient / Zigbee detectors
        │  (Zigbee 3.0, over the air)
        ▼
   ZBT-2 coordinator (USB)
        │  (serial)
        ▼
   Zigbee2MQTT ──► Mosquitto (MQTT broker) ◄── FireWatch (this app)
                                                  │
                          ┌───────────────────────┼───────────────────────┐
                          ▼                        ▼                       ▼
                  web dashboard            GatewayAPI (SMS)         TRM240 modem
                  (FastAPI + WS)           primary                  SMS fallback + backup WAN
                                                                    NUT ◄── UPS
```

Everything except the radio runs as software on one Debian box. Z2M owns the
ZBT-2; this app is just an MQTT subscriber, so restarting it never disturbs the
Zigbee mesh.

## Features

- **NORMAL / EVENT state machine.** EVENT silences alerts for configured haze
  zones and is logged + shown with a countdown. It **always auto-reverts** —
  there is no open-ended silent toggle, and a hard ceiling (`FW_EVENT_MAX_HOURS`)
  caps the silent window regardless of what end time is chosen.
- **Carve-outs never silenced:** UPS/power events and detector offline/fault,
  even during EVENT.
- **SMS routing, operator-selectable:** *GatewayAPI → modem* failover (default),
  or *both at once*. GatewayAPI is primary; the TRM240 sends native cellular SMS
  as the fallback that works even with no internet.
- **Supervision:** flags detectors that miss check-ins or report low battery.
- **Audit trail:** every alarm, mode change and SMS attempt in SQLite — your
  incident-review record.

## Setup

```bash
# 1. clone your repo, then inside it:
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 2. configure
cp .env.example .env                 # secrets: tokens, MQTT, UPS
cp config.example.yaml config.yaml   # detectors + recipients
$EDITOR .env config.yaml

# 3. run
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Open `http://<box>:8080`. Prereqs on the box: Mosquitto, Zigbee2MQTT (pointed at
the ZBT-2 with the `ember` adapter), and optionally NUT for the UPS.

Confirm each detector's `alarm_property` against the live values in your Z2M
frontend — for the frient HESZB-120 it's usually `heat`.

Run the tests with `pytest -q`.

For production, copy `systemd/venue-firewatch.service`, adjust paths, and
`systemctl enable --now venue-firewatch`.

## Status

Scaffold / v0.1 — working skeleton meant to be built on. Known next steps:
GatewayAPI delivery-receipt (DLR) webhook handling, GatewayAPI balance check,
per-recipient escalation, and auth on the dashboard before exposing it.

## License

MIT — see LICENSE.
