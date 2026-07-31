"""SQLite persistence: video catalogue, target groups, send log, runtime settings.

No video bytes are ever stored. Only the message_id inside the vault channel,
which is all `copyMessage` needs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id   INTEGER NOT NULL UNIQUE,
    file_id      TEXT,
    caption      TEXT,
    kind         TEXT NOT NULL DEFAULT 'video',
    active       INTEGER NOT NULL DEFAULT 1,
    times_sent   INTEGER NOT NULL DEFAULT 0,
    last_sent_at TEXT,
    added_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS groups (
    chat_id   INTEGER PRIMARY KEY,
    title     TEXT,
    active    INTEGER NOT NULL DEFAULT 1,
    added_at  TEXT NOT NULL,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS send_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER,
    chat_id  INTEGER,
    status   TEXT NOT NULL,
    detail   TEXT,
    sent_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_send_log_sent_at ON send_log(sent_at);
CREATE INDEX IF NOT EXISTS idx_videos_rotation ON videos(active, times_sent, last_sent_at);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Video:
    id: int
    message_id: int
    caption: str | None
    kind: str
    times_sent: int
    last_sent_at: str | None


@dataclass
class Group:
    chat_id: int
    title: str | None


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    # ---------- lifecycle ----------

    async def connect(self) -> None:
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() was never awaited")
        return self._conn

    # ---------- videos ----------

    async def add_video(
        self, message_id: int, caption: str | None, kind: str, file_id: str | None
    ) -> bool:
        """Returns True if newly inserted, False if this message_id was already known."""
        cur = await self.conn.execute(
            """INSERT OR IGNORE INTO videos (message_id, file_id, caption, kind, added_at)
               VALUES (?, ?, ?, ?, ?)""",
            (message_id, file_id, caption, kind, utcnow()),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def remove_video(self, message_id: int) -> bool:
        cur = await self.conn.execute("DELETE FROM videos WHERE message_id = ?", (message_id,))
        await self.conn.commit()
        return cur.rowcount > 0

    async def deactivate_video(self, video_id: int) -> bool:
        """Pull a video out of rotation but keep the row so send_log stays meaningful."""
        cur = await self.conn.execute(
            "UPDATE videos SET active = 0 WHERE id = ? AND active = 1", (video_id,)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def restore_video(self, video_id: int) -> bool:
        cur = await self.conn.execute(
            "UPDATE videos SET active = 1 WHERE id = ? AND active = 0", (video_id,)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def list_removed(self, limit: int = 20) -> list[Video]:
        async with self.conn.execute(
            """SELECT id, message_id, caption, kind, times_sent, last_sent_at
               FROM videos WHERE active = 0 ORDER BY id DESC LIMIT ?""",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [Video(**dict(r)) for r in rows]

    async def next_video(self) -> Video | None:
        """Least-recently-used rotation: fewest sends first, oldest send as tiebreaker."""
        async with self.conn.execute(
            """SELECT id, message_id, caption, kind, times_sent, last_sent_at
               FROM videos
               WHERE active = 1
               ORDER BY times_sent ASC, COALESCE(last_sent_at, '0') ASC, id ASC
               LIMIT 1"""
        ) as cur:
            row = await cur.fetchone()
        return Video(**dict(row)) if row else None

    async def get_video(self, video_id: int) -> Video | None:
        async with self.conn.execute(
            """SELECT id, message_id, caption, kind, times_sent, last_sent_at
               FROM videos WHERE id = ?""",
            (video_id,),
        ) as cur:
            row = await cur.fetchone()
        return Video(**dict(row)) if row else None

    async def list_videos(self, limit: int = 20) -> list[Video]:
        async with self.conn.execute(
            """SELECT id, message_id, caption, kind, times_sent, last_sent_at
               FROM videos WHERE active = 1
               ORDER BY times_sent ASC, id ASC LIMIT ?""",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [Video(**dict(r)) for r in rows]

    async def count_videos(self) -> int:
        async with self.conn.execute("SELECT COUNT(*) FROM videos WHERE active = 1") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def mark_video_sent(self, video_id: int) -> None:
        await self.conn.execute(
            "UPDATE videos SET times_sent = times_sent + 1, last_sent_at = ? WHERE id = ?",
            (utcnow(), video_id),
        )
        await self.conn.commit()

    # ---------- groups ----------

    async def upsert_group(self, chat_id: int, title: str | None) -> None:
        await self.conn.execute(
            """INSERT INTO groups (chat_id, title, active, added_at)
               VALUES (?, ?, 1, ?)
               ON CONFLICT(chat_id) DO UPDATE
                 SET title = excluded.title, active = 1, last_error = NULL""",
            (chat_id, title, utcnow()),
        )
        await self.conn.commit()

    async def deactivate_group(self, chat_id: int, reason: str | None = None) -> None:
        await self.conn.execute(
            "UPDATE groups SET active = 0, last_error = ? WHERE chat_id = ?",
            (reason, chat_id),
        )
        await self.conn.commit()

    async def active_groups(self) -> list[Group]:
        async with self.conn.execute(
            "SELECT chat_id, title FROM groups WHERE active = 1 ORDER BY added_at ASC, chat_id ASC"
        ) as cur:
            rows = await cur.fetchall()
        return [Group(chat_id=r["chat_id"], title=r["title"]) for r in rows]

    async def count_groups(self) -> int:
        async with self.conn.execute("SELECT COUNT(*) FROM groups WHERE active = 1") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    # ---------- log ----------

    async def log_send(
        self, video_id: int | None, chat_id: int | None, status: str, detail: str | None = None
    ) -> None:
        await self.conn.execute(
            "INSERT INTO send_log (video_id, chat_id, status, detail, sent_at) VALUES (?, ?, ?, ?, ?)",
            (video_id, chat_id, status, detail, utcnow()),
        )
        await self.conn.commit()

    # ---------- settings ----------

    async def get_setting(self, key: str) -> str | None:
        async with self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self.conn.execute(
            """INSERT INTO settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, value),
        )
        await self.conn.commit()

    async def interval_hours(self, default: float) -> float:
        raw = await self.get_setting("interval_hours")
        try:
            return float(raw) if raw else default
        except ValueError:
            return default

    async def set_interval_hours(self, hours: float) -> None:
        await self.set_setting("interval_hours", str(hours))

    async def is_paused(self) -> bool:
        return (await self.get_setting("paused")) == "1"

    async def set_paused(self, paused: bool) -> None:
        await self.set_setting("paused", "1" if paused else "0")

    async def last_run_at(self) -> datetime | None:
        raw = await self.get_setting("last_run_at")
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    async def touch_last_run(self) -> None:
        await self.set_setting("last_run_at", utcnow())
