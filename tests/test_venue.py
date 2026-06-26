"""Venue-name branding: header field + the prefix on every SMS.

The key regression is that with no venue set, the brand prefix reproduces the
old hard-coded "FireWatch:" wording exactly — so stripping it from the template
defaults didn't silently change any message.
"""
from app.config import Detector, Settings, YamlConfig
from app.db import Database
from app.notify import Notifier
from app.state import StateMachine


def _machine(tmp_path, venue=""):
    settings = Settings(db_path=str(tmp_path / "t.db"))
    cfg = YamlConfig(
        venue_name=venue,
        detectors=[Detector(friendly_name="heat_a", label="Heat A", alarm_property="heat")],
    )
    db = Database(settings.db_path)
    return StateMachine(settings, cfg, db, Notifier(settings))


def test_brand_falls_back_to_firewatch(tmp_path):
    m = _machine(tmp_path)
    assert m.brand() == "FireWatch"
    # Unset venue reproduces the historical "FireWatch: ..." wording exactly.
    assert m._msg("detector_offline", label="Heat A", zone="hall") == (
        "FireWatch: detector Heat A in hall went offline (no check-in). Check the device."
    )


def test_venue_prefixes_messages(tmp_path):
    m = _machine(tmp_path, venue="Tivoli Hall")
    assert m.brand() == "Tivoli Hall"
    assert m._msg("detector_low_battery", label="Heat A", battery=8).startswith("Tivoli Hall: ")
    assert "low battery on detector Heat A (8%)" in m._msg("detector_low_battery", label="Heat A", battery=8)


def test_status_summary_uses_brand(tmp_path):
    m = _machine(tmp_path, venue="Tivoli Hall")
    assert m.status_summary().startswith("Tivoli Hall TEST ")


def test_set_venue_updates_snapshot_and_brand(tmp_path):
    import asyncio

    m = _machine(tmp_path)
    assert m.snapshot().venue == ""
    asyncio.run(m.set_venue("  North Stage  "))  # trims whitespace
    assert m.venue == "North Stage"
    assert m.snapshot().venue == "North Stage"
    assert m._msg("alarm", label="Heat A", kind="heat", zone="hall", temperature=58).startswith(
        "North Stage: FIRE ALARM:"
    )
