"""Tests for auto-prompting newly created interactive topics."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccgram.bot import topic_created_handler


@pytest.fixture(autouse=True)
def _allow_user():
    with patch("ccgram.bot.is_user_allowed", return_value=True):
        yield


def _make_update(*, thread_id: int = 42, user_id: int = 100, chat_id: int = -100999):
    update = MagicMock()
    update.effective_user = MagicMock(id=user_id)
    update.effective_chat = MagicMock(id=chat_id)
    message = MagicMock()
    message.message_thread_id = thread_id
    message.chat.id = chat_id
    message.chat.type = "supergroup"
    message.forum_topic_created = MagicMock()
    message.forum_topic_created.name = "ops"
    update.message = message
    return update


def _make_context():
    context = MagicMock()
    context.user_data = {}
    context.bot = AsyncMock()
    return context


class TestTopicCreatedHandler:
    @patch("ccgram.bot._handle_unbound_topic", new_callable=AsyncMock)
    @patch("ccgram.bot.session_manager")
    async def test_prompts_unbound_new_topic(
        self,
        mock_sm: MagicMock,
        mock_handle_unbound: AsyncMock,
    ) -> None:
        mock_sm.get_window_for_thread.return_value = None

        update = _make_update()
        context = _make_context()

        await topic_created_handler(update, context)

        mock_handle_unbound.assert_awaited_once_with(
            100,
            42,
            "",
            context.user_data,
            update.message,
        )

    @patch("ccgram.bot.is_user_allowed", return_value=False)
    @patch("ccgram.bot._handle_unbound_topic", new_callable=AsyncMock)
    async def test_caches_bot_created_topic_without_prompting(
        self,
        mock_handle_unbound: AsyncMock,
        _allowed: MagicMock,
    ) -> None:
        from ccgram.handlers.topic_emoji import _topic_names

        update = _make_update(user_id=999)
        context = _make_context()

        await topic_created_handler(update, context)

        assert _topic_names[(-100999, 42)] == "ops"
        mock_handle_unbound.assert_not_awaited()
