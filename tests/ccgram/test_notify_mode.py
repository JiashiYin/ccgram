"""Focused tests for notify-mode routing and explicit interactive restores."""

from unittest.mock import AsyncMock, MagicMock, patch

from ccgram.bot import handle_new_message
from ccgram.handlers.directory_callbacks import _create_window_and_bind
from ccgram.handlers.directory_browser import UNBOUND_WINDOWS_KEY
from ccgram.handlers.recovery_callbacks import _create_and_bind_window
from ccgram.handlers.resume_command import _create_resume_window
from ccgram.handlers.restore_command import restore_command
from ccgram.handlers.window_callbacks import _handle_bind
from ccgram.handlers.user_state import (
    PENDING_THREAD_ID,
    PENDING_THREAD_TEXT,
    PENDING_TOPIC_DEFAULT_BOT,
    RECOVERY_WINDOW_ID,
)
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
            patch(
                "ccgram.handlers.directory_callbacks._wait_for_hookless_session_ready",
                new_callable=AsyncMock,
                return_value=True,
            ),
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

    async def test_directory_create_window_skips_pending_text_until_hookless_ready(
        self,
    ) -> None:
        query = _make_query()
        context = _make_context({PENDING_THREAD_ID: 42, PENDING_THREAD_TEXT: "hello"})

        with (
            patch("ccgram.handlers.directory_callbacks.tmux_manager") as mock_tm,
            patch("ccgram.handlers.directory_callbacks.session_manager") as mock_sm,
            patch("ccgram.handlers.directory_callbacks.provider_registry") as mock_pr,
            patch("ccgram.handlers.directory_callbacks.safe_edit"),
            patch("ccgram.handlers.directory_callbacks.safe_send") as mock_send,
            patch("ccgram.handlers.directory_callbacks._wait_for_shell_ready"),
            patch(
                "ccgram.handlers.directory_callbacks._wait_for_hookless_session_ready",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            mock_tm.create_window = AsyncMock(
                return_value=(True, "Created", "topic", "@10")
            )
            mock_tm.stamp_pane_title = AsyncMock()
            mock_pr.get.return_value.capabilities.supports_hook = False
            mock_sm.get_window_state.return_value = MagicMock(cwd="")
            mock_sm.get_approval_mode.return_value = "normal"
            mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

            await _create_window_and_bind(
                query,
                100,
                "/tmp/project",
                "codex",
                "normal",
                context,
            )

        mock_sm.send_to_window.assert_not_called()
        assert "still starting" in mock_send.call_args.args[2].lower()

    async def test_directory_create_window_claims_default_responder_marker(self) -> None:
        query = _make_query()
        context = _make_context(
            {PENDING_THREAD_ID: 42, PENDING_TOPIC_DEFAULT_BOT: "Jacob_localCodexBot"}
        )

        with (
            patch("ccgram.handlers.directory_callbacks.tmux_manager") as mock_tm,
            patch("ccgram.handlers.directory_callbacks.session_manager") as mock_sm,
            patch("ccgram.handlers.directory_callbacks.provider_registry") as mock_pr,
            patch("ccgram.handlers.directory_callbacks.safe_edit"),
            patch("ccgram.handlers.directory_callbacks._wait_for_shell_ready"),
            patch(
                "ccgram.handlers.directory_callbacks._wait_for_hookless_session_ready",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            mock_tm.create_window = AsyncMock(
                return_value=(True, "Created", "topic", "@10")
            )
            mock_tm.stamp_pane_title = AsyncMock()
            mock_pr.get.return_value.capabilities.supports_hook = False
            mock_sm.get_window_state.return_value = MagicMock(cwd="")
            mock_sm.get_approval_mode.return_value = "normal"
            mock_sm.resolve_chat_id.return_value = -100

            await _create_window_and_bind(
                query,
                100,
                "/tmp/project",
                "codex",
                "normal",
                context,
            )

        context.bot.edit_forum_topic.assert_awaited_once()
        assert "[@Jacob_localCodexBot]" in context.bot.edit_forum_topic.await_args.kwargs["name"]

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

    async def test_window_rebind_claims_default_responder_marker(self) -> None:
        query = _make_query()
        context = _make_context(
            {
                PENDING_THREAD_ID: 42,
                UNBOUND_WINDOWS_KEY: ["@5"],
                PENDING_TOPIC_DEFAULT_BOT: "Jacob_localCodexBot",
            }
        )
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

        context.bot.edit_forum_topic.assert_awaited_once()
        assert "[@Jacob_localCodexBot]" in context.bot.edit_forum_topic.await_args.kwargs["name"]

    async def test_recovery_flow_sets_interactive_mode(self) -> None:
        query = _make_query()
        context = _make_context({PENDING_THREAD_ID: 42, RECOVERY_WINDOW_ID: "@1"})

        with (
            patch("ccgram.handlers.recovery_callbacks.tmux_manager") as mock_tm,
            patch("ccgram.handlers.recovery_callbacks.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.recovery_callbacks.get_provider_for_window"
            ) as mock_gpw,
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
            patch(
                "ccgram.handlers.restore_command.get_provider_for_window"
            ) as mock_gpw,
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
    async def test_codex_notify_mode_stages_final_answer_until_halt(self) -> None:
        msg = NewMessage(
            session_id="sess-1",
            text="I finished the implementation and need your next instruction.",
            is_complete=True,
            phase="final_answer",
            notify_kind="report_back",
        )
        bot = AsyncMock()

        with (
            patch("ccgram.bot.session_manager") as mock_sm,
            patch("ccgram.bot.get_provider_for_window") as mock_provider,
            patch("ccgram.bot.stage_pending_notify_report") as mock_stage,
            patch(
                "ccgram.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_enqueue,
            patch("ccgram.bot.build_response_parts", return_value=["done"]) as mock_parts,
        ):
            mock_sm.find_users_for_session.return_value = [(100, "@7", 42)]
            mock_sm.get_notification_mode.return_value = "notify"
            mock_provider.return_value.capabilities.supports_semantic_notify = True

            await handle_new_message(msg, bot)

        mock_stage.assert_called_once_with(
            100,
            42,
            text="I finished the implementation and need your next instruction.",
            content_type="text",
            role="assistant",
        )
        mock_parts.assert_not_called()
        mock_enqueue.assert_not_awaited()

    async def test_notify_mode_ignores_streaming_assistant_output(self) -> None:
        msg = NewMessage(
            session_id="sess-1",
            text="Still working on it",
            is_complete=False,
        )
        bot = AsyncMock()

        with (
            patch("ccgram.bot.session_manager") as mock_sm,
            patch("ccgram.bot.stage_pending_notify_report") as mock_stage,
            patch(
                "ccgram.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_enqueue,
        ):
            mock_sm.find_users_for_session.return_value = [(100, "@7", 42)]
            mock_sm.get_notification_mode.return_value = "notify"

            await handle_new_message(msg, bot)

        mock_stage.assert_not_called()
        mock_enqueue.assert_not_called()

    async def test_notify_mode_suppresses_noninteractive_tool_flow(self) -> None:
        msg = NewMessage(
            session_id="sess-1",
            text="**exec_command** `pytest -q`",
            is_complete=True,
            content_type="tool_use",
            tool_name="exec_command",
        )
        bot = AsyncMock()

        with (
            patch("ccgram.bot.session_manager") as mock_sm,
            patch("ccgram.bot.stage_pending_notify_report") as mock_stage,
            patch(
                "ccgram.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_enqueue,
        ):
            mock_sm.find_users_for_session.return_value = [(100, "@7", 42)]
            mock_sm.get_notification_mode.return_value = "notify"

            await handle_new_message(msg, bot)

        mock_stage.assert_not_called()
        mock_enqueue.assert_not_called()

    async def test_semantic_notify_provider_drops_unclassified_assistant_text(
        self,
    ) -> None:
        msg = NewMessage(
            session_id="sess-1",
            text="Internal progress note without halt semantics.",
            is_complete=True,
        )
        bot = AsyncMock()

        with (
            patch("ccgram.bot.session_manager") as mock_sm,
            patch("ccgram.bot.get_provider_for_window") as mock_provider,
            patch("ccgram.bot.stage_pending_notify_report") as mock_stage,
            patch(
                "ccgram.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_enqueue,
        ):
            mock_sm.find_users_for_session.return_value = [(100, "@7", 42)]
            mock_sm.get_notification_mode.return_value = "notify"
            mock_provider.return_value.capabilities.supports_semantic_notify = True

            await handle_new_message(msg, bot)

        mock_stage.assert_not_called()
        mock_enqueue.assert_not_called()

    async def test_codex_notify_mode_ignores_commentary(
        self,
    ) -> None:
        msg = NewMessage(
            session_id="sess-1",
            text="I'm checking the service log now.",
            is_complete=True,
            phase="commentary",
            notify_kind="commentary",
        )
        bot = AsyncMock()

        with (
            patch("ccgram.bot.session_manager") as mock_sm,
            patch("ccgram.bot.get_provider_for_window") as mock_provider,
            patch("ccgram.bot.stage_pending_notify_report") as mock_stage,
            patch(
                "ccgram.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_enqueue,
        ):
            mock_sm.find_users_for_session.return_value = [(100, "@7", 42)]
            mock_sm.get_notification_mode.return_value = "notify"
            mock_provider.return_value.capabilities.supports_semantic_notify = True

            await handle_new_message(msg, bot)

        mock_stage.assert_not_called()
        mock_enqueue.assert_not_called()

    async def test_notify_mode_clears_pending_report_when_commentary_resumes(
        self,
    ) -> None:
        msg = NewMessage(
            session_id="sess-1",
            text="I am still checking logs.",
            is_complete=True,
            phase="commentary",
            notify_kind="commentary",
        )
        bot = AsyncMock()

        with (
            patch("ccgram.bot.session_manager") as mock_sm,
            patch("ccgram.bot.get_provider_for_window") as mock_provider,
            patch("ccgram.bot.clear_pending_notify_report") as mock_clear,
            patch("ccgram.bot.stage_pending_notify_report") as mock_stage,
            patch(
                "ccgram.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_enqueue,
        ):
            mock_sm.find_users_for_session.return_value = [(100, "@7", 42)]
            mock_sm.get_notification_mode.return_value = "notify"
            mock_provider.return_value.capabilities.supports_semantic_notify = True

            await handle_new_message(msg, bot)

        mock_clear.assert_called_once_with(100, 42)
        mock_stage.assert_not_called()
        mock_enqueue.assert_not_called()

    async def test_notify_mode_clears_pending_report_when_tool_flow_resumes(
        self,
    ) -> None:
        msg = NewMessage(
            session_id="sess-1",
            text="**exec_command** `pytest -q`",
            is_complete=True,
            content_type="tool_use",
            tool_name="exec_command",
        )
        bot = AsyncMock()

        with (
            patch("ccgram.bot.session_manager") as mock_sm,
            patch("ccgram.bot.clear_pending_notify_report") as mock_clear,
            patch("ccgram.bot.stage_pending_notify_report") as mock_stage,
            patch(
                "ccgram.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_enqueue,
        ):
            mock_sm.find_users_for_session.return_value = [(100, "@7", 42)]
            mock_sm.get_notification_mode.return_value = "notify"

            await handle_new_message(msg, bot)

        mock_clear.assert_called_once_with(100, 42)
        mock_stage.assert_not_called()
        mock_enqueue.assert_not_called()

    async def test_nonsemantic_notify_provider_drops_unclassified_assistant_text(
        self,
    ) -> None:
        msg = NewMessage(
            session_id="sess-1",
            text="I need you to choose the next task.",
            is_complete=True,
        )
        bot = AsyncMock()

        with (
            patch("ccgram.bot.session_manager") as mock_sm,
            patch("ccgram.bot.get_provider_for_window") as mock_provider,
            patch("ccgram.bot.stage_pending_notify_report") as mock_stage,
            patch(
                "ccgram.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_enqueue,
        ):
            mock_sm.find_users_for_session.return_value = [(100, "@7", 42)]
            mock_sm.get_notification_mode.return_value = "notify"
            mock_provider.return_value.capabilities.supports_semantic_notify = False

            await handle_new_message(msg, bot)

        mock_stage.assert_not_called()
        mock_enqueue.assert_not_called()

    async def test_notify_mode_drops_unclassified_text_when_provider_resolution_fails(
        self,
    ) -> None:
        msg = NewMessage(
            session_id="sess-1",
            text="I need you to choose the next task.",
            is_complete=True,
        )
        bot = AsyncMock()

        with (
            patch("ccgram.bot.session_manager") as mock_sm,
            patch(
                "ccgram.bot.get_provider_for_window",
                side_effect=RuntimeError("provider lookup failed"),
            ),
            patch("ccgram.bot.stage_pending_notify_report") as mock_stage,
            patch(
                "ccgram.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_enqueue,
        ):
            mock_sm.find_users_for_session.return_value = [(100, "@7", 42)]
            mock_sm.get_notification_mode.return_value = "notify"

            await handle_new_message(msg, bot)

        mock_stage.assert_not_called()
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
