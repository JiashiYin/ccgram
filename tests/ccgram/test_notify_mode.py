"""Focused tests for notify-mode routing and explicit interactive restores."""

from unittest.mock import AsyncMock, MagicMock, patch

from ccgram.bot import _extract_notify_summary, handle_new_message
from ccgram.handlers.directory_callbacks import _create_window_and_bind
from ccgram.handlers.directory_browser import UNBOUND_WINDOWS_KEY
from ccgram.handlers.recovery_callbacks import _create_and_bind_window
from ccgram.handlers.resume_command import _create_resume_window
from ccgram.handlers.restore_command import restore_command
from ccgram.handlers.window_callbacks import _handle_bind
from ccgram.handlers.user_state import PENDING_THREAD_ID, RECOVERY_WINDOW_ID
from ccgram.session_monitor import NewMessage
from ccgram.session import WindowState
def _make_query(thread_id: int = 42) -> MagicMock:
    query = MagicMock()
    query.answer = AsyncMock()
    query.message = MagicMock()
    query.message.chat.type = "supergroup"
    query.message.chat.id = -100999
    query.message.message_thread_id = thread_id
    return query


def _make_context(user_data: dict | None = None) -> MagicMock:
    context = MagicMock()
    context.user_data = user_data if user_data is not None else {}
    context.bot = AsyncMock()
    return context


class TestTelegramCreatedFlowsStayInteractive:
    async def test_directory_create_window_sets_interactive_mode(self) -> None:
        query = _make_query()
        context = _make_context({PENDING_THREAD_ID: 42})

        with (
            patch("ccgram.handlers.directory_callbacks.tmux_manager") as mock_tm,
            patch("ccgram.handlers.directory_callbacks.session_manager") as mock_sm,
            patch("ccgram.handlers.directory_callbacks.provider_registry") as mock_pr,
            patch("ccgram.handlers.directory_callbacks.safe_edit"),
            patch("ccgram.handlers.directory_callbacks._wait_for_shell_ready"),
        ):
            mock_tm.create_window = AsyncMock(
                return_value=(True, "Created", "topic", "@10")
            )
            mock_tm.stamp_pane_title = AsyncMock()
            mock_pr.get.return_value.capabilities.supports_hook = False
            mock_sm.get_window_state.return_value = MagicMock(cwd="")
            mock_sm.get_approval_mode.return_value = "normal"

            await _create_window_and_bind(
                query,
                100,
                "/tmp/project",
                "claude",
                "normal",
                context,
            )

        mock_sm.set_notification_mode.assert_called_once_with("@10", "interactive")

    async def test_window_rebind_sets_interactive_mode(self) -> None:
        query = _make_query()
        context = _make_context({PENDING_THREAD_ID: 42, UNBOUND_WINDOWS_KEY: ["@5"]})
        update = MagicMock()
        update.callback_query = MagicMock(message=query.message)
        update.message = None

        mock_window = MagicMock()
        mock_window.window_name = "project"
        mock_window.pane_current_command = "claude"
        mock_window.pane_tty = "/dev/ttys000"

        with (
            patch("ccgram.handlers.window_callbacks.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.window_callbacks.tmux_manager.find_window_by_id",
                new_callable=AsyncMock,
                return_value=mock_window,
            ),
            patch("ccgram.handlers.window_callbacks.safe_edit"),
            patch("ccgram.handlers.window_callbacks.format_topic_name_for_mode"),
            patch(
                "ccgram.providers.detect_provider_from_pane",
                new_callable=AsyncMock,
                return_value="claude",
            ),
            patch("ccgram.handlers.window_callbacks.clear_window_picker_state"),
        ):
            mock_sm.resolve_chat_id.return_value = -100
            mock_sm.get_approval_mode.return_value = "normal"
            await _handle_bind(query, 100, "wb:sel:0", update, context)

        mock_sm.set_notification_mode.assert_called_once_with("@5", "interactive")

    async def test_recovery_flow_sets_interactive_mode(self) -> None:
        query = _make_query()
        context = _make_context({PENDING_THREAD_ID: 42, RECOVERY_WINDOW_ID: "@1"})

        with (
            patch("ccgram.handlers.recovery_callbacks.tmux_manager") as mock_tm,
            patch("ccgram.handlers.recovery_callbacks.session_manager") as mock_sm,
            patch("ccgram.handlers.recovery_callbacks.get_provider_for_window") as mock_gpw,
            patch("ccgram.handlers.recovery_callbacks.safe_edit"),
            patch("ccgram.handlers.recovery_callbacks.format_topic_name_for_mode"),
            patch("ccgram.handlers.status_polling.clear_dead_notification"),
        ):
            mock_tm.create_window = AsyncMock(
                return_value=(True, "Created", "topic", "@11")
            )
            caps = mock_gpw.return_value.capabilities
            caps.name = "claude"
            caps.supports_hook = False
            mock_sm.get_approval_mode.return_value = "normal"

            await _create_and_bind_window(
                query,
                100,
                42,
                "/tmp/project",
                context,
                old_window_id="@1",
            )

        mock_sm.set_notification_mode.assert_called_once_with("@11", "interactive")

    async def test_resume_flow_sets_interactive_mode(self) -> None:
        with (
            patch("ccgram.handlers.resume_command.tmux_manager") as mock_tm,
            patch("ccgram.handlers.resume_command.session_manager") as mock_sm,
            patch("ccgram.handlers.resume_command.get_provider_for_window") as mock_gpw,
            patch("ccgram.handlers.resume_command.resolve_launch_command"),
        ):
            mock_tm.create_window = AsyncMock(
                return_value=(True, "Created", "topic", "@12")
            )
            caps = mock_gpw.return_value.capabilities
            caps.name = "claude"
            caps.supports_hook = False
            mock_sm.get_window_for_thread.return_value = "@1"
            mock_sm.get_approval_mode.return_value = "normal"

            await _create_resume_window(100, 42, "sess-1", "/tmp/project")

        mock_sm.set_notification_mode.assert_called_once_with("@12", "interactive")

    async def test_restore_command_sets_interactive_mode(self, tmp_path) -> None:
        update = MagicMock()
        update.effective_user = MagicMock(id=100)
        update.message = AsyncMock()
        update.message.message_thread_id = 42
        update.message.chat.type = "supergroup"
        update.message.chat.id = -100999
        context = _make_context()

        with (
            patch("ccgram.handlers.restore_command.config") as mock_cfg,
            patch("ccgram.handlers.restore_command.session_manager") as mock_sm,
            patch("ccgram.handlers.restore_command.tmux_manager") as mock_tm,
            patch("ccgram.handlers.restore_command.get_provider_for_window") as mock_gpw,
            patch("ccgram.handlers.restore_command.resolve_launch_command"),
            patch("ccgram.handlers.restore_command.safe_reply"),
            patch("ccgram.handlers.restore_command.clear_dead_notification"),
            patch("ccgram.handlers.restore_command.format_topic_name_for_mode"),
        ):
            mock_cfg.is_user_allowed.return_value = True
            mock_sm.resolve_window_for_thread.return_value = "@1"
            mock_sm.get_window_state.return_value = WindowState(cwd=str(tmp_path))
            mock_sm.get_approval_mode.return_value = "normal"
            mock_tm.find_window_by_id = AsyncMock(return_value=None)
            mock_tm.create_window = AsyncMock(
                return_value=(True, "Created", "topic", "@13")
            )
            caps = mock_gpw.return_value.capabilities
            caps.name = "claude"
            caps.supports_hook = False

            await restore_command(update, context)

        mock_sm.set_notification_mode.assert_called_once_with("@13", "interactive")


