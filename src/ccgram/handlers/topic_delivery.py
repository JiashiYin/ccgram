"""Helpers for topic-aware Telegram delivery with deleted-thread recovery."""

import asyncio
from pathlib import Path
from typing import Any

import structlog
from telegram import Bot, Message
from telegram.error import BadRequest, RetryAfter, TelegramError

from ..session import session_manager
from .message_sender import rate_limit_send, rate_limit_send_message

logger = structlog.get_logger()


def is_thread_gone_error(exc: TelegramError) -> bool:
    """Return True when Telegram says the forum topic/thread no longer exists."""
    if isinstance(exc, BadRequest):
        msg = str(exc).lower()
        return "thread not found" in msg or "topic_id_invalid" in msg
    return False


def _topic_name_for_window(window_id: str) -> str:
    """Resolve a stable topic name for a tmux window."""
    display = session_manager.get_display_name(window_id)
    if display:
        return display
    state = session_manager.get_window_state(window_id)
    if state.cwd:
        name = Path(state.cwd).name
        if name:
            return name
    return window_id


async def recreate_notify_topic_binding(
    bot: Bot,
    user_id: int,
    window_id: str,
    old_thread_id: int,
) -> int | None:
    """Create a replacement topic for a notify-mode window and rebind it."""
    chat_id = session_manager.resolve_chat_id(user_id, old_thread_id)
    if chat_id >= 0:
        logger.warning(
            "Cannot recreate topic for window %s: chat_id %s is not a forum chat",
            window_id,
            chat_id,
        )
        return None

    topic_name = _topic_name_for_window(window_id)
    try:
        topic = await bot.create_forum_topic(chat_id=chat_id, name=topic_name)
    except RetryAfter as exc:
        retry_after = (
            exc.retry_after
            if isinstance(exc.retry_after, int)
            else int(exc.retry_after.total_seconds())
        )
        await asyncio.sleep(max(1, retry_after))
        try:
            topic = await bot.create_forum_topic(chat_id=chat_id, name=topic_name)
        except TelegramError:
            logger.exception(
                "Failed to recreate deleted notify topic for window %s in chat %d",
                window_id,
                chat_id,
            )
            return None
    except TelegramError:
        logger.exception(
            "Failed to recreate deleted notify topic for window %s in chat %d",
            window_id,
            chat_id,
        )
        return None

    from .cleanup import clear_topic_state

    await clear_topic_state(user_id, old_thread_id, bot, window_id=window_id)
    session_manager.unbind_thread(user_id, old_thread_id)
    session_manager.bind_thread(
        user_id, topic.message_thread_id, window_id, window_name=topic_name
    )
    session_manager.set_group_chat_id(user_id, topic.message_thread_id, chat_id)
    logger.info(
        "Recreated notify topic '%s' (thread=%d) in chat %d for window %s",
        topic_name,
        topic.message_thread_id,
        chat_id,
        window_id,
    )
    return topic.message_thread_id


async def _handle_deleted_binding(
    bot: Bot,
    *,
    user_id: int,
    window_id: str,
    thread_id: int,
) -> int | None:
    """Heal or clear a binding after Telegram confirmed the thread is gone."""
    if session_manager.get_notification_mode(window_id) == "notify":
        return await recreate_notify_topic_binding(bot, user_id, window_id, thread_id)
    from .cleanup import clear_topic_state

    await clear_topic_state(user_id, thread_id, bot, window_id=window_id)
    session_manager.unbind_thread(user_id, thread_id)
    logger.info(
        "Cleared deleted non-notify topic binding: window %s thread %d user %d",
        window_id,
        thread_id,
        user_id,
    )
    return None


async def recover_failed_bound_message_delivery(
    bot: Bot,
    *,
    user_id: int,
    window_id: str,
    thread_id: int,
) -> int | None:
    """Probe a failed send to confirm deletion, then heal or clear the binding."""
    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    try:
        await bot.unpin_all_forum_topic_messages(
            chat_id=chat_id,
            message_thread_id=thread_id,
        )
    except TelegramError as exc:
        if is_thread_gone_error(exc):
            return await _handle_deleted_binding(
                bot,
                user_id=user_id,
                window_id=window_id,
                thread_id=thread_id,
            )
    return None


async def send_bound_message(
    bot: Bot,
    *,
    user_id: int,
    window_id: str,
    thread_id: int | None,
    text: str,
    **kwargs: Any,
) -> tuple[Message | None, int | None]:
    """Send a formatted message, recreating notify topics if the thread is gone."""
    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    sent = await rate_limit_send_message(
        bot,
        chat_id,
        text,
        message_thread_id=thread_id,
        **kwargs,
    )
    if sent is not None or thread_id is None:
        return sent, thread_id

    new_thread_id = await _handle_deleted_binding(
        bot,
        user_id=user_id,
        window_id=window_id,
        thread_id=thread_id,
    )
    if new_thread_id is None:
        return None, thread_id

    new_chat_id = session_manager.resolve_chat_id(user_id, new_thread_id)
    resent = await rate_limit_send_message(
        bot,
        new_chat_id,
        text,
        message_thread_id=new_thread_id,
        **kwargs,
    )
    return resent, new_thread_id


async def send_plain_bound_message(
    bot: Bot,
    *,
    user_id: int,
    window_id: str,
    thread_id: int | None,
    text: str,
    **kwargs: Any,
) -> tuple[Message | None, int | None]:
    """Send a plain-text message, recreating notify topics if the thread is gone."""
    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    await rate_limit_send(chat_id)
    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=text,
            message_thread_id=thread_id,
            **kwargs,
        )
        return sent, thread_id
    except TelegramError as exc:
        if thread_id is None or not is_thread_gone_error(exc):
            logger.error("Failed to send plain message to %s: %s", chat_id, exc)
            return None, thread_id

    new_thread_id = await _handle_deleted_binding(
        bot,
        user_id=user_id,
        window_id=window_id,
        thread_id=thread_id,
    )
    if new_thread_id is None:
        return None, thread_id

    new_chat_id = session_manager.resolve_chat_id(user_id, new_thread_id)
    await rate_limit_send(new_chat_id)
    resent = await bot.send_message(
        chat_id=new_chat_id,
        text=text,
        message_thread_id=new_thread_id,
        **kwargs,
    )
    return resent, new_thread_id
