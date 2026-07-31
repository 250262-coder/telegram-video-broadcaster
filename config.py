"""Environment-backed configuration.

ADMIN_IDS and VAULT_CHAT_ID may be left blank on the first run. The bot then
starts in *setup mode*, where /id is the only thing it does — which solves the
chicken-and-egg problem of needing your Telegram id before you can configure it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    pass


# Values copied straight out of .env.example — treated as "not filled in yet"
# so the bot drops into setup mode instead of trying to post into a fake chat.
PLACEHOLDER_VAULT = "-1001234567890"
PLACEHOLDER_ADMIN = "123456789"
PLACEHOLDER_TOKEN = "123456789:AAExampleTokenReplaceMe"


def _parse_ids(raw: str) -> list[int]:
    cleaned = raw.replace(",", " ").split()
    out: list[int] = []
    for chunk in cleaned:
        try:
            out.append(int(chunk))
        except ValueError as exc:
            raise ConfigError(f"ADMIN_IDS contains a non-numeric value: {chunk!r}") from exc
    return out


def _parse_float(name: str, default: float, minimum: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


@dataclass(frozen=True)
class Config:
    bot_token: str
    vault_chat_id: int | None
    admin_ids: list[int]
    interval_hours: float
    delay_between_groups: float
    caption_suffix: str
    db_path: str

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.admin_ids

    @property
    def is_configured(self) -> bool:
        return bool(self.admin_ids) and self.vault_chat_id is not None

    def missing(self) -> list[str]:
        gaps = []
        if not self.admin_ids:
            gaps.append("ADMIN_IDS")
        if self.vault_chat_id is None:
            gaps.append("VAULT_CHAT_ID")
        return gaps


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip().strip('"').strip("'")
    if not token:
        raise ConfigError("BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")
    if token == PLACEHOLDER_TOKEN:
        raise ConfigError(
            "BOT_TOKEN is still the example value from .env.example.\n"
            "  Get a real one: open https://t.me/BotFather -> /newbot (or /mybots -> API Token)\n"
            "  then put it in .env as  BOT_TOKEN=<your token>"
        )
    if not re.fullmatch(r"\d{5,16}:[A-Za-z0-9_-]{30,}", token):
        raise ConfigError(
            f"BOT_TOKEN doesn't look like a Telegram token (got {len(token)} chars).\n"
            "  Expected the form  1234567890:AAH...  (digits, a colon, then ~35 characters).\n"
            "  Copy it again from @BotFather - it's easy to truncate."
        )

    vault_raw = os.getenv("VAULT_CHAT_ID", "").strip()
    vault_chat_id: int | None = None
    if vault_raw and vault_raw != PLACEHOLDER_VAULT:
        try:
            vault_chat_id = int(vault_raw)
        except ValueError as exc:
            raise ConfigError(
                f"VAULT_CHAT_ID must be a numeric id like -1001234567890, got {vault_raw!r}"
            ) from exc

    admin_raw = os.getenv("ADMIN_IDS", "").strip()
    admin_ids = [] if admin_raw == PLACEHOLDER_ADMIN else _parse_ids(admin_raw)

    return Config(
        bot_token=token,
        vault_chat_id=vault_chat_id,
        admin_ids=admin_ids,
        interval_hours=_parse_float("INTERVAL_HOURS", 4.0, 0.05),
        delay_between_groups=_parse_float("DELAY_BETWEEN_GROUPS", 2.0, 0.0),
        caption_suffix=os.getenv("CAPTION_SUFFIX", ""),
        db_path=os.getenv("DB_PATH", "data/broadcaster.db").strip() or "data/broadcaster.db",
    )
