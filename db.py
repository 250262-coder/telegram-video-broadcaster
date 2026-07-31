"""Postgres persistence: video catalogue, target groups, send log, runtime settings.

No video bytes are ever stored. Only the message_id inside the vault channel,
which is all `copyMessage` needs.

Why Postgres and not a local file: the bot runs on App Platform, whose containers
have an ephemeral filesystem. Group chat_ids in particular cannot be recovered
from the Telegram API once lost, so state has to live outside the container.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import asyncpg

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    message_id   BIGINT NOT NULL UNIQUE,
    file_id      TEXT,
    caption      TEXT,
    kind         TEXT NOT NULL DEFAULT 'video',
    active       BOOLEAN NOT NULL DEFAULT TRUE,
    times_sent   INTEGER NOT NULL DEFAULT 0,
    last_sent_at TIMESTAMPTZ,
    added_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS groups (
    chat_id    BIGINT PRIMARY KEY,
    title      TEXT,
    active     BOOLEAN NOT NULL DEFAULT TRUE,
    added_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS send_log (
    id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    video_id BIGINT,
    chat_id  BIGINT,
    status   TEXT NOT NULL,
    detail   TEXT,
    sent_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_send_log_sent_at ON send_log (sent_at);
CREATE INDEX IF NOT EXISTS idx_videos_rotation ON videos (active, times_sent, last_sent_at);
"""

VIDEO_COLUMNS = "id, message_id, caption, kind, times_sent, last_sent_at"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Video:
    id: int
    message_id: int
    caption: str | None
    kind: str
    times_sent: int
    last_sent_at: datetime | None


@dataclass
class Group:
    chat_id: int
    title: str | None


class Database:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._pool: asyncpg.Pool | None = None

    # ---------- lifecycle ----------

    async def connect(self) -> None:
        kwargs: dict = {
            "min_size": 1,
            "max_size": 5,
            "command_timeout": 30,
            # Required when the DSN points at Supavisor transaction mode (port 6543),
            # which cannot hold server-side prepared statements. Harmless otherwise.
            "statement_cache_size": 0,
        }
        if "sslmode" not in self.dsn and not self._is_local():
            kwargs["ssl"] = "require"

        self._pool = await asyncpg.create_pool(self.dsn, **kwargs)
        async with self._pool.acquire() as conn:
            await conn.execute(SCHEMA)

    def _is_local(self) -> bool:
        return "@localhost" in self.dsn or "@127.0.0.1" in self.dsn

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database.connect() was never awaited")
        return self._pool

    # ---------- videos ----------

    async def add_video(
        self, message_id: int, caption: str | None, kind: str, file_id: str | None
    ) -> bool:
        """Returns True if newly inserted, False if this message_id was already known."""
        row = await self.pool.fetchval(
            """INSERT INTO videos (message_id, file_id, caption, kind)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (message_id) DO NOTHING
               RETURNING id""",
            message_id, file_id, caption, kind,
        )
        return row is not None

    async def remove_video(self, message_id: int) -> bool:
        row = await self.pool.fetchval(
            "DELETE FROM videos WHERE message_id = $1 RETURNING id", message_id
        )
        return row is not None

    async def deactivate_video(self, video_id: int) -> bool:
        """Pull a video out of rotation but keep the row so send_log stays meaningful."""
        row = await self.pool.fetchval(
            "UPDATE videos SET active = FALSE WHERE id = $1 AND active RETURNING id", video_id
        )
        return row is not None

    async def restore_video(self, video_id: int) -> bool:
        row = await self.pool.fetchval(
            "UPDATE videos SET active = TRUE WHERE id = $1 AND NOT active RETURNING id", video_id
        )
        return row is not None

    async def next_video(self) -> Video | None:
        """Least-recently-used rotation: fewest sends first, oldest send as tiebreaker."""
        row = await self.pool.fetchrow(
            f"""SELECT {VIDEO_COLUMNS} FROM videos
                WHERE active
                ORDER BY times_sent ASC, last_sent_at ASC NULLS FIRST, id ASC
                LIMIT 1"""
        )
        return Video(**dict(row)) if row else None

    async def get_video(self, video_id: int) -> Video | None:
        row = await self.pool.fetchrow(
            f"SELECT {VIDEO_COLUMNS} FROM videos WHERE id = $1", video_id
        )
        return Video(**dict(row)) if row else None

    async def list_videos(self, limit: int = 20) -> list[Video]:
        rows = await self.pool.fetch(
            f"""SELECT {VIDEO_COLUMNS} FROM videos WHERE active
                ORDER BY times_sent ASC, id ASC LIMIT $1""",
            limit,
        )
        return [Video(**dict(r)) for r in rows]

    async def list_removed(self, limit: int = 20) -> list[Video]:
        rows = await self.pool.fetch(
            f"""SELECT {VIDEO_COLUMNS} FROM videos WHERE NOT active
                ORDER BY id DESC LIMIT $1""",
            limit,
        )
        return [Video(**dict(r)) for r in rows]

    async def count_videos(self) -> int:
        return int(await self.pool.fetchval("SELECT count(*) FROM videos WHERE active") or 0)

    async def mark_video_sent(self, video_id: int) -> None:
        await self.pool.execute(
            "UPDATE videos SET times_sent = times_sent + 1, last_sent_at = now() WHERE id = $1",
            video_id,
        )

    # ---------- groups ----------

    async def upsert_group(self, chat_id: int, title: str | None) -> None:
        await self.pool.execute(
            """INSERT INTO groups (chat_id, title) VALUES ($1, $2)
               ON CONFLICT (chat_id) DO UPDATE
                 SET title = EXCLUDED.title, active = TRUE, last_error = NULL""",
            chat_id, title,
        )

    async def deactivate_group(self, chat_id: int, reason: str | None = None) -> None:
        await self.pool.execute(
            "UPDATE groups SET active = FALSE, last_error = $1 WHERE chat_id = $2",
            reason, chat_id,
        )

    async def active_groups(self) -> list[Group]:
        rows = await self.pool.fetch(
            "SELECT chat_id, title FROM groups WHERE active ORDER BY added_at ASC, chat_id ASC"
        )
        return [Group(chat_id=r["chat_id"], title=r["title"]) for r in rows]

    async def count_groups(self) -> int:
        return int(await self.pool.fetchval("SELECT count(*) FROM groups WHERE active") or 0)

    # ---------- log ----------

    async def log_send(
        self, video_id: int | None, chat_id: int | None, status: str, detail: str | None = None
    ) -> None:
        await self.pool.execute(
            "INSERT INTO send_log (video_id, chat_id, status, detail) VALUES ($1, $2, $3, $4)",
            video_id, chat_id, status, detail,
        )

    # ---------- settings ----------

    async def get_setting(self, key: str) -> str | None:
        return await self.pool.fetchval("SELECT value FROM settings WHERE key = $1", key)

    async def set_setting(self, key: str, value: str) -> None:
        await self.pool.execute(
            """INSERT INTO settings (key, value) VALUES ($1, $2)
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
            key, value,
        )

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
        await self.set_setting("last_run_at", utcnow().isoformat(timespec="seconds"))
