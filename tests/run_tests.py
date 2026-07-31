"""Offline test suite. No Telegram account and no Postgres server needed.

    python tests/run_tests.py

Two layers:
  1. Every SQL string in db.py is parsed by libpg_query (the real PostgreSQL
     parser) if `pglast` is installed, so syntax errors can't reach production.
  2. Behaviour — rotation, albums, removals, retries, migrations — runs against
     sqlite through a shim that mimics asyncpg's interface.

The shim can't catch Postgres-specific semantics, so the live connection is still
proved by `python check_db.py`.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import asyncpg  # noqa: E402

PASS, FAIL = "  ok  ", " FAIL "
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"[{PASS if condition else FAIL}] {label}{'' if condition else '  <- ' + detail}")
    if not condition:
        failures.append(label)


# --------------------------------------------------------------------------
# 1. SQL syntax, against the real PostgreSQL grammar
# --------------------------------------------------------------------------

def check_sql() -> None:
    try:
        import pglast
    except ImportError:
        print("[ skip ] pglast not installed — skipping PostgreSQL syntax check")
        print("         pip install pglast   to enable it\n")
        return

    src = (ROOT / "db.py").read_text()
    tree = ast.parse(src)
    leader = re.search(r'LEADER_ONLY = """(.*?)"""', src, re.S).group(1)
    columns = re.search(r'VIDEO_COLUMNS = "(.*?)"', src).group(1)

    nested = {id(c) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)
              for c in ast.walk(n) if isinstance(c, ast.Constant)}

    stmts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in nested:
            text = node.value.strip()
            if any(text.upper().startswith(k) for k in ("SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER")):
                stmts.append(text)
        elif isinstance(node, ast.JoinedStr):
            parts = []
            for value in node.values:
                if isinstance(value, ast.Constant):
                    parts.append(value.value)
                elif isinstance(value, ast.FormattedValue) and isinstance(value.value, ast.Name):
                    parts.append(leader if value.value.id == "LEADER_ONLY" else columns)
                else:
                    parts.append(columns)
            text = "".join(parts).strip()
            if any(text.upper().startswith(k) for k in ("SELECT", "INSERT", "UPDATE", "DELETE")):
                stmts.append(text)

    bad = []
    for stmt in stmts:
        try:
            pglast.parse_sql(stmt)
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{' '.join(stmt.split())[:70]} -> {exc}")
    check(f"{len(stmts)} SQL statements parse as PostgreSQL", not bad, "; ".join(bad[:2]))


# --------------------------------------------------------------------------
# 2. asyncpg-shaped shim over sqlite
# --------------------------------------------------------------------------

DDL_SUBSTITUTIONS = {
    "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "TIMESTAMPTZ NOT NULL DEFAULT now()": "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
    "TIMESTAMPTZ": "TEXT",
    "BOOLEAN NOT NULL DEFAULT TRUE": "INTEGER NOT NULL DEFAULT 1",
    # sqlite has no ADD COLUMN IF NOT EXISTS; the CREATE above already has it
    "ALTER TABLE videos ADD COLUMN IF NOT EXISTS media_group_id TEXT;": "",
}


def translate(sql: str, args: tuple = ()) -> tuple[str, tuple]:
    """$n -> ?, reordering args by textual position (asyncpg binds by number)."""
    for src, dst in DDL_SUBSTITUTIONS.items():
        sql = sql.replace(src, dst)
    sql = sql.replace("now()", "CURRENT_TIMESTAMP")
    order = [int(n) for n in re.findall(r"\$(\d+)", sql)]
    sql = re.sub(r"\$(\d+)", "?", sql)
    return sql, tuple(args[n - 1] for n in order)


class FakeConn:
    def __init__(self, raw: sqlite3.Connection) -> None:
        self.raw = raw

    async def execute(self, sql, *args):
        sql, bound = translate(sql, args)
        if args:
            self.raw.execute(sql, bound)
        else:
            self.raw.executescript(sql)
        self.raw.commit()

    async def fetchval(self, sql, *args):
        sql, bound = translate(sql, args)
        row = self.raw.execute(sql, bound).fetchone()
        self.raw.commit()
        return row[0] if row else None

    async def fetchrow(self, sql, *args):
        sql, bound = translate(sql, args)
        row = self.raw.execute(sql, bound).fetchone()
        self.raw.commit()
        return dict(row) if row else None

    async def fetch(self, sql, *args):
        sql, bound = translate(sql, args)
        rows = self.raw.execute(sql, bound).fetchall()
        self.raw.commit()
        return [dict(r) for r in rows]


class FakePool(FakeConn):
    @contextlib.asynccontextmanager
    async def acquire(self):
        yield FakeConn(self.raw)

    async def close(self):
        self.raw.close()


async def _fake_create_pool(dsn, **_kwargs):
    raw = sqlite3.connect(_fake_create_pool.path)
    raw.row_factory = sqlite3.Row
    return FakePool(raw)


_fake_create_pool.path = ""
asyncpg.create_pool = _fake_create_pool


class StubBot:
    """Records calls; `script` queues exceptions to raise, one per call."""

    def __init__(self) -> None:
        self.singles: list[tuple] = []
        self.albums: list[tuple] = []
        self.script: list[Exception] = []

    async def copy_message(self, chat_id, from_chat_id, message_id, caption=None):
        self.singles.append((chat_id, message_id, caption))
        if self.script:
            raise self.script.pop(0)

    async def copy_messages(self, chat_id, from_chat_id, message_ids):
        self.albums.append((chat_id, tuple(message_ids)))
        if self.script:
            raise self.script.pop(0)


async def behaviour() -> None:
    _fake_create_pool.path = tempfile.mkdtemp() + "/test.db"
    os.environ.update(
        BOT_TOKEN="8123456789:AAH" + "x" * 32,
        DATABASE_URL="postgresql://u:p@aws-1-ap-south-1.pooler.supabase.com:5432/postgres",
        VAULT_CHAT_ID="-1004416731612",
        ADMIN_IDS="6651698857",
        DELAY_BETWEEN_GROUPS="0",
        CAPTION_SUFFIX="Buy now",
    )
    import config
    import db as dbmod
    import broadcaster as bc
    from aiogram.exceptions import (
        TelegramForbiddenError, TelegramMigrateToChat, TelegramRetryAfter,
    )

    cfg = config.load_config()
    check("config parses the session-pooler DSN", cfg.is_configured)

    d = dbmod.Database(cfg.database_url)
    await d.connect()
    check("schema applies cleanly", True)

    # --- every content type is accepted -------------------------------------
    await d.add_video(1, "a caption", "photo", "f1")
    await d.add_video(2, "hello world", "text", None)
    await d.add_video(3, None, "document", "f3")
    await d.add_video(4, "clip", "video", "f4")
    check("text / photo / video / document all stored", await d.count_videos() == 4)
    check("duplicate message_id ignored", await d.add_video(1, "x", "photo", "f1") is False)

    # --- an album counts as ONE post ----------------------------------------
    for msg_id in (10, 11, 12):
        await d.add_video(msg_id, "album cap" if msg_id == 10 else None, "photo", f"f{msg_id}", "GRP1")
    check("3-part album adds 1 rotation entry", await d.count_videos() == 5,
          f"got {await d.count_videos()}")
    check("album parts retrievable", await d.album_message_ids("GRP1") == [10, 11, 12])
    check("album_size counts parts", await d.album_size("GRP1") == 3)

    await d.upsert_group(-1004416731612, "target")
    bot = StubBot()
    caster = bc.Broadcaster(bot, d, cfg)

    # --- caption suffix only where it is legal ------------------------------
    order = []
    for _ in range(5):
        result = await caster.run_cycle()
        order.append((result.video.kind, result.video.media_group_id))
    kinds = [k for k, _ in order]
    check("rotation visits all 5 posts once", sorted(kinds) == ["document", "photo", "photo", "text", "video"],
          str(kinds))

    photo_call = next(c for c in bot.singles if c[1] == 1)
    text_call = next(c for c in bot.singles if c[1] == 2)
    check("suffix appended to captionable post", photo_call[2] == "a caption\n\nBuy now", str(photo_call))
    check("no caption passed for a text post", text_call[2] is None, str(text_call))

    # --- album sent as one grouped copy -------------------------------------
    check("album sent via copy_messages", bot.albums and bot.albums[0][1] == (10, 11, 12),
          str(bot.albums))
    check("album not also sent as singles", all(c[1] not in (10, 11, 12) for c in bot.singles))

    # --- album stays one unit through rotation ------------------------------
    for _ in range(5):
        await caster.run_cycle()
    check("album repeats once per full rotation, still grouped",
          len(bot.albums) == 2 and bot.albums[1][1] == (10, 11, 12), str(bot.albums))

    # --- removing an album removes every part -------------------------------
    leader = next(v for v in await d.list_videos() if v.media_group_id == "GRP1")
    check("removing album leader", await d.deactivate_video(leader.id) is True)
    check("all album parts deactivated", await d.album_size("GRP1") == 3
          and not any(v.media_group_id == "GRP1" for v in await d.list_videos()))
    check("album absent from rotation", await d.count_videos() == 4)
    check("restore brings the album back", await d.restore_video(leader.id) is True
          and await d.count_videos() == 5)

    # --- failure handling ---------------------------------------------------
    await d.upsert_group(-501, "second")
    bot.script = [TelegramRetryAfter(method=None, message="flood", retry_after=0)]
    result = await caster.run_cycle()
    check("flood wait retried, nothing lost", result.failed == 0, result.summary())

    bot.script = [TelegramForbiddenError(method=None, message="kicked")]
    result = await caster.run_cycle()
    check("kicked group deactivated", result.failed == 1 and await d.count_groups() == 1,
          result.summary())

    bot.script = [TelegramMigrateToChat(method=None, message="up", migrate_to_chat_id=-1009999999999)]
    await caster.run_cycle()
    check("supergroup migration followed",
          [g.chat_id for g in await d.active_groups()] == [-1009999999999])

    # --- settings persist ---------------------------------------------------
    await d.set_interval_hours(5)
    await d.set_paused(True)
    check("interval persists", await d.interval_hours(4) == 5.0)
    check("pause persists", await d.is_paused() is True)
    check("paused cycle is skipped",
          "Paused" in ((await caster.run_cycle(trigger="schedule")).note or ""))
    await d.set_paused(False)
    check("restart anchor recorded", await d.last_run_at() is not None)

    await d.close()


def main() -> int:
    print("PostgreSQL syntax\n" + "-" * 60)
    check_sql()
    print("\nBehaviour\n" + "-" * 60)
    asyncio.run(behaviour())
    print("\n" + "-" * 60)
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
