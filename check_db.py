"""Verify DATABASE_URL before running the bot.

    python check_db.py

Separates 'the database URL is wrong' from 'the bot is broken', which are very
different problems with similar-looking symptoms.
"""

from __future__ import annotations

import asyncio
import re
import sys

from config import ConfigError, load_config
from db import Database


def explain(exc: Exception, dsn: str) -> str:
    text = f"{type(exc).__name__}: {exc}"
    low = text.lower()

    if "network is unreachable" in low or "unreachable" in low:
        if "pooler.supabase.com" not in dsn:
            return (
                "Cannot reach the host — this is the classic IPv6 problem.\n"
                "You're using the DIRECT Supabase host, which has no IPv4 address.\n"
                "Fix: Project Settings -> Database -> Connection string -> Session pooler.\n"
                "The host should end in .pooler.supabase.com and the user should look\n"
                "like postgres.<projectref> rather than plain postgres."
            )
        return "Cannot reach the host. Check your internet connection and the host spelling."

    if "password authentication failed" in low:
        return (
            "Wrong password.\n"
            "  - Did you leave the [YOUR-PASSWORD] brackets in? Remove them.\n"
            "  - Special characters must be percent-encoded (@ -> %40, # -> %23).\n"
            "  - Reset it under Project Settings -> Database -> Reset database password."
        )

    if "does not exist" in low and "role" in low:
        return (
            "That user doesn't exist.\n"
            "The session pooler needs the user postgres.<projectref>, not plain postgres.\n"
            "Copy the whole URI from the Session pooler tab rather than editing by hand."
        )

    if "name or service not known" in low or "getaddrinfo" in low:
        return "Host not found — check the hostname for typos."

    if "timeout" in low:
        return (
            "Connection timed out. If you're on a restricted network, port 5432 may be\n"
            "blocked; try the Transaction pooler on port 6543 instead."
        )

    return text


async def main() -> int:
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"\nConfig problem:\n  {exc}\n")
        return 1

    safe = re.sub(r"://([^:]+):[^@]*@", r"://\1:***@", cfg.database_url)
    print(f"Connecting to {safe}")

    db = Database(cfg.database_url)
    try:
        await db.connect()
    except Exception as exc:  # noqa: BLE001 - we want to explain anything that comes back
        print(f"\nFAILED\n\n{explain(exc, cfg.database_url)}\n")
        return 1

    try:
        version = await db.pool.fetchval("SELECT version()")
        tables = await db.pool.fetch(
            """SELECT table_name FROM information_schema.tables
               WHERE table_schema = 'public' ORDER BY table_name"""
        )
        print("\nOK — connected and schema is in place.")
        print(f"  server : {version.split(',')[0]}")
        print(f"  tables : {', '.join(t['table_name'] for t in tables)}")
        print(f"  videos : {await db.count_videos()}")
        print(f"  groups : {await db.count_groups()}")
        print("\nRun the bot with:  bash run.sh")
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
