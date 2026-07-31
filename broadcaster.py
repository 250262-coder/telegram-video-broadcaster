"""Fan-out engine: copies one vault video into every active group, politely."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNotFound,
    TelegramRetryAfter,
)

from config import Config
from db import Database, Video

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
CAPTION_LIMIT = 1024


@dataclass
class CycleResult:
    video: Video | None = None
    sent: int = 0
    failed: int = 0
    dropped_groups: list[int] = field(default_factory=list)
    note: str | None = None

    def summary(self) -> str:
        if self.note:
            return self.note
        label = f"video #{self.video.id}" if self.video else "no video"
        parts = [f"Broadcast {label}: {self.sent} sent"]
        if self.failed:
            parts.append(f"{self.failed} failed")
        if self.dropped_groups:
            parts.append(f"{len(self.dropped_groups)} group(s) deactivated")
        return ", ".join(parts)


class Broadcaster:
    def __init__(self, bot: Bot, db: Database, cfg: Config) -> None:
        self.bot = bot
        self.db = db
        self.cfg = cfg
        self._lock = asyncio.Lock()

    def _caption_for(self, video: Video) -> str | None:
        """Only override the caption when a suffix is configured, so entities survive otherwise."""
        suffix = self.cfg.caption_suffix
        if not suffix:
            return None
        base = video.caption or ""
        merged = f"{base}\n\n{suffix}".strip() if base else suffix
        return merged[:CAPTION_LIMIT]

    async def run_cycle(
        self, *, video_id: int | None = None, trigger: str = "schedule"
    ) -> CycleResult:
        if self._lock.locked():
            log.warning("Cycle already running, skipping this %s trigger", trigger)
            return CycleResult(note="A broadcast is already in progress, skipped.")

        async with self._lock:
            if self.cfg.vault_chat_id is None:
                return CycleResult(note="VAULT_CHAT_ID is not set — still in setup mode.")

            if trigger == "schedule" and await self.db.is_paused():
                log.info("Paused, skipping scheduled cycle")
                return CycleResult(note="Paused, scheduled cycle skipped.")

            video = (
                await self.db.get_video(video_id) if video_id is not None else await self.db.next_video()
            )
            if video is None:
                await self.db.log_send(None, None, "skipped", "no active videos")
                return CycleResult(note="No videos in the vault yet, nothing to send.")

            groups = await self.db.active_groups()
            if not groups:
                await self.db.log_send(video.id, None, "skipped", "no active groups")
                return CycleResult(video=video, note="No active groups, nothing to send.")

            result = CycleResult(video=video)
            caption = self._caption_for(video)

            for index, group in enumerate(groups):
                ok, dropped, detail = await self._send_to_group(video, group.chat_id, caption)
                if ok:
                    result.sent += 1
                    await self.db.log_send(video.id, group.chat_id, "sent")
                else:
                    result.failed += 1
                    await self.db.log_send(video.id, group.chat_id, "failed", detail)
                    if dropped:
                        result.dropped_groups.append(group.chat_id)

                if index < len(groups) - 1 and self.cfg.delay_between_groups > 0:
                    await asyncio.sleep(self.cfg.delay_between_groups)

            await self.db.mark_video_sent(video.id)
            await self.db.touch_last_run()
            log.info(result.summary())
            return result

    async def _send_to_group(
        self, video: Video, chat_id: int, caption: str | None
    ) -> tuple[bool, bool, str | None]:
        """Returns (success, group_dropped, error_detail)."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                await self.bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=self.cfg.vault_chat_id,
                    message_id=video.message_id,
                    caption=caption,
                )
                return True, False, None

            except TelegramRetryAfter as exc:
                wait = exc.retry_after + 1
                log.warning("Flood limit on %s, waiting %ss (attempt %s)", chat_id, wait, attempt)
                await asyncio.sleep(wait)

            except TelegramMigrateToChat as exc:
                # A basic group was upgraded to a supergroup, which changes its id.
                # Follow the move so the target isn't silently lost.
                new_id = exc.migrate_to_chat_id
                log.info("Group %s migrated to supergroup %s, following", chat_id, new_id)
                await self.db.deactivate_group(chat_id, f"migrated to {new_id}")
                await self.db.upsert_group(new_id, None)
                chat_id = new_id

            except (TelegramForbiddenError, TelegramNotFound) as exc:
                # Kicked, chat deleted, or bot blocked. Stop trying this chat.
                reason = str(exc)[:200]
                log.warning("Deactivating group %s: %s", chat_id, reason)
                await self.db.deactivate_group(chat_id, reason)
                return False, True, reason

            except TelegramBadRequest as exc:
                reason = str(exc)[:200]
                log.error("Bad request for %s: %s", chat_id, reason)
                # A missing source message means the vault entry is stale, not a group problem.
                if "message to copy not found" in reason.lower():
                    await self.db.remove_video(video.message_id)
                return False, False, reason

            except Exception as exc:  # network hiccups, 5xx from Telegram
                reason = f"{type(exc).__name__}: {exc}"[:200]
                log.error("Send to %s failed (attempt %s): %s", chat_id, attempt, reason)
                if attempt == MAX_ATTEMPTS:
                    return False, False, reason
                await asyncio.sleep(2 * attempt)

        return False, False, "exhausted retries"
