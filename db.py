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
    media_group_id TEXT,
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

-- Additive migration for databases created before album support.
ALTER TABLE videos ADD COLUMN IF NOT EXISTS media_group_id TEXT;

CREATE INDEX IF NOT EXISTS idx_send_log_sent_at ON send_log (sent_at);
CREATE INDEX IF NOT EXISTS idx_videos_rotation ON videos (active, times_sent, last_sent_at);
CREATE INDEX IF NOT EXISTS idx_videos_album ON videos (media_group_id);
"""

VIDEO_COLUMNS = "id, message_id, caption, kind, times_sent, last_sent_at, media_group_id"

# An album arrives as several messages sharing a media_group_id. Only the lowest
# message_id represents the post in rotation; the rest are sent alongside it.
LEADER_ONLY = """
    (v.media_group_id IS NULL
     OR v.message_id = (SELECT min(v2.message_id) FROM videos v2
                        WHERE v2.media_group_id = v.media_group_id))
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Video:
    """One post in the rotation. For an album, the first message of the group."""

    id: int
    message_id: int
    caption: str | None
    kind: str
    times_sent: int
    last_sent_at: datetime | None
    media_group_id: str | None = None


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
        self,
        message_id: int,
        caption: str | None,
        kind: str,
        file_id: str | None,
        media_group_id: str | None = None,
    ) -> bool:
        """Returns True if newly inserted, False if this message_id was already known."""
        row = await self.pool.fetchval(
            """INSERT INTO videos (message_id, file_id, caption, kind, media_group_id)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (message_id) DO NOTHING
               RETURNING id""",
            message_id, file_id, caption, kind, media_group_id,
        )
        return row is not None

    async def album_size(self, media_group_id: str) -> int:
        """How many messages of this album we've stored so far."""
        return int(
            await self.pool.fetchval(
                "SELECT count(*) FROM videos WHERE media_group_id = $1", media_group_id
            )
            or 0
        )

    async def album_message_ids(self, media_group_id: str) -> list[int]:
        rows = await self.pool.fetch(
            """SELECT message_id FROM videos WHERE media_group_id = $1
               ORDER BY message_id ASC""",
            media_group_id,
        )
        return [r["message_id"] for r in rows]

    async def remove_video(self, message_id: int) -> bool:
        row = await self.pool.fetchval(
            "DELETE FROM videos WHERE message_id = $1 RETURNING id", message_id
        )
        return row is not None

    async def deactivate_video(self, video_id: int) -> bool:
        """Pull a post out of rotation, keeping the row so send_log stays meaningful.

        Removing an album removes all of its parts.
        """
        row = await self.pool.fetchval(
            """UPDATE videos SET active = FALSE
               WHERE active
                 AND (id = $1
                      OR (media_group_id IS NOT NULL
                          AND media_group_id = (SELECT media_group_id FROM videos WHERE id = $1)))
               RETURNING id""",
            video_id,
        )
        return row is not None

    async def restore_video(self, video_id: int) -> bool:
        row = await self.pool.fetchval(
            """UPDATE videos SET active = TRUE
               WHERE NOT active
                 AND (id = $1
                      OR (media_group_id IS NOT NULL
                          AND media_group_id = (SELECT media_group_id FROM videos WHERE id = $1)))
               RETURNING id""",
            video_id,
        )
        return row is not None

    async def next_video(self) -> Video | None:
        """Least-recently-used rotation: fewest sends first, oldest send as tiebreaker."""
        row = await self.pool.fetchrow(
            f"""SELECT {VIDEO_COLUMNS} FROM videos v
                WHERE active AND {LEADER_ONLY}
                ORDER BY times_sent ASC, last_sent_at ASC NULLS FIRST, id ASC
                LIMIT 1"""
        )
        return Video(**dict(row)) if row else None

    async def get_video(self, video_id: int) -> Video | None:
        row = await self.pool.fetchrow(
            f"SELECT {VIDEO_COLUMNS} FROM videos v WHERE id = $1", video_id
        )
        return Video(**dict(row)) if row else None

    async def list_videos(self, limit: int = 20) -> list[Video]:
        rows = await self.pool.fetch(
            f"""SELECT {VIDEO_COLUMNS} FROM videos v
                WHERE active AND {LEADER_ONLY}
                ORDER BY times_sent ASC, id ASC LIMIT $1""",
            limit,
        )
        return [Video(**dict(r)) for r in rows]

    async def list_removed(self, limit: int = 20) -> list[Video]:
        rows = await self.pool.fetch(
            f"""SELECT {VIDEO_COLUMNS} FROM videos v
                WHERE NOT active AND {LEADER_ONLY}
                ORDER BY id DESC LIMIT $1""",
            limit,
        )
        return [Video(**dict(r)) for r in rows]

    async def count_videos(self) -> int:
        return int(
            await self.pool.fetchval(
                f"SELECT count(*) FROM videos v WHERE active AND {LEADER_ONLY}"
            )
            or 0
        )

    async def mark_video_sent(self, video_id: int) -> None:
        """Bump the whole album together, so its parts stay in step in the rotation."""
        await self.pool.execute(
            """UPDATE videos SET times_sent = times_sent + 1, last_sent_at = now()
               WHERE id = $1
                  OR (media_group_id IS NOT NULL
                      AND media_group_id = (SELECT media_group_id FROM videos WHERE id = $1))""",
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
