"""SQLite persistence (aiosqlite).

Holds two things:
  - kv: the current mode, sms_policy and event_until, so a restart resumes
    exactly where it left off (important: a crash must not silently re-arm or
    un-arm the system).
  - audit: an append-only log of every alarm, mode change and SMS attempt.
    This is your incident-review trail and what keeps the fire-authority
    relationship clean.
"""
from __future__ import annotations

import json
from pathlib import Path

import aiosqlite

from .models import now

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    kind      TEXT NOT NULL,   -- alarm | mode_change | sms | fault | system
    severity  TEXT,
    actor     TEXT,            -- who/what caused it
    detail    TEXT             -- JSON blob
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit (ts);
"""


class Database:
    def __init__(self, path: str) -> None:
        self._path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    async def init(self) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    async def get(self, key: str, default: str | None = None) -> str | None:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute("SELECT value FROM kv WHERE key = ?", (key,))
            row = await cur.fetchone()
            return row[0] if row else default

    async def set(self, key: str, value: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            await db.commit()

    async def audit(
        self,
        kind: str,
        detail: dict,
        *,
        severity: str | None = None,
        actor: str | None = None,
    ) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT INTO audit (ts, kind, severity, actor, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (now().isoformat(), kind, severity, actor, json.dumps(detail)),
            )
            await db.commit()

    async def recent_audit(self, limit: int = 100) -> list[dict]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT ts, kind, severity, actor, detail FROM audit "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            rows = await cur.fetchall()
            out = []
            for r in rows:
                out.append(
                    {
                        "ts": r["ts"],
                        "kind": r["kind"],
                        "severity": r["severity"],
                        "actor": r["actor"],
                        "detail": json.loads(r["detail"]) if r["detail"] else {},
                    }
                )
            return out
