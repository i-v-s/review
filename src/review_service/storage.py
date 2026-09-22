from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiosqlite

from .models import Snapshot, SourceEvent


class Store:
    """Versioned artifacts and write-ahead operation journal, separate from Git."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()

    async def open(self):
        self.db = await aiosqlite.connect(self.path)
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA synchronous=FULL")
        await self.db.executescript("""
            CREATE TABLE IF NOT EXISTS artifacts (
              kind TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL,
              seq INTEGER PRIMARY KEY AUTOINCREMENT, UNIQUE(kind, id)
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            PRAGMA user_version=1;
        """)
        await self.db.commit()

    async def close(self):
        await self.db.close()

    async def put(self, kind: str, id: str, body: dict):
        async with self.lock:
            await self.db.execute(
                "INSERT INTO artifacts(kind,id,body) VALUES(?,?,?) "
                "ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body",
                (kind, id, json.dumps(body, ensure_ascii=True)),
            )
            await self.db.commit()

    async def get(self, kind: str, id: str) -> dict | None:
        async with self.db.execute("SELECT body FROM artifacts WHERE kind=? AND id=?", (kind, id)) as cur:
            row = await cur.fetchone()
        return json.loads(row[0]) if row else None

    async def list(self, kind: str) -> list[dict]:
        async with self.db.execute("SELECT body FROM artifacts WHERE kind=? ORDER BY seq", (kind,)) as cur:
            rows = await cur.fetchall()
        return [json.loads(row[0]) for row in rows]

    async def setting(self, key: str, default=None):
        async with self.db.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
        return json.loads(row[0]) if row else default

    async def set_setting(self, key: str, value):
        async with self.lock:
            await self.db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, json.dumps(value)))
            await self.db.commit()

    async def save_snapshot(self, snapshot: Snapshot):
        await self.put("snapshot", snapshot.id, snapshot.model_dump())

    async def snapshot(self, id: str) -> Snapshot | None:
        body = await self.get("snapshot", id)
        return Snapshot.model_validate(body) if body else None

    async def event(self, event: SourceEvent):
        await self.put("event", event.id, event.model_dump())
