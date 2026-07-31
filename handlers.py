"""Bot commands, vault ingestion, and group membership tracking."""

from __future__ import annotations

import asyncio
import html
import logging

from aiogram import F, Router
from aiogram.filters import (
    IS_MEMBER,
    IS_NOT_MEMBER,
    ChatMemberUpdatedFilter,
    Command,
    CommandObject,
)
from aiogram.types import ChatMemberUpdated, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from broadcaster import Broadcaster
from config import Config
from db import Database
from scheduling import CycleFunc, next_run, reschedule

log = logging.getLogger(__name__)

HELP = """<b>Video broadcaster</b>

Post videos into the vault channel and I register them automatically.
Every cycle I pick the least-recently-sent video and copy it to every group.

<b>Commands</b>
/status - counts, interval, next run
/videos - the rotation queue
/remove &lt;id&gt; - drop a video from rotation (/restore to undo)
/groups - active target groups
/sendnow [id] - broadcast right now (optionally a specific video id)
/interval &lt;hours&gt; - change the cadence, e.g. <code>/interval 3</code>
/pause - stop scheduled broadcasts
/resume - start them again
/here - register the current group manually
/id - show the ids of this chat and of anything forwarded to me
"""

SETUP = """<b>Setup mode</b> — I'm not configured yet.

Missing in <code>.env</code>: {missing}

<b>Your user id:</b> <code>{user_id}</code>  ← this goes in <code>ADMIN_IDS</code>

For <code>VAULT_CHAT_ID</code>: make me an admin of your private video channel,
then <b>forward any post from that channel to me here</b> and I'll show its id.

Fill both into <code>.env</code> and restart me.
"""