class TestNotifyModeMessageRouting:
    def test_extract_notify_summary_strips_milestone_marker(self) -> None:
        is_summary, filtered = _extract_notify_summary(
            "[CCGRAM_MILESTONE] shell integration installed"
        )

        assert is_summary is True
        assert filtered == "shell integration installed"

    def test_extract_notify_summary_strips_final_marker(self) -> None:
        is_summary, filtered = _extract_notify_summary(
            "[CCGRAM_FINAL] finished sync and tests passed"
        )

        assert is_summary is True
        assert filtered == "finished sync and tests passed"

    def test_extract_notify_summary_leaves_plain_text_unchanged(self) -> None:
        is_summary, filtered = _extract_notify_summary("plain progress update")

        assert is_summary is False
        assert filtered == "plain progress update"

    async def test_notify_mode_filters_routine_assistant_output(self) -> None:
        msg = NewMessage(
            session_id="sess-1",
            text="Working through the task",
            is_complete=True,
        )
        bot = AsyncMock()

        with (
            patch("ccgram.bot.session_manager") as mock_sm,
            patch(
                "ccgram.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_enqueue,
            patch("ccgram.bot.clear_interactive_msg", new_callable=AsyncMock) as mock_clear,
            patch("ccgram.bot.get_interactive_msg_id", return_value=None),
        ):
            mock_sm.find_users_for_session.return_value = [(100, "@7", 42)]
            mock_sm.get_notification_mode.return_value = "notify"

            await handle_new_message(msg, bot)

        mock_enqueue.assert_not_called()
        mock_clear.assert_not_called()

    async def test_notify_mode_clears_stale_interactive_ui_before_suppressing(
        self,
    ) -> None:
        msg = NewMessage(session_id="sess-1", text="Done", is_complete=True)
        bot = AsyncMock()

        with (
            patch("ccgram.bot.session_manager") as mock_sm,
            patch(
                "ccgram.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_enqueue,
            patch("ccgram.bot.clear_interactive_msg", new_callable=AsyncMock) as mock_clear,
            patch("ccgram.bot.get_interactive_msg_id", return_value=99),
        ):
            mock_sm.find_users_for_session.return_value = [(100, "@7", 42)]
            mock_sm.get_notification_mode.return_value = "notify"

            await handle_new_message(msg, bot)

        mock_clear.assert_awaited_once_with(100, bot, 42)
        mock_enqueue.assert_not_called()

    async def test_notify_mode_allows_interactive_tool_prompts(self) -> None:
        msg = NewMessage(
            session_id="sess-1",
            text="Please choose an option",
            is_complete=True,
            content_type="tool_use",
            tool_name="AskUserQuestion",
        )
        bot = AsyncMock()
        queue = AsyncMock()

        with (
            patch("ccgram.bot.session_manager") as mock_sm,
            patch("ccgram.bot.get_message_queue", return_value=queue),
            patch("ccgram.bot.set_interactive_mode"),
            patch(
                "ccgram.bot.handle_interactive_ui",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_handle,
            patch("ccgram.bot.get_interactive_msg_id", return_value=None),
            patch("ccgram.bot.asyncio.sleep", new_callable=AsyncMock),
            patch(
                "ccgram.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_enqueue,
        ):
            mock_sm.find_users_for_session.return_value = [(100, "@7", 42)]
            mock_sm.get_notification_mode.return_value = "notify"
            mock_sm.resolve_session_for_window = AsyncMock(return_value=None)

            await handle_new_message(msg, bot)

        queue.join.assert_awaited_once()
        mock_handle.assert_awaited_once_with(bot, 100, "@7", 42)
        mock_enqueue.assert_not_called()

    async def test_notify_mode_allows_explicit_milestone_summary(self) -> None:
        msg = NewMessage(
            session_id="sess-1",
            text="[CCGRAM_MILESTONE] shell integration installed",
            is_complete=True,
        )
        bot = AsyncMock()

        with (
            patch("ccgram.bot.session_manager") as mock_sm,
            patch(
                "ccgram.bot.build_response_parts",
                return_value=["shell integration installed"],
            ) as mock_parts,
            patch(
                "ccgram.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_enqueue,
            patch("ccgram.bot.clear_interactive_msg", new_callable=AsyncMock) as mock_clear,
            patch("ccgram.bot.get_interactive_msg_id", return_value=None),
        ):
            mock_sm.find_users_for_session.return_value = [(100, "@7", 42)]
            mock_sm.get_notification_mode.return_value = "notify"
            mock_sm.resolve_session_for_window = AsyncMock(return_value=None)

            await handle_new_message(msg, bot)

        mock_parts.assert_called_once_with(
            "shell integration installed",
            True,
            "text",
            "assistant",
        )
        mock_enqueue.assert_awaited_once()
        assert mock_enqueue.await_args.kwargs["text"] == "shell integration installed"
        mock_clear.assert_not_called()

    async def test_notify_mode_allows_explicit_final_summary_and_strips_marker(
        self,
    ) -> None:
        msg = NewMessage(
            session_id="sess-1",
            text="[CCGRAM_FINAL] finished sync and tests passed",
            is_complete=True,
        )
        bot = AsyncMock()

        with (
            patch("ccgram.bot.session_manager") as mock_sm,
            patch(
                "ccgram.bot.build_response_parts",
                return_value=["finished sync and tests passed"],
            ) as mock_parts,
            patch(
                "ccgram.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_enqueue,
            patch("ccgram.bot.clear_interactive_msg", new_callable=AsyncMock) as mock_clear,
            patch("ccgram.bot.get_interactive_msg_id", return_value=None),
        ):
            mock_sm.find_users_for_session.return_value = [(100, "@7", 42)]
            mock_sm.get_notification_mode.return_value = "notify"
            mock_sm.resolve_session_for_window = AsyncMock(return_value=None)

            await handle_new_message(msg, bot)

        mock_parts.assert_called_once_with(
            "finished sync and tests passed",
            True,
            "text",
            "assistant",
        )
        mock_enqueue.assert_awaited_once()
        assert mock_enqueue.await_args.kwargs["text"] == "finished sync and tests passed"
        mock_clear.assert_not_called()
