"""Tests for deleted-topic recovery during outbound delivery."""

from unittest.mock import AsyncMock, MagicMock, patch
from telegram import Message
from telegram.error import BadRequest

from ccgram.handlers.topic_delivery import (
    recreate_notify_topic_binding,
    send_plain_bound_message,
)


class TestRecreateNotifyTopicBinding:
    async def test_recreates_topic_and_rebinds_window(self) -> None:
        bot = AsyncMock()
        topic = MagicMock()
        topic.message_thread_id = 248
        bot.create_forum_topic.return_value = topic

        with (
            patch("ccgram.handlers.topic_delivery.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.cleanup.clear_topic_state",
                new_callable=AsyncMock,
            ) as mock_cleanup,
        ):
            mock_sm.resolve_chat_id.return_value = -100500
            mock_sm.get_display_name.return_value = "helloworld-2"

            new_thread_id = await recreate_notify_topic_binding(bot, 100, "@2", 212)

        assert new_thread_id == 248
        bot.create_forum_topic.assert_called_once_with(
            chat_id=-100500, name="helloworld-2"
        )
        mock_cleanup.assert_called_once_with(100, 212, bot, window_id="@2")
        mock_sm.unbind_thread.assert_called_once_with(100, 212)
        mock_sm.bind_thread.assert_called_once_with(
            100, 248, "@2", window_name="helloworld-2"
        )
        mock_sm.set_group_chat_id.assert_called_once_with(100, 248, -100500)


class TestSendPlainBoundMessage:
    async def test_recreates_notify_topic_and_retries_send(self) -> None:
        bot = AsyncMock()
        sent = AsyncMock(spec=Message)
        bot.send_message.side_effect = [
            BadRequest("Message thread not found"),
            sent,
        ]

        with (
            patch("ccgram.handlers.topic_delivery.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.topic_delivery.recreate_notify_topic_binding",
                new_callable=AsyncMock,
                return_value=248,
            ) as mock_recreate,
        ):
            mock_sm.resolve_chat_id.return_value = -100500
            mock_sm.get_notification_mode.return_value = "notify"

            result, thread_id = await send_plain_bound_message(
                bot,
                user_id=100,
                window_id="@2",
                thread_id=212,
                text="Do you trust the contents of this directory?",
            )

        assert result is sent
        assert thread_id == 248
        assert bot.send_message.call_count == 2
        assert bot.send_message.call_args_list[0].kwargs["message_thread_id"] == 212
        assert bot.send_message.call_args_list[1].kwargs["message_thread_id"] == 248
        mock_recreate.assert_called_once_with(bot, 100, "@2", 212)

    async def test_non_notify_deleted_topic_does_not_recreate(self) -> None:
        bot = AsyncMock()
        bot.send_message.side_effect = BadRequest("Message thread not found")

        with (
            patch("ccgram.handlers.topic_delivery.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.cleanup.clear_topic_state",
                new_callable=AsyncMock,
            ) as mock_cleanup,
        ):
            mock_sm.resolve_chat_id.return_value = -100500
            mock_sm.get_notification_mode.return_value = "interactive"

            result, thread_id = await send_plain_bound_message(
                bot,
                user_id=100,
                window_id="@2",
                thread_id=212,
                text="prompt",
            )

        assert result is None
        assert thread_id == 212
        mock_cleanup.assert_called_once_with(100, 212, bot, window_id="@2")
        mock_sm.unbind_thread.assert_called_once_with(100, 212)