def _fmt_dt(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else "not scheduled"


def describe_origin(message: Message) -> str | None:
    """Where a forwarded message came from, as a display string with the id.

    Bot API 7.0+ reports this via forward_origin; older payloads used
    forward_from_chat. Both are handled so the id trick works either way.
    """
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        legacy = getattr(message, "forward_from_chat", None)
        if legacy is None:
            return None
        return f"<code>{legacy.id}</code> — {html.escape(legacy.title or legacy.type)}"

    chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
    if chat is not None:
        return f"<code>{chat.id}</code> — {html.escape(chat.title or chat.type)}"

    user = getattr(origin, "sender_user", None)
    if user is not None:
        return f"<code>{user.id}</code> — user {html.escape(user.full_name)}"

    hidden = getattr(origin, "sender_user_name", None)
    if hidden:
        return f"{html.escape(hidden)} (hides their account, id not shared)"
    return None


def _short(text: str | None, width: int = 45) -> str:
    if not text:
        return "<i>no caption</i>"
    flat = " ".join(text.split())
    clipped = flat if len(flat) <= width else flat[: width - 1] + "…"
    return html.escape(clipped)


def build_router(
    cfg: Config,
    db: Database,
    broadcaster: Broadcaster,
    scheduler: AsyncIOScheduler,
    cycle: CycleFunc,
) -> Router:
    router = Router(name="broadcaster")

    def is_admin(message: Message) -> bool:
        return cfg.is_admin(message.from_user.id if message.from_user else None)

    async def notify_admins(text: str) -> None:
        for admin_id in cfg.admin_ids:
            try:
                await broadcaster.bot.send_message(admin_id, text)
            except Exception as exc:  # admin never started a DM with the bot
                log.warning("Could not notify admin %s: %s", admin_id, exc)

    # ---------- vault ingestion ----------

    async def ingest(message: Message) -> None:
        if message.video:
            kind, file_id = "video", message.video.file_id
        elif message.animation:
            kind, file_id = "animation", message.animation.file_id
        elif message.video_note:
            kind, file_id = "video_note", message.video_note.file_id
        elif message.document and (message.document.mime_type or "").startswith("video/"):
            kind, file_id = "document", message.document.file_id
        else:
            if message.chat.id == cfg.vault_chat_id:
                log.info(
                    "Ignored a %s in the vault — only videos, GIFs, video notes and "
                    "video documents go into rotation.",
                    message.content_type,
                )
            return

        # Loud about mismatches: a wrong VAULT_CHAT_ID otherwise looks like the
        # bot silently doing nothing, which is painful to debug.
        if cfg.vault_chat_id is None:
            log.warning(
                "Got a %s from chat %s (%s) but VAULT_CHAT_ID is unset. "
                "If this is your vault, put VAULT_CHAT_ID=%s in .env",
                kind, message.chat.id, message.chat.title, message.chat.id,
            )
            return
        if message.chat.id != cfg.vault_chat_id:
            log.warning(
                "Ignoring %s from chat %s (%s) — configured vault is %s",
                kind, message.chat.id, message.chat.title, cfg.vault_chat_id,
            )
            return

        created = await db.add_video(message.message_id, message.caption, kind, file_id)
        if created:
            total = await db.count_videos()
            await notify_admins(
                f"➕ Added {kind} to rotation (msg {message.message_id}). Queue size: {total}."
            )

    MEDIA = F.video | F.animation | F.video_note | F.document
    router.channel_post.register(ingest, MEDIA)
    router.message.register(ingest, MEDIA, F.chat.type != "private")

    # ---------- group membership ----------

    @router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER))
    async def on_join(event: ChatMemberUpdated) -> None:
        if event.chat.type not in {"group", "supergroup"}:
            return
        await db.upsert_group(event.chat.id, event.chat.title)
        log.info("Added to group %s (%s)", event.chat.title, event.chat.id)
        await notify_admins(
            f"✅ Added to <b>{html.escape(event.chat.title or 'group')}</b> "
            f"(<code>{event.chat.id}</code>). Now targeting {await db.count_groups()} group(s)."
        )

    @router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_MEMBER >> IS_NOT_MEMBER))
    async def on_leave(event: ChatMemberUpdated) -> None:
        await db.deactivate_group(event.chat.id, "removed from chat")
        log.info("Removed from group %s", event.chat.id)
        await notify_admins(
            f"⛔️ Removed from <b>{html.escape(event.chat.title or 'group')}</b> "
            f"(<code>{event.chat.id}</code>)."
        )

    # ---------- ids (works before the bot is configured) ----------

    async def id_card(message: Message, header: str) -> None:
        chat = message.chat
        lines = [header, f"This chat: <code>{chat.id}</code> ({chat.type})"]
        if message.from_user:
            lines.append(f"You: <code>{message.from_user.id}</code>")
        origin = describe_origin(message)
        if origin:
            lines.append(f"Forwarded from: {origin}")
        if not cfg.is_configured:
            lines.append("")
            lines.append(f"Still missing in .env: <b>{', '.join(cfg.missing())}</b>")
        await message.answer("\n".join(lines))

    @router.message(Command("id"))
    async def cmd_id(message: Message) -> None:
        # Public in DMs (you need it before ADMIN_IDS exists); admins-only in groups.
        if message.chat.type != "private" and not is_admin(message):
            return
        await id_card(message, "<b>IDs</b>")

    # Any forwarded message in a DM: the easiest way to grab a channel id.
    async def on_forward(message: Message) -> None:
        await id_card(message, "<b>Forwarded message</b>")

    router.message.register(on_forward, F.chat.type == "private", F.forward_origin)

    # ---------- commands ----------

    @router.message(Command("start", "help"))
    async def cmd_help(message: Message) -> None:
        if not cfg.is_configured:
            await message.answer(
                SETUP.format(
                    missing=", ".join(cfg.missing()),
                    user_id=message.from_user.id if message.from_user else "unknown",
                )
            )
            return
        if not is_admin(message):
            return
        await message.answer(HELP)

    @router.message(Command("here"))
    async def cmd_here(message: Message) -> None:
        if not is_admin(message):
            return
        if message.chat.type not in {"group", "supergroup"}:
            await message.answer("Run this inside the group you want to target.")
            return
        await db.upsert_group(message.chat.id, message.chat.title)
        await message.answer(f"Registered. Targeting {await db.count_groups()} group(s).")

    @router.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        if not is_admin(message):
            return
        hours = await db.interval_hours(cfg.interval_hours)
        paused = await db.is_paused()
        upcoming = await db.next_video()
        await message.answer(
            "<b>Status</b>\n"
            f"Videos in rotation: <b>{await db.count_videos()}</b>\n"
            f"Target groups: <b>{await db.count_groups()}</b>\n"
            f"Interval: every <b>{hours}h</b>\n"
            f"State: <b>{'paused' if paused else 'running'}</b>\n"
            f"Next run: <b>{_fmt_dt(next_run(scheduler))}</b>\n"
            f"Last run: <b>{_fmt_dt(await db.last_run_at())}</b>\n"
            f"Up next: {('#' + str(upcoming.id) + ' ' + _short(upcoming.caption)) if upcoming else '<i>nothing</i>'}"
        )

    @router.message(Command("videos"))
    async def cmd_videos(message: Message) -> None:
        if not is_admin(message):
            return
        videos = await db.list_videos(limit=20)
        if not videos:
            await message.answer("Vault is empty. Post a video into the vault channel.")
            return
        lines = [
            f"<code>#{v.id}</code> {_short(v.caption)} — sent {v.times_sent}x" for v in videos
        ]
        await message.answer(
            f"<b>Rotation queue</b> (next first, {await db.count_videos()} total)\n" + "\n".join(lines)
        )

    @router.message(Command("remove", "rm"))
    async def cmd_remove(message: Message, command: CommandObject) -> None:
        if not is_admin(message):
            return
        raw = (command.args or "").strip().lstrip("#")
        if not raw.isdigit():
            await message.answer(
                "Usage: <code>/remove 3</code> — the number from /videos.\n"
                "Undo with <code>/restore 3</code>."
            )
            return
        video_id = int(raw)
        video = await db.get_video(video_id)
        if video is None:
            await message.answer(f"No video with id {video_id}. Check /videos.")
            return
        if not await db.deactivate_video(video_id):
            await message.answer(f"Video #{video_id} was already removed.")
            return
        await message.answer(
            f"Removed #{video_id} ({_short(video.caption)}) from rotation. "
            f"{await db.count_videos()} left.\nUndo: <code>/restore {video_id}</code>"
        )

    @router.message(Command("restore"))
    async def cmd_restore(message: Message, command: CommandObject) -> None:
        if not is_admin(message):
            return
        raw = (command.args or "").strip().lstrip("#")
        if not raw.isdigit():
            removed = await db.list_removed()
            if not removed:
                await message.answer("Nothing has been removed.")
                return
            lines = [f"<code>#{v.id}</code> {_short(v.caption)}" for v in removed]
            await message.answer(
                "<b>Removed videos</b>\n" + "\n".join(lines) + "\n\nRestore with <code>/restore 3</code>"
            )
            return
        video_id = int(raw)
        if not await db.restore_video(video_id):
            await message.answer(f"Video #{video_id} isn't in the removed list.")
            return
        await message.answer(f"Restored #{video_id}. {await db.count_videos()} in rotation.")

    @router.message(Command("groups"))
    async def cmd_groups(message: Message) -> None:
        if not is_admin(message):
            return
        groups = await db.active_groups()
        if not groups:
            await message.answer("No groups yet. Add me to a group as a member with send rights.")
            return
        lines = [
            f"• {html.escape(g.title or 'untitled')} <code>{g.chat_id}</code>" for g in groups
        ]
        await message.answer(f"<b>{len(groups)} active group(s)</b>\n" + "\n".join(lines))

    @router.message(Command("sendnow"))
    async def cmd_sendnow(message: Message, command: CommandObject) -> None:
        if not is_admin(message):
            return
        video_id: int | None = None
        if command.args:
            raw = command.args.strip().lstrip("#")
            if not raw.isdigit():
                await message.answer("Usage: <code>/sendnow</code> or <code>/sendnow 12</code>")
                return
            video_id = int(raw)
            if await db.get_video(video_id) is None:
                await message.answer(f"No video with id {video_id}. Check /videos.")
                return

        await message.answer("Broadcasting now, I'll report back when done…")

        async def run() -> None:
            result = await broadcaster.run_cycle(video_id=video_id, trigger="manual")
            await message.answer(result.summary())

        asyncio.create_task(run())

    @router.message(Command("interval"))
    async def cmd_interval(message: Message, command: CommandObject) -> None:
        if not is_admin(message):
            return
        raw = (command.args or "").strip()
        try:
            hours = float(raw)
        except ValueError:
            await message.answer("Usage: <code>/interval 3</code> (hours, decimals allowed)")
            return
        if not 0.05 <= hours <= 168:
            await message.answer("Pick something between 0.05 and 168 hours.")
            return

        await db.set_interval_hours(hours)
        first = reschedule(scheduler, cycle, hours)
        await message.answer(f"Interval set to every <b>{hours}h</b>. Next run {_fmt_dt(first)}.")

    @router.message(Command("pause"))
    async def cmd_pause(message: Message) -> None:
        if not is_admin(message):
            return
        await db.set_paused(True)
        await message.answer("Paused. Scheduled broadcasts will be skipped; /sendnow still works.")

    @router.message(Command("resume"))
    async def cmd_resume(message: Message) -> None:
        if not is_admin(message):
            return
        await db.set_paused(False)
        await message.answer(f"Resumed. Next run {_fmt_dt(next_run(scheduler))}.")

    # ---------- catch-all, registered last so it only sees leftovers ----------
    # Turns aiogram's bare "Update is not handled" into something diagnosable.

    @router.channel_post()
    async def unhandled_channel_post(message: Message) -> None:
        log.info(
            "Unhandled channel post: chat=%s (%s) type=%s",
            message.chat.id, message.chat.title, message.content_type,
        )

    @router.message()
    async def unhandled_message(message: Message) -> None:
        log.info(
            "Unhandled message: chat=%s (%s) type=%s text=%r",
            message.chat.id, message.chat.type, message.content_type,
            (message.text or "")[:60],
        )
        # Only answer admins in DMs — never chatter in a target group.
        if message.chat.type == "private" and is_admin(message) and (message.text or "").startswith("/"):
            await message.answer("Unknown command. Send /help for the list.")

    return router
