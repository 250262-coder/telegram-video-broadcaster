"""Entrypoint. Long polling + an interval broadcast job."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from broadcaster import Broadcaster
from config import ConfigError, load_config
from db import Database
from handlers import build_router
from scheduling import compute_first_run, install_job

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("bot")


async def main() -> None:
    cfg = load_config()

    db = Database(cfg.db_path)
    await db.connect()

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    scheduler = AsyncIOScheduler(timezone="UTC")
    broadcaster = Broadcaster(bot, db, cfg)

    async def scheduled_cycle() -> None:
        """Runs on the interval and reports the outcome to admins."""
        result = await broadcaster.run_cycle(trigger="schedule")
        if result.sent == 0 and result.failed == 0 and result.note:
            return  # nothing to say: paused, or empty vault/groups
        for admin_id in cfg.admin_ids:
            try:
                await bot.send_message(admin_id, result.summary())
            except Exception as exc:
                log.warning("Could not notify admin %s: %s", admin_id, exc)

    dp.include_router(build_router(cfg, db, broadcaster, scheduler, scheduled_cycle))

    hours = await db.interval_hours(cfg.interval_hours)
    install_job(scheduler, scheduled_cycle, hours, await compute_first_run(db, hours))
    scheduler.start()

    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError:
        scheduler.shutdown(wait=False)
        await db.close()
        await bot.session.close()
        raise SystemExit(
            "\nTelegram rejected the bot token (Unauthorized).\n"
            "  - Check BOT_TOKEN in .env matches what @BotFather shows under /mybots.\n"
            "  - If you ever pressed 'Revoke token', the old one stopped working.\n"
        )

    if cfg.is_configured:
        log.info(
            "Running as @%s | vault=%s | interval=%sh | videos=%s | groups=%s",
            me.username,
            cfg.vault_chat_id,
            hours,
            await db.count_videos(),
            await db.count_groups(),
        )
    else:
        log.warning(
            "\n%s\n  SETUP MODE — missing in .env: %s\n"
            "  Open https://t.me/%s and send /id, then forward a post from your\n"
            "  vault channel to it. Paste both numbers into .env and restart.\n%s",
            "=" * 70, ", ".join(cfg.missing()), me.username, "=" * 70,
        )

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        await db.close()
        await bot.session.close()
        log.info("Stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ConfigError as exc:
        raise SystemExit(f"Config error: {exc}")
    except (KeyboardInterrupt, SystemExit):
        pass
