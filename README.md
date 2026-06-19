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
- **Delivery receipts (DLR):** GatewayAPI delivery reports are captured via a
  webhook (`/api/sms/dlr`) and logged in the audit trail.
- **Balance monitoring:** periodic GatewayAPI credit check with a configurable
  low-credit warning (`FW_BALANCE_WARN_THRESHOLD`), shown on the dashboard.
- **Per-recipient escalation:** recipients have a `priority` field. The first
  tier is alerted immediately; if no acknowledgment arrives within
  `FW_ESCALATION_TIMEOUT_MINUTES`, the next tier is alerted. Acknowledgment
  via `POST /api/sms/ack` stops escalation.
- **Dashboard authentication:** optional password protection
  (`FW_AUTH_PASSWORD`). When set, all dashboard and API access requires a
  session token obtained via the login page.
- **Supervision:** flags detectors that miss check-ins or report low battery.
- **Audit trail:** every alarm, mode change, SMS attempt, DLR, and escalation
  in SQLite — your incident-review record.

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

v0.2 — all planned features implemented: DLR webhooks, balance monitoring,
per-recipient escalation, and dashboard auth.

### Configuration via the dashboard

The dashboard password and the GatewayAPI token are managed from the UI and
persisted in the database (the password is stored as a salted PBKDF2 hash, never
in plaintext) — you do not need to set them in `.env`:

- **First run:** the dashboard prompts you to choose a password (min 8 chars).
  Until one is set, every page redirects to that setup screen.
- **Later:** open **⚙ Settings** (top-right) to change the password or to set /
  clear the GatewayAPI token. A new token takes effect immediately — no restart.
- **Legacy `FW_AUTH_PASSWORD`:** if present in `.env`, it is migrated into the
  store on first startup, so existing deployments keep working. A token saved in
  the UI overrides `FW_GATEWAYAPI_TOKEN`.

### Managing detectors from the dashboard

The set of monitored smoke/heat alarms is edited from **🔥 Detectors** (top-right)
and stored in the database — you no longer have to hand-edit `config.yaml`:

- **Discovery:** the MQTT bridge subscribes to Z2M's retained
  `zigbee2mqtt/bridge/devices` topic, so the page lists the devices actually
  paired to the coordinator. Alarm-capable ones (those exposing `smoke`, `heat`,
  `gas`, …) are listed first, and **Add** pre-fills a row with the right
  `friendly_name`, `alarm_property`, and `kind`.
- **Live updates:** the bridge subscribes with a wildcard and reads each
  detector's `alarm_property` off the live config, so adding or editing a
  detector takes effect on the next MQTT message — **no restart**.
- **Migration:** on first boot the detectors in `config.yaml` are seeded into the
  store; from then on the dashboard is authoritative.

(`config.yaml` still holds SMS recipients and `silent_zones_in_event`.)

## License

MIT — see LICENSE.
