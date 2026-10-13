"""Per-recipient message categories: who gets fire / power / technical SMS."""
import asyncio

from app.config import Recipient, Settings, YamlConfig, Detector
from app.db import Database
from app.notify import Notifier
from app.state import StateMachine


def _machine(tmp_path, recipients):
    settings = Settings(db_path=str(tmp_path / "t.db"))
    cfg = YamlConfig(
        recipients=recipients,
        detectors=[Detector(friendly_name="smoke_a", label="A", kind="smoke", alarm_property="smoke")],
    )
    machine = StateMachine(settings, cfg, Database(settings.db_path), Notifier(settings))
    sent: list[tuple[list[str], str]] = []

    async def fake_broadcast(msisdns, text, policy):
        sent.append((sorted(msisdns), text))
        return {}

    machine._notifier.broadcast = fake_broadcast
    return machine, sent


def test_recipient_defaults_to_all_categories():
    # Recipients stored before categories existed keep getting everything.
    assert Recipient(name="Old", msisdn="4790000000").categories == ["fire", "power", "technical"]


def test_recipient_drops_unknown_categories():
    r = Recipient(name="X", msisdn="4790000000", categories=["technical", "bogus", "fire", "fire"])
    assert r.categories == ["fire", "technical"]


def test_messages_route_by_category(tmp_path):
    machine, sent = _machine(tmp_path, [
        Recipient(name="Manager", msisdn="4711111111", categories=["fire"]),
        Recipient(name="Tech", msisdn="4722222222", categories=["fire", "power", "technical"]),
    ])

    async def go():
        await machine._db.init()
        await machine._raise(machine_sev("WARNING"), "detector offline", "supervision", category="technical")
        await machine._raise(machine_sev("CRITICAL"), "FIRE", "detector", category="fire")
        assert sent == [(["4722222222"], "detector offline"),
                        (["4711111111", "4722222222"], "FIRE")]

    asyncio.run(go())


def test_category_with_no_subscribers_is_audited_not_sent(tmp_path):
    machine, sent = _machine(tmp_path, [Recipient(name="M", msisdn="4711111111", categories=["fire"])])

    async def go():
        await machine._db.init()
        await machine._raise(machine_sev("WARNING"), "UPS on battery", "ups", category="power")
        assert sent == []
        kinds = [r["kind"] for r in await machine._db.recent_audit(5)]
        assert "sms_unrouted" in kinds

    asyncio.run(go())


def test_escalation_stays_within_category(tmp_path):
    # Priority tiers are computed among the category's subscribers only: a
    # tier-1 manager who doesn't take technical alerts is never escalated to.
    from datetime import timedelta
    machine, sent = _machine(tmp_path, [
        Recipient(name="Tech", msisdn="4722222222", priority=0, categories=["technical", "fire"]),
        Recipient(name="Manager", msisdn="4711111111", priority=1, categories=["fire"]),
    ])

    async def go():
        await machine._db.init()
        await machine._raise(machine_sev("CRITICAL"), "FIRE", "detector", category="fire")
        assert sent == [(["4722222222"], "FIRE")]
        assert len(machine._active_alerts) == 1  # manager pending escalation
        machine._active_alerts.clear()
        await machine._raise(machine_sev("WARNING"), "offline", "supervision", category="technical")
        assert sent[-1] == (["4722222222"], "offline")
        assert machine._active_alerts == {}

    asyncio.run(go())


def machine_sev(name):
    from app.models import Severity
    return Severity[name]
