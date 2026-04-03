"""Helpers for topic-level bot routing in shared Telegram chats."""

from __future__ import annotations

import re
import time
from typing import Literal

from telegram import Bot, Message
from telegram.constants import MessageEntityType
from telegram.error import TelegramError

from .topic_emoji import strip_emoji_prefix

_DEFAULT_RESPONDER_RE = re.compile(
    r"^(?P<base>.*?)(?:\s+\[(?:ccgram:)?(?P<bot>@[A-Za-z0-9_]{5,})\])?$"
)
_CHAT_BOT_CACHE_TTL_SECONDS = 60.0
_chat_bot_cache: dict[int, tuple[float, tuple[str, ...]]] = {}


def extract_default_responder(display_name: str | object) -> tuple[str, str | None]:
    """Return the base topic name and optional explicit default responder."""
    clean_name = display_name if isinstance(display_name, str) else ""
    clean = strip_emoji_prefix(clean_name).strip()
    match = _DEFAULT_RESPONDER_RE.match(clean)
    if not match:
        return clean, None
    base = (match.group("base") or "").rstrip()
    responder = match.group("bot")
    return base or clean, responder


def has_legacy_default_responder(display_name: str) -> bool:
    """Return whether a topic still uses the legacy ``[ccgram:@Bot]`` marker."""
    clean = strip_emoji_prefix(display_name).strip()
    return "[ccgram:@" in clean


def format_topic_name_with_default_responder(
    display_name: str, bot_username: str | None
) -> str:
    """Attach or remove the default responder marker from a clean topic name."""
    base, _ = extract_default_responder(display_name)
    if not isinstance(bot_username, str) or not bot_username:
        return base
    username = bot_username if bot_username.startswith("@") else f"@{bot_username}"
    return f"{base} [{username}]"


def leading_bot_target(message: Message) -> str | None:
    """Return the leading bot mention target (e.g. ``@mybot``) when present."""
    text = message.text or ""
    entities = list(message.entities or [])
    if not text or not entities:
        return None

    first = entities[0]
    if first.offset != 0:
        return None

    if first.type == MessageEntityType.MENTION:
        return text[: first.length]

    if first.type == MessageEntityType.TEXT_MENTION:
        entity_user = getattr(first, "user", None)
        username = getattr(entity_user, "username", None)
        if username:
            return f"@{username}"

    return None


def leading_command_target(command_text: str) -> str | None:
    """Return the bot target encoded in a slash command like ``/cmd@mybot``."""
    parts = command_text.split(None, 1)
    if not parts:
        return None
    command_word = parts[0]
    if "@" not in command_word:
        return None
    _base, _sep, target = command_word.partition("@")
    return f"@{target}" if target else None


async def get_admin_bot_usernames(bot: Bot, chat_id: int) -> tuple[str, ...]:
    """Return bot usernames among chat administrators, cached briefly."""
    now = time.monotonic()
    cached = _chat_bot_cache.get(chat_id)
    if cached and now - cached[0] < _CHAT_BOT_CACHE_TTL_SECONDS:
        return cached[1]

    try:
        members = await bot.get_chat_administrators(chat_id)
    except (TelegramError, TypeError, AttributeError):
        return ()

    bot_usernames = sorted(
        {
            f"@{member.user.username}"
            for member in members
            if getattr(member.user, "is_bot", False)
            and getattr(member.user, "username", None)
        },
        key=str.casefold,
    )
    result = tuple(bot_usernames)
    _chat_bot_cache[chat_id] = (now, result)
    return result


async def classify_topic_routing(
    *,
    bot: Bot,
    chat_id: int,
    display_name: str,
    bot_username: str | None,
    explicit_self_target: bool,
) -> tuple[Literal["handle", "ignore"], bool]:
    """Classify whether this bot should handle an incoming topic message.

    Returns ``(decision, should_claim_topic_default)`` where claim_default
    applies after an explicit self-target when the topic has no existing
    default responder marker.
    """
    _base_name, default_responder = extract_default_responder(display_name)
    bot_usernames = await get_admin_bot_usernames(bot, chat_id)
    shared_chat = len(bot_usernames) > 1

    if explicit_self_target:
        should_claim = has_legacy_default_responder(display_name) or (
            shared_chat and default_responder is None
        )
        return "handle", should_claim

    if default_responder:
        if bot_username and default_responder.casefold() == f"@{bot_username}".casefold():
            return "handle", False
        return "ignore", False

    if shared_chat:
        return "ignore", False

    return "handle", False
