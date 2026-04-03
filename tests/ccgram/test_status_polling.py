"""Tests for status polling: shell detection, autoclose timers, rename sync,
activity heuristic, and startup timeout."""

import time

from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from telegram import Bot
from telegram.error import BadRequest, TelegramError

from tests.ccgram.conftest import make_mock_provider

from ccgram.handlers.status_polling import (
    PendingNotifyReport,
    _check_autoclose_timers,
    _cleanup_ghost_bindings,
    _cleanup_stale_bound_shell_windows,
    _check_transcript_activity,
    _clear_autoclose_if_active,
    _dead_notified,
    _get_window_state,
    _handle_dead_window_notification,
    _handle_missing_window,
    _MAX_PROBE_FAILURES,
    _pane_alert_hashes,
    _parse_with_pyte,
    _probe_topic_existence,
    _probe_unbound_cached_topics,
    _prune_stale_state,
    _scan_window_panes,
    _start_autoclose_timer,
    _topic_poll_state,
    _window_poll_state,
    clear_autoclose_timer,
    clear_pane_alerts,
    clear_screen_buffer,
    has_pane_alert,
    is_shell_prompt,
    reset_screen_buffer_state,
)
from ccgram.session import AuditIssue, AuditResult
from ccgram.tmux_manager import PaneInfo


# Helpers for readable assertions on dataclass-based state
def _has_autoclose(user_id: int, thread_id: int) -> bool:
    ts = _topic_poll_state.get((user_id, thread_id))
    return ts is not None and ts.autoclose is not None


def _get_autoclose(user_id: int, thread_id: int) -> tuple[str, float] | None:
    ts = _topic_poll_state.get((user_id, thread_id))
    return ts.autoclose if ts else None


@pytest.fixture(autouse=True)
def _reset():
    _window_poll_state.clear()
    _topic_poll_state.clear()
    _dead_notified.clear()
    yield
    _window_poll_state.clear()
    _topic_poll_state.clear()
    _dead_notified.clear()


class TestIsShellPrompt:
    @pytest.mark.parametrize(
        "cmd",
        ["bash", "zsh", "fish", "sh", "/usr/bin/zsh", "  bash  ", "dash", "ksh"],
    )
    def test_shell_detected(self, cmd: str) -> None:
        assert is_shell_prompt(cmd) is True

    @pytest.mark.parametrize("cmd", ["node", "claude", "npx", ""])
    def test_non_shell_rejected(self, cmd: str) -> None:
        assert is_shell_prompt(cmd) is False


class TestAutocloseTimers:
    def test_start_timer(self) -> None:
        _start_autoclose_timer(1, 42, "done", 100.0)
        assert _get_autoclose(1, 42) == ("done", 100.0)

    def test_start_timer_preserves_existing_same_state(self) -> None:
        _start_autoclose_timer(1, 42, "done", 100.0)
        _start_autoclose_timer(1, 42, "done", 200.0)
        assert _get_autoclose(1, 42) == ("done", 100.0)

    def test_start_timer_resets_on_state_change(self) -> None:
        _start_autoclose_timer(1, 42, "done", 100.0)
        _start_autoclose_timer(1, 42, "dead", 200.0)
        assert _get_autoclose(1, 42) == ("dead", 200.0)

    def test_clear_on_active(self) -> None:
        _start_autoclose_timer(1, 42, "done", 100.0)
        _clear_autoclose_if_active(1, 42)
        assert not _has_autoclose(1, 42)

    def test_clear_timer(self) -> None:
        _start_autoclose_timer(1, 42, "done", 100.0)
        clear_autoclose_timer(1, 42)
        assert not _has_autoclose(1, 42)

    def test_clear_nonexistent_is_noop(self) -> None:
        clear_autoclose_timer(1, 42)

    @pytest.mark.parametrize(
        ("state", "minutes", "elapsed"),
        [("done", 30, 30 * 60 + 1), ("dead", 10, 10 * 60 + 1)],
        ids=["done", "dead"],
    )
    async def test_check_expired(
        self, state: str, minutes: int, elapsed: float
    ) -> None:
        _start_autoclose_timer(1, 42, state, 0.0)
        bot = AsyncMock(spec=Bot)
        with (
            patch("ccgram.handlers.status_polling.config") as mock_config,
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch("ccgram.handlers.status_polling.time") as mock_time,
            patch("ccgram.handlers.status_polling.clear_topic_state"),
        ):
            mock_config.autoclose_done_minutes = 30
            mock_config.autoclose_dead_minutes = minutes
            mock_time.monotonic.return_value = elapsed
            mock_sm.resolve_chat_id.return_value = -100
            await _check_autoclose_timers(bot)
        bot.delete_forum_topic.assert_called_once_with(
            chat_id=-100, message_thread_id=42
        )
        mock_sm.unbind_thread.assert_called_once_with(1, 42)
        assert not _has_autoclose(1, 42)

    async def test_check_not_expired_yet(self) -> None:
        _start_autoclose_timer(1, 42, "done", 0.0)
        bot = AsyncMock(spec=Bot)
        with (
            patch("ccgram.handlers.status_polling.config") as mock_config,
            patch("ccgram.handlers.status_polling.time") as mock_time,
        ):
            mock_config.autoclose_done_minutes = 30
            mock_config.autoclose_dead_minutes = 10
            mock_time.monotonic.return_value = 29 * 60
            await _check_autoclose_timers(bot)
        bot.close_forum_topic.assert_not_called()
        assert _has_autoclose(1, 42)

    async def test_check_disabled_when_zero(self) -> None:
        _start_autoclose_timer(1, 42, "done", 0.0)
        bot = AsyncMock(spec=Bot)
        with (
            patch("ccgram.handlers.status_polling.config") as mock_config,
            patch("ccgram.handlers.status_polling.time") as mock_time,
        ):
            mock_config.autoclose_done_minutes = 0
            mock_config.autoclose_dead_minutes = 0
            mock_time.monotonic.return_value = 999999
            await _check_autoclose_timers(bot)
        bot.close_forum_topic.assert_not_called()

    async def test_check_telegram_error_handled(self) -> None:
        _start_autoclose_timer(1, 42, "done", 0.0)
        bot = AsyncMock(spec=Bot)
        bot.close_forum_topic.side_effect = TelegramError("fail")
        with (
            patch("ccgram.handlers.status_polling.config") as mock_config,
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch("ccgram.handlers.status_polling.time") as mock_time,
        ):
            mock_config.autoclose_done_minutes = 30
            mock_config.autoclose_dead_minutes = 10
            mock_time.monotonic.return_value = 30 * 60 + 1
            mock_sm.resolve_chat_id.return_value = -100
            await _check_autoclose_timers(bot)
        assert not _has_autoclose(1, 42)


class TestTranscriptActivityHeuristic:
    def test_active_when_recent_transcript(self) -> None:
        now = time.monotonic()
        mock_monitor = MagicMock()
        mock_monitor.get_last_activity.return_value = now - 5.0
        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.status_polling.get_active_monitor",
                return_value=mock_monitor,
            ),
        ):
            mock_sm.get_session_id_for_window.return_value = "sess-123"
            result = _check_transcript_activity("@0", now)
        assert result is True
        assert _window_poll_state.get("@0") and _window_poll_state["@0"].has_seen_status

    def test_inactive_when_stale_transcript(self) -> None:
        now = time.monotonic()
        mock_monitor = MagicMock()
        mock_monitor.get_last_activity.return_value = now - 20.0
        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.status_polling.get_active_monitor",
                return_value=mock_monitor,
            ),
        ):
            mock_sm.get_session_id_for_window.return_value = "sess-123"
            result = _check_transcript_activity("@0", now)
        assert result is False
        assert not (
            _window_poll_state.get("@0") and _window_poll_state["@0"].has_seen_status
        )

    def test_inactive_when_no_session(self) -> None:
        now = time.monotonic()
        with patch("ccgram.handlers.status_polling.session_manager") as mock_sm:
            mock_sm.get_session_id_for_window.return_value = None
            result = _check_transcript_activity("@0", now)
        assert result is False

    def test_inactive_when_no_monitor(self) -> None:
        now = time.monotonic()
        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.status_polling.get_active_monitor",
                return_value=None,
            ),
        ):
            mock_sm.get_session_id_for_window.return_value = "sess-123"
            result = _check_transcript_activity("@0", now)
        assert result is False

    def test_clears_startup_timer_on_activity(self) -> None:
        now = time.monotonic()

        _get_window_state("@0").startup_time = now - 15.0
        mock_monitor = MagicMock()
        mock_monitor.get_last_activity.return_value = now - 3.0
        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.status_polling.get_active_monitor",
                return_value=mock_monitor,
            ),
        ):
            mock_sm.get_session_id_for_window.return_value = "sess-123"
            result = _check_transcript_activity("@0", now)
        assert result is True
        assert (
            _window_poll_state.get("@0") is None
            or _window_poll_state["@0"].startup_time is None
        )


class TestStartupTimeout:
    async def test_first_poll_records_startup_time(self) -> None:
        from ccgram.handlers.status_polling import _handle_no_status

        bot = AsyncMock(spec=Bot)
        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch("ccgram.handlers.status_polling.update_topic_emoji"),
            patch("ccgram.handlers.status_polling._send_typing_throttled"),
            patch(
                "ccgram.handlers.status_polling._check_transcript_activity",
                return_value=False,
            ),
            patch("ccgram.handlers.status_polling.time") as mock_time,
        ):
            mock_time.monotonic.return_value = 1000.0
            mock_sm.resolve_chat_id.return_value = -100
            mock_sm.get_display_name.return_value = "project"
            await _handle_no_status(bot, 1, "@0", 42, "node", "normal")
        assert (
            _window_poll_state.get("@0") is not None
            and _window_poll_state["@0"].startup_time is not None
        )

    async def test_startup_timeout_transitions_to_idle(self) -> None:
        from ccgram.handlers.status_polling import _handle_no_status

        bot = AsyncMock(spec=Bot)
        _get_window_state("@0").startup_time = 1000.0
        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch("ccgram.handlers.status_polling.update_topic_emoji") as mock_emoji,
            patch("ccgram.handlers.status_polling.enqueue_status_update"),
            patch(
                "ccgram.handlers.status_polling._check_transcript_activity",
                return_value=False,
            ),
            patch("ccgram.handlers.status_polling.time") as mock_time,
        ):
            mock_time.monotonic.return_value = 1000.0 + 31.0
            mock_sm.resolve_chat_id.return_value = -100
            mock_sm.get_display_name.return_value = "project"
            await _handle_no_status(bot, 1, "@0", 42, "node", "normal")
        assert _window_poll_state.get("@0") and _window_poll_state["@0"].has_seen_status
        assert (
            _window_poll_state.get("@0") is None
            or _window_poll_state["@0"].startup_time is None
        )
        mock_emoji.assert_called_once_with(bot, -100, 42, "active", "project")

    async def test_startup_grace_period_sends_typing(self) -> None:
        from ccgram.handlers.status_polling import _handle_no_status

        bot = AsyncMock(spec=Bot)
        _get_window_state("@0").startup_time = 1000.0
        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch("ccgram.handlers.status_polling.update_topic_emoji") as mock_emoji,
            patch(
                "ccgram.handlers.status_polling._send_typing_throttled"
            ) as mock_typing,
            patch(
                "ccgram.handlers.status_polling._check_transcript_activity",
                return_value=False,
            ),
            patch("ccgram.handlers.status_polling.time") as mock_time,
        ):
            mock_time.monotonic.return_value = 1010.0
            mock_sm.resolve_chat_id.return_value = -100
            mock_sm.get_display_name.return_value = "project"
            await _handle_no_status(bot, 1, "@0", 42, "node", "normal")
        mock_typing.assert_called_once_with(bot, 1, 42)
        mock_emoji.assert_called_once_with(bot, -100, 42, "active", "project")
        assert not (
            _window_poll_state.get("@0") and _window_poll_state["@0"].has_seen_status
        )


@pytest.fixture()
def _reset_pyte():
    reset_screen_buffer_state()
    yield
    reset_screen_buffer_state()


_SEP = "─" * 30


@pytest.mark.usefixtures("_reset_pyte")
class TestParseWithPyte:
    """Tests for pyte-based screen parsing integration."""

    @pytest.mark.parametrize(
        ("spinner", "text", "expected_raw"),
        [
            ("✻", "Reading file src/main.py", "Reading file src/main.py"),
            ("⠋", "Thinking about things", "Thinking about things"),
        ],
        ids=["unicode-spinner", "braille-spinner"],
    )
    def test_detects_spinner(self, spinner: str, text: str, expected_raw: str) -> None:
        pane_text = f"Output\n{spinner} {text}\n{_SEP}\n"
        result = _parse_with_pyte("@0", pane_text)
        assert result is not None
        assert result.raw_text == expected_raw
        assert result.is_interactive is False

    def test_detects_interactive_ui(self) -> None:
        pane_text = (
            "  Would you like to proceed?\n"
            f"  {_SEP}\n"
            "  Yes     No\n"
            f"  {_SEP}\n"
            "  ctrl-g to edit in vim\n"
        )
        result = _parse_with_pyte("@0", pane_text)
        assert result is not None
        assert result.is_interactive is True
        assert result.ui_type == "ExitPlanMode"

    def test_returns_none_for_plain_text(self) -> None:
        result = _parse_with_pyte("@0", "$ echo hello\nhello\n$\n")
        assert result is None

    def test_screen_buffer_cached_per_window(self) -> None:
        pane_text = f"Output\n✻ Working\n{_SEP}\n"
        _parse_with_pyte("@0", pane_text)
        _parse_with_pyte("@1", pane_text)
        assert _window_poll_state["@0"].screen_buffer is not None
        assert _window_poll_state["@1"].screen_buffer is not None

    def test_interactive_takes_precedence_over_status(self) -> None:
        pane_text = (
            f"✻ Working on task\n{_SEP}\n"
            "  Do you want to proceed?\n"
            "  Allow write to /tmp/foo\n"
            "  Esc to cancel\n"
        )
        result = _parse_with_pyte("@0", pane_text)
        assert result is not None
        assert result.is_interactive is True
        assert result.ui_type == "PermissionPrompt"


@pytest.mark.usefixtures("_reset_pyte")
class TestPyteContentHashCaching:
    """Tests for content-hash optimization in _parse_with_pyte."""

    def test_cache_hit_returns_same_result(self) -> None:
        pane_text = f"Output\n✻ Working on task\n{_SEP}\n"
        result1 = _parse_with_pyte("@0", pane_text)
        result2 = _parse_with_pyte("@0", pane_text)
        assert result1 is not None
        assert result2 is result1

    def test_cache_miss_on_changed_content(self) -> None:
        result1 = _parse_with_pyte("@0", f"Output\n✻ Reading file\n{_SEP}\n")
        result2 = _parse_with_pyte("@0", f"Output\n✻ Writing file\n{_SEP}\n")
        assert result1 is not None
        assert result2 is not None
        assert result1 is not result2
        assert result1.raw_text != result2.raw_text

    def test_cache_miss_on_dimension_change(self) -> None:
        pane_text = f"Output\n✻ Working\n{_SEP}\n"
        result1 = _parse_with_pyte("@0", pane_text, columns=80, rows=24)
        result2 = _parse_with_pyte("@0", pane_text, columns=120, rows=40)
        assert result1 is not None
        assert result2 is not None
        # Same text, different dimensions — must re-parse (not cache hit)
        assert result2 is not result1

    def test_cache_none_result(self) -> None:
        pane_text = "$ echo hello\nhello\n$\n"
        result1 = _parse_with_pyte("@0", pane_text)
        result2 = _parse_with_pyte("@0", pane_text)
        assert result1 is None
        assert result2 is None
        assert _get_window_state("@0").last_pane_hash != 0

    def test_interactive_ui_not_cached(self) -> None:
        pane_text = (
            "  Would you like to proceed?\n"
            "  Yes / No\n"
            f"  {_SEP}\n"
            "  ctrl-g to edit in vim\n"
        )
        result1 = _parse_with_pyte("@0", pane_text)
        result2 = _parse_with_pyte("@0", pane_text)
        assert result1 is not None
        assert result1.is_interactive is True
        assert result2 is not result1

    def test_clear_screen_buffer_resets_cache(self) -> None:
        _parse_with_pyte("@0", f"Output\n✻ Working\n{_SEP}\n")
        ws = _get_window_state("@0")
        assert ws.last_pane_hash != 0

        clear_screen_buffer("@0")
        assert ws.last_pane_hash == 0
        assert ws.last_pyte_result is None


@pytest.mark.usefixtures("_reset_pyte")
class TestPyteDimensionPassthrough:
    """Tests that _parse_with_pyte uses actual pane dimensions."""

    def test_custom_dimensions_used(self) -> None:
        _parse_with_pyte("@0", f"Output\n✻ Working\n{_SEP}\n", columns=80, rows=24)
        buf = _get_window_state("@0").screen_buffer
        assert buf is not None
        assert buf.columns == 80
        assert buf.rows == 24

    def test_zero_dimensions_fall_back_to_default(self) -> None:
        _parse_with_pyte("@0", f"Output\n✻ Working\n{_SEP}\n", columns=0, rows=0)
        buf = _get_window_state("@0").screen_buffer
        assert buf is not None
        assert buf.columns == 200
        assert buf.rows == 50

    def test_resize_reuses_buffer(self) -> None:
        pane_text = f"Output\n✻ Working\n{_SEP}\n"
        _parse_with_pyte("@0", pane_text, columns=80, rows=24)
        buf1 = _get_window_state("@0").screen_buffer
        assert buf1 is not None

        _parse_with_pyte("@0", pane_text + " changed", columns=120, rows=40)
        buf2 = _get_window_state("@0").screen_buffer
        assert buf2 is buf1
        assert buf2 is not None
        assert buf2.columns == 120
        assert buf2.rows == 40


@pytest.mark.usefixtures("_reset_pyte")
class TestAnsiCapturePyteParsing:
    """Tests for ANSI capture -> pyte rendering -> clean fallback text."""

    def test_ansi_spinner_detected(self) -> None:
        pane_text = f"Some output\n\x1b[36m✻ Reading file src/main.py\x1b[0m\n{_SEP}\n"
        result = _parse_with_pyte("@0", pane_text)
        assert result is not None
        assert result.raw_text == "Reading file src/main.py"
        assert result.is_interactive is False

    def test_ansi_interactive_ui_detected(self) -> None:
        pane_text = (
            "  \x1b[1mWould you like to proceed?\x1b[0m\n"
            f"  {_SEP}\n"
            "  Yes     No\n"
            f"  {_SEP}\n"
            "  ctrl-g to edit in vim\n"
        )
        result = _parse_with_pyte("@0", pane_text)
        assert result is not None
        assert result.is_interactive is True

    def test_last_rendered_text_populated(self) -> None:
        _parse_with_pyte("@0", "\x1b[32mHello\x1b[0m\nWorld\n")
        ws = _get_window_state("@0")
        assert ws.last_rendered_text is not None
        assert "\x1b" not in ws.last_rendered_text
        assert "Hello" in ws.last_rendered_text
        assert "World" in ws.last_rendered_text

    def test_last_rendered_text_cached_on_hash_hit(self) -> None:
        pane_text = "$ echo hello\nhello\n"
        _parse_with_pyte("@0", pane_text)
        rendered_first = _get_window_state("@0").last_rendered_text
        _parse_with_pyte("@0", pane_text)
        assert _get_window_state("@0").last_rendered_text is rendered_first

    def test_last_rendered_text_cleared_by_clear_screen_buffer(self) -> None:
        _parse_with_pyte("@0", "Hello\nWorld\n")
        ws = _get_window_state("@0")
        assert ws.last_rendered_text is not None
        clear_screen_buffer("@0")
        assert ws.last_rendered_text is None

    def test_empty_screen_renders_as_empty_string(self) -> None:
        _parse_with_pyte("@0", "\n\n\n")
        assert _get_window_state("@0").last_rendered_text == ""


def _mock_update_status_patches(*, pyte_result, provider):
    """Context manager stack for update_status_message tests."""
    from contextlib import ExitStack

    stack = ExitStack()
    mocks: dict[str, MagicMock] = {}
    mocks["tm"] = stack.enter_context(
        patch("ccgram.handlers.status_polling.tmux_manager")
    )
    mocks["sm"] = stack.enter_context(
        patch("ccgram.handlers.status_polling.session_manager")
    )
    stack.enter_context(patch("ccgram.handlers.status_polling.update_topic_emoji"))
    mocks["enqueue"] = stack.enter_context(
        patch("ccgram.handlers.status_polling.enqueue_status_update")
    )
    stack.enter_context(
        patch(
            "ccgram.handlers.status_polling.get_interactive_window",
            return_value=None,
        )
    )
    mocks["provider"] = stack.enter_context(
        patch(
            "ccgram.handlers.status_polling.get_provider_for_window",
            return_value=provider,
        )
    )
    stack.enter_context(
        patch(
            "ccgram.handlers.status_polling._parse_with_pyte",
            return_value=pyte_result,
        )
    )
    mocks["emoji"] = stack.enter_context(
        patch("ccgram.handlers.status_polling.update_topic_emoji")
    )
    mocks["typing"] = stack.enter_context(
        patch("ccgram.handlers.status_polling._send_typing_throttled")
    )
    mocks["interactive"] = stack.enter_context(
        patch("ccgram.handlers.status_polling.handle_interactive_ui")
    )

    mock_window = MagicMock()
    mock_window.window_id = "@0"
    mock_window.window_name = "project"
    mock_window.pane_current_command = "node"
    mock_window.pane_width = 80
    mock_window.pane_height = 24
    mocks["tm"].find_window_by_id = AsyncMock(return_value=mock_window)
    mocks["tm"].capture_pane = AsyncMock(return_value="\x1b[1msome ansi output\x1b[0m")
    mocks["tm"].get_pane_title = AsyncMock(return_value="")
    mocks["sm"].resolve_chat_id.return_value = -100
    mocks["sm"].get_display_name.return_value = "project"
    mocks["sm"].get_notification_mode.return_value = "normal"

    return stack, mocks


class TestPyteFallbackInUpdateStatus:
    """Tests that update_status_message falls back to regex when pyte returns None."""

    async def test_empty_rendered_text_does_not_fall_back_to_raw_ansi(self) -> None:
        stack, mocks = _mock_update_status_patches(
            pyte_result=None, provider=make_mock_provider(has_status=False)
        )
        with stack:
            from ccgram.handlers.status_polling import update_status_message

            _get_window_state("@0").last_rendered_text = ""
            await update_status_message(AsyncMock(spec=Bot), 1, "@0", thread_id=42)

            call_args = mocks["provider"].return_value.parse_terminal_status.call_args
            assert call_args[0][0] == ""

    async def test_falls_back_to_provider_with_rendered_text(self) -> None:
        stack, mocks = _mock_update_status_patches(
            pyte_result=None, provider=make_mock_provider(has_status=True)
        )
        with stack:
            from ccgram.handlers.status_polling import update_status_message

            _get_window_state("@0").last_rendered_text = "clean rendered text"
            await update_status_message(AsyncMock(spec=Bot), 1, "@0", thread_id=42)

            provider_mock = mocks["provider"].return_value
            provider_mock.parse_terminal_status.assert_called_once()
            assert (
                provider_mock.parse_terminal_status.call_args[0][0]
                == "clean rendered text"
            )

    async def test_uses_pyte_result_when_available(self) -> None:
        from ccgram.providers.base import StatusUpdate

        pyte_status = StatusUpdate(
            raw_text="Reading file",
            display_label="\U0001f4d6 reading\u2026",
        )
        stack, mocks = _mock_update_status_patches(
            pyte_result=pyte_status, provider=make_mock_provider(has_status=True)
        )
        with stack:
            from ccgram.handlers.status_polling import update_status_message

            await update_status_message(AsyncMock(spec=Bot), 1, "@0", thread_id=42)

            provider_mock = mocks["provider"].return_value
            provider_mock.parse_terminal_status.assert_not_called()
            mocks["enqueue"].assert_called_once()
            assert mocks["enqueue"].call_args[0][3] == "\U0001f4d6 reading\u2026"

    async def test_notify_mode_suppresses_regular_status_updates(self) -> None:
        from ccgram.providers.base import StatusUpdate

        pyte_status = StatusUpdate(
            raw_text="Working",
            display_label="\u272b Working",
        )
        stack, mocks = _mock_update_status_patches(
            pyte_result=pyte_status, provider=make_mock_provider(has_status=True)
        )
        with stack:
            from ccgram.handlers.status_polling import update_status_message

            mocks["sm"].get_notification_mode.return_value = "notify"
            await update_status_message(AsyncMock(spec=Bot), 1, "@0", thread_id=42)

        mocks["enqueue"].assert_not_called()
        mocks["emoji"].assert_called_once_with(ANY, -100, 42, "active", "project")
        mocks["typing"].assert_called_once_with(ANY, 1, 42)

    async def test_notify_mode_still_handles_interactive_ui(self) -> None:
        from ccgram.providers.base import StatusUpdate

        interactive_status = StatusUpdate(
            raw_text="Would you like to proceed?",
            display_label="Permission prompt",
            is_interactive=True,
            ui_type="PermissionPrompt",
        )
        stack, mocks = _mock_update_status_patches(
            pyte_result=interactive_status, provider=make_mock_provider(has_status=True)
        )
        with stack:
            from ccgram.handlers.status_polling import update_status_message

            mocks["sm"].get_notification_mode.return_value = "notify"
            await update_status_message(AsyncMock(spec=Bot), 1, "@0", thread_id=42)

        mocks["interactive"].assert_called_once_with(ANY, 1, "@0", 42)
        mocks["enqueue"].assert_not_called()

    async def test_interactive_ui_falls_back_to_plain_capture(self) -> None:
        from ccgram.providers.base import StatusUpdate

        interactive_status = StatusUpdate(
            raw_text="Would you like to proceed?",
            display_label="Permission prompt",
            is_interactive=True,
            ui_type="PermissionPrompt",
        )
        mock_provider = MagicMock()
        mock_provider.capabilities.uses_pane_title = False
        mock_provider.parse_terminal_status.side_effect = [None, interactive_status]
        stack, mocks = _mock_update_status_patches(
            pyte_result=None, provider=mock_provider
        )
        with stack:
            from ccgram.handlers.status_polling import update_status_message

            _get_window_state("@0").last_rendered_text = ""
            mocks["tm"].capture_pane = AsyncMock(
                side_effect=[
                    "\x1b[1mWould you like to proceed?\x1b[0m",
                    "Would you like to proceed?\n1. Yes\n2. No\n",
                ]
            )
            await update_status_message(AsyncMock(spec=Bot), 1, "@0", thread_id=42)

        assert mock_provider.parse_terminal_status.call_args_list == [
            (( "",), {"pane_title": ""}),
            (("Would you like to proceed?\n1. Yes\n2. No\n",), {"pane_title": ""}),
        ]
        mocks["interactive"].assert_called_once_with(ANY, 1, "@0", 42)
        mocks["enqueue"].assert_not_called()


class TestClearSeenStatus:
    def test_clears_seen_status_and_startup(self) -> None:
        from ccgram.handlers.status_polling import clear_seen_status

        _get_window_state("@0").has_seen_status = True
        _get_window_state("@0").startup_time = 100.0
        clear_seen_status("@0")
        assert not (
            _window_poll_state.get("@0") and _window_poll_state["@0"].has_seen_status
        )
        assert (
            _window_poll_state.get("@0") is None
            or _window_poll_state["@0"].startup_time is None
        )


class TestTransitionToIdle:
    async def test_sends_idle_text(self) -> None:
        from ccgram.handlers.callback_data import IDLE_STATUS_TEXT
        from ccgram.handlers.status_polling import _transition_to_idle

        bot = AsyncMock(spec=Bot)
        with (
            patch("ccgram.handlers.status_polling.update_topic_emoji"),
            patch(
                "ccgram.handlers.status_polling.enqueue_status_update"
            ) as mock_enqueue,
            patch("ccgram.handlers.status_polling.time") as mock_time,
        ):
            mock_time.monotonic.return_value = 100.0
            await _transition_to_idle(bot, 1, "@0", 42, -100, "project", "normal")
        mock_enqueue.assert_called_once()
        assert mock_enqueue.call_args[0][3] == IDLE_STATUS_TEXT
        assert mock_enqueue.call_args[1]["thread_id"] == 42

    async def test_keeps_topic_green_for_generic_idle(self) -> None:
        from ccgram.handlers.status_polling import _transition_to_idle

        bot = AsyncMock(spec=Bot)
        with (
            patch("ccgram.handlers.status_polling.update_topic_emoji") as mock_emoji,
            patch("ccgram.handlers.status_polling.enqueue_status_update"),
        ):
            await _transition_to_idle(bot, 1, "@0", 42, -100, "project", "normal")

        mock_emoji.assert_called_once_with(bot, -100, 42, "active", "project")

    @pytest.mark.parametrize("mode", ["notify", "muted", "errors_only"])
    async def test_suppressed_mode_clears_status_no_timer(self, mode: str) -> None:
        from ccgram.handlers.status_polling import _transition_to_idle

        bot = AsyncMock(spec=Bot)
        with (
            patch("ccgram.handlers.status_polling.update_topic_emoji"),
            patch(
                "ccgram.handlers.status_polling.enqueue_status_update"
            ) as mock_enqueue,
        ):
            await _transition_to_idle(bot, 1, "@0", 42, -100, "project", mode)
        mock_enqueue.assert_called_once_with(bot, 1, "@0", None, thread_id=42)

    async def test_notify_mode_flushes_pending_report_back_on_idle(self) -> None:
        from ccgram.handlers.status_polling import _transition_to_idle

        bot = AsyncMock(spec=Bot)
        with (
            patch("ccgram.handlers.status_polling.update_topic_emoji"),
            patch(
                "ccgram.handlers.status_polling.enqueue_status_update"
            ) as mock_status,
            patch(
                "ccgram.handlers.status_polling.pop_pending_notify_report",
                return_value=PendingNotifyReport(
                    text="I finished the task and need your next step.",
                    content_type="text",
                    role="assistant",
                ),
            ),
            patch(
                "ccgram.handlers.status_polling.build_response_parts",
                return_value=["I finished the task and need your next step."],
            ) as mock_parts,
            patch(
                "ccgram.handlers.status_polling.enqueue_content_message",
                new_callable=AsyncMock,
            ) as mock_enqueue,
        ):
            await _transition_to_idle(bot, 1, "@0", 42, -100, "project", "notify")

        mock_parts.assert_called_once_with(
            "I finished the task and need your next step.",
            True,
            "text",
            "assistant",
        )
        mock_enqueue.assert_awaited_once()
        mock_status.assert_called_once_with(bot, 1, "@0", None, thread_id=42)


class TestShellPromptClearsStatus:
    async def test_shell_prompt_enqueues_status_clear(self) -> None:
        from ccgram.handlers.status_polling import _handle_no_status

        _get_window_state("@0").has_seen_status = True
        bot = AsyncMock(spec=Bot)
        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch("ccgram.handlers.status_polling.update_topic_emoji"),
            patch(
                "ccgram.handlers.status_polling.enqueue_status_update"
            ) as mock_enqueue,
            patch(
                "ccgram.handlers.status_polling._check_transcript_activity",
                return_value=False,
            ),
            patch("ccgram.handlers.status_polling.time") as mock_time,
        ):
            mock_time.monotonic.return_value = 1000.0
            mock_sm.resolve_chat_id.return_value = -100
            mock_sm.get_display_name.return_value = "project"
            await _handle_no_status(bot, 1, "@0", 42, "bash", "normal")
        mock_enqueue.assert_called_once_with(bot, 1, "@0", None, thread_id=42)

    async def test_hookless_shell_prompt_keeps_idle_status(self) -> None:
        from ccgram.handlers.callback_data import IDLE_STATUS_TEXT
        from ccgram.handlers.status_polling import _handle_no_status

        _get_window_state("@0").has_seen_status = True
        bot = AsyncMock(spec=Bot)
        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch("ccgram.handlers.status_polling.update_topic_emoji"),
            patch(
                "ccgram.handlers.status_polling.enqueue_status_update"
            ) as mock_enqueue,
            patch(
                "ccgram.handlers.status_polling._check_transcript_activity",
                return_value=False,
            ),
            patch("ccgram.handlers.status_polling.time") as mock_time,
        ):
            mock_time.monotonic.return_value = 1000.0
            mock_sm.resolve_chat_id.return_value = -100
            mock_sm.get_display_name.return_value = "project"
            mock_sm.get_window_state.return_value = MagicMock(provider_name="codex")
            await _handle_no_status(bot, 1, "@0", 42, "bash", "normal")

        mock_enqueue.assert_called_once_with(
            bot, 1, "@0", IDLE_STATUS_TEXT, thread_id=42
        )
        assert not _has_autoclose(1, 42)


class TestMissingNotifyWindowCleanup:
    async def test_notify_window_is_deleted_after_confirmation(self) -> None:
        bot = AsyncMock(spec=Bot)

        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.status_polling.remove_topic",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_remove_topic,
            patch(
                "ccgram.handlers.status_polling.clear_topic_state",
                new_callable=AsyncMock,
            ) as mock_clear_topic,
            patch(
                "ccgram.handlers.status_polling.purge_dead_window_state"
            ) as mock_purge,
        ):
            mock_sm.get_notification_mode.return_value = "notify"
            mock_sm.iter_thread_bindings.return_value = [(1, 42, "@0")]
            mock_sm.resolve_chat_id.return_value = -100

            await _handle_missing_window(bot, 1, 42, "@0")
            mock_remove_topic.assert_not_awaited()

            await _handle_missing_window(bot, 1, 42, "@0")

        mock_remove_topic.assert_awaited_once_with(bot, -100, 42)
        mock_clear_topic.assert_awaited_once_with(1, 42, bot=bot, window_id="@0")
        mock_sm.unbind_thread.assert_called_once_with(1, 42)
        mock_purge.assert_called_once_with("@0")

    async def test_interactive_window_is_purged_after_confirmation(self) -> None:
        bot = AsyncMock(spec=Bot)

        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.status_polling.remove_topic",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_remove_topic,
            patch(
                "ccgram.handlers.status_polling.clear_topic_state",
                new_callable=AsyncMock,
            ) as mock_clear_topic,
            patch(
                "ccgram.handlers.status_polling._handle_dead_window_notification",
                new_callable=AsyncMock,
            ) as mock_dead_notice,
            patch(
                "ccgram.handlers.status_polling.purge_dead_window_state"
            ) as mock_purge,
        ):
            mock_sm.get_notification_mode.return_value = "interactive"
            mock_sm.iter_thread_bindings.return_value = [(1, 42, "@0")]
            mock_sm.resolve_chat_id.return_value = -100

            await _handle_missing_window(bot, 1, 42, "@0")
            mock_remove_topic.assert_not_awaited()

            await _handle_missing_window(bot, 1, 42, "@0")

        assert mock_dead_notice.await_count == 2
        mock_remove_topic.assert_awaited_once_with(bot, -100, 42)
        mock_clear_topic.assert_awaited_once_with(1, 42, bot=bot, window_id="@0")
        mock_sm.unbind_thread.assert_called_once_with(1, 42)
        mock_purge.assert_called_once_with("@0")


class TestGhostBindingCleanup:
    async def test_cleanup_ghost_bindings_removes_dead_topics(self) -> None:
        bot = AsyncMock(spec=Bot)

        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.status_polling.remove_topic",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_remove_topic,
            patch(
                "ccgram.handlers.status_polling.clear_topic_state",
                new_callable=AsyncMock,
            ) as mock_clear_topic,
            patch(
                "ccgram.handlers.status_polling.purge_dead_window_state"
            ) as mock_purge,
        ):
            mock_sm.audit_state.return_value = AuditResult(
                issues=[
                    AuditIssue(
                        "ghost_binding",
                        "user:1 thread:42 window:@0 (dead)",
                        fixable=True,
                    )
                ],
                total_bindings=1,
                live_binding_count=0,
            )
            mock_sm.iter_thread_bindings.return_value = [(1, 42, "@0")]
            mock_sm.resolve_chat_id.return_value = -100

            await _cleanup_ghost_bindings(bot, [])

        mock_remove_topic.assert_awaited_once_with(bot, -100, 42)
        mock_clear_topic.assert_awaited_once_with(1, 42, bot=bot, window_id="@0")
        mock_sm.unbind_thread.assert_called_once_with(1, 42)
        mock_purge.assert_called_once_with("@0")


class TestDuplicateNotifyBindingCleanup:
    async def test_duplicate_notify_topic_is_removed(self) -> None:
        from ccgram.handlers.status_polling import _cleanup_duplicate_notify_bindings

        bot = AsyncMock(spec=Bot)

        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.status_polling.remove_topic",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_remove_topic,
            patch(
                "ccgram.handlers.status_polling.clear_topic_state",
                new_callable=AsyncMock,
            ) as mock_clear_topic,
        ):
            mock_sm.find_redundant_notify_session_bindings.return_value = [(1, 42, "@0")]
            mock_sm.resolve_chat_id.return_value = -100

            await _cleanup_duplicate_notify_bindings(bot)

        mock_remove_topic.assert_awaited_once_with(bot, -100, 42)
        mock_clear_topic.assert_awaited_once_with(1, 42, bot=bot, window_id="@0")
        mock_sm.unbind_thread.assert_called_once_with(1, 42)


class TestStaleShellBindingCleanup:
    async def test_cleans_notify_shell_fallback_window(self) -> None:
        bot = AsyncMock(spec=Bot)
        live_window = MagicMock()
        live_window.window_id = "@11"
        live_window.pane_current_command = "bash"

        shell_state = MagicMock()
        shell_state.provider_name = "shell"
        shell_state.session_id = ""
        shell_state.transcript_path = ""

        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.status_polling.remove_topic",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_remove_topic,
            patch(
                "ccgram.handlers.status_polling.clear_topic_state",
                new_callable=AsyncMock,
            ) as mock_clear_topic,
            patch(
                "ccgram.handlers.status_polling.tmux_manager.kill_window",
                new_callable=AsyncMock,
            ) as mock_kill_window,
            patch("ccgram.handlers.status_polling.purge_dead_window_state") as mock_purge,
        ):
            mock_sm.iter_thread_bindings.return_value = [(1, 191, "@11")]
            mock_sm.get_window_state.return_value = shell_state
            mock_sm.get_notification_mode.return_value = "notify"
            mock_sm.get_window_last_activity.return_value = None
            mock_sm.resolve_chat_id.return_value = -100

            await _cleanup_stale_bound_shell_windows(bot, [live_window])

        mock_remove_topic.assert_awaited_once_with(bot, -100, 191)
        mock_clear_topic.assert_awaited_once_with(1, 191, bot=bot, window_id="@11")
        mock_sm.unbind_thread.assert_called_once_with(1, 191)
        mock_kill_window.assert_awaited_once_with("@11")
        mock_purge.assert_called_once_with("@11")

    async def test_cleans_shell_binding_after_long_idle(self) -> None:
        bot = AsyncMock(spec=Bot)
        live_window = MagicMock()
        live_window.window_id = "@6"
        live_window.pane_current_command = "bash"

        shell_state = MagicMock()
        shell_state.provider_name = "shell"
        shell_state.session_id = ""
        shell_state.transcript_path = ""

        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.status_polling.remove_topic",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "ccgram.handlers.status_polling.clear_topic_state",
                new_callable=AsyncMock,
            ) as mock_clear_topic,
            patch(
                "ccgram.handlers.status_polling.tmux_manager.kill_window",
                new_callable=AsyncMock,
            ) as mock_kill_window,
            patch("ccgram.handlers.status_polling.purge_dead_window_state"),
            patch("ccgram.handlers.status_polling.time") as mock_time,
        ):
            mock_time.time.return_value = 200000.0
            mock_sm.iter_thread_bindings.return_value = [(1, 173, "@6")]
            mock_sm.get_window_state.return_value = shell_state
            mock_sm.get_notification_mode.return_value = "interactive"
            mock_sm.get_window_last_activity.return_value = 1000.0
            mock_sm.resolve_chat_id.return_value = -100

            await _cleanup_stale_bound_shell_windows(bot, [live_window])

        mock_clear_topic.assert_awaited_once_with(1, 173, bot=bot, window_id="@6")
        mock_sm.unbind_thread.assert_called_once_with(1, 173)
        mock_kill_window.assert_awaited_once_with("@6")


class TestProbeFailures:
    async def test_probe_skips_windows_during_backoff(self) -> None:
        _get_window_state("@5").probe_failures = _MAX_PROBE_FAILURES
        _get_window_state("@5").probe_backoff_until = time.monotonic() + 60.0
        bot = AsyncMock(spec=Bot)
        with patch("ccgram.handlers.status_polling.session_manager") as mock_sm:
            mock_sm.iter_thread_bindings.return_value = [(1, 42, "@5")]
            await _probe_topic_existence(bot)
        bot.unpin_all_forum_topic_messages.assert_not_called()

    async def test_probe_success_resets_counter(self) -> None:
        _get_window_state("@5").probe_failures = 2
        bot = AsyncMock(spec=Bot)
        with patch("ccgram.handlers.status_polling.session_manager") as mock_sm:
            mock_sm.iter_thread_bindings.return_value = [(1, 42, "@5")]
            mock_sm.resolve_chat_id.return_value = -100
            await _probe_topic_existence(bot)
        assert (
            _window_poll_state.get("@5") is None
            or _window_poll_state["@5"].probe_failures == 0
        )


class TestUnboundCachedTopicProbe:
    async def test_deleted_unbound_cached_topic_is_pruned(self) -> None:
        bot = AsyncMock(spec=Bot)
        bot.unpin_all_forum_topic_messages.side_effect = BadRequest("Topic_id_invalid")

        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.status_polling.iter_stored_topic_names",
                return_value=[(-100, 2259, "workspace [@Jacob_CodexBot]")],
            ),
            patch("ccgram.handlers.status_polling.clear_topic_emoji_state") as mock_clear,
        ):
            mock_sm.iter_thread_bindings.return_value = []
            await _probe_unbound_cached_topics(bot)

        bot.unpin_all_forum_topic_messages.assert_awaited_once_with(
            chat_id=-100,
            message_thread_id=2259,
        )
        mock_clear.assert_called_once_with(-100, 2259)

    async def test_bound_topics_are_skipped_by_unbound_probe(self) -> None:
        bot = AsyncMock(spec=Bot)

        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.status_polling.iter_stored_topic_names",
                return_value=[(-100, 42, "NPU [@Jacob_localCodexBot]")],
            ),
        ):
            mock_sm.iter_thread_bindings.return_value = [(1, 42, "@5")]
            mock_sm.resolve_chat_id.return_value = -100
            await _probe_unbound_cached_topics(bot)

        bot.unpin_all_forum_topic_messages.assert_not_called()

    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param(TelegramError("Timed out"), id="telegram-error"),
            pytest.param(BadRequest("Permission denied"), id="bad-request-other"),
        ],
    )
    async def test_probe_error_increments_counter(self, exc: TelegramError) -> None:
        bot = AsyncMock(spec=Bot)
        bot.unpin_all_forum_topic_messages.side_effect = exc
        with patch("ccgram.handlers.status_polling.session_manager") as mock_sm:
            mock_sm.iter_thread_bindings.return_value = [(1, 42, "@5")]
            mock_sm.resolve_chat_id.return_value = -100
            await _probe_topic_existence(bot)
        assert _window_poll_state["@5"].probe_failures == 1

    async def test_probe_backs_off_after_max_failures(self) -> None:
        bot = AsyncMock(spec=Bot)
        bot.unpin_all_forum_topic_messages.side_effect = TelegramError("Timed out")
        with patch("ccgram.handlers.status_polling.session_manager") as mock_sm:
            mock_sm.iter_thread_bindings.return_value = [(1, 42, "@5")]
            mock_sm.resolve_chat_id.return_value = -100
            for _ in range(_MAX_PROBE_FAILURES + 1):
                await _probe_topic_existence(bot)
        assert bot.unpin_all_forum_topic_messages.call_count == _MAX_PROBE_FAILURES
        assert _window_poll_state["@5"].probe_failures == _MAX_PROBE_FAILURES
        assert _window_poll_state["@5"].probe_backoff_until is not None

    async def test_probe_retries_after_backoff_window_expires(self) -> None:
        bot = AsyncMock(spec=Bot)
        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch("ccgram.handlers.status_polling.time") as mock_time,
        ):
            mock_sm.iter_thread_bindings.return_value = [(1, 42, "@5")]
            mock_sm.resolve_chat_id.return_value = -100
            mock_time.monotonic.return_value = 100.0
            _get_window_state("@5").probe_failures = _MAX_PROBE_FAILURES
            _get_window_state("@5").probe_backoff_until = 100.0

            await _probe_topic_existence(bot)

        bot.unpin_all_forum_topic_messages.assert_called_once_with(
            chat_id=-100, message_thread_id=42
        )

    @pytest.mark.parametrize(
        "window_alive",
        [
            pytest.param(True, id="window-alive"),
            pytest.param(False, id="window-already-gone"),
        ],
    )
    async def test_topic_deleted_cleans_up(self, window_alive: bool) -> None:
        _get_window_state("@5").probe_failures = 1
        bot = AsyncMock(spec=Bot)
        bot.unpin_all_forum_topic_messages.side_effect = BadRequest("Topic_id_invalid")
        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tm,
            patch(
                "ccgram.handlers.status_polling.clear_topic_state",
                new_callable=AsyncMock,
            ) as mock_cleanup,
        ):
            mock_sm.iter_thread_bindings.return_value = [(1, 42, "@5")]
            mock_sm.resolve_chat_id.return_value = -100
            mock_sm.get_notification_mode.return_value = "interactive"
            await _probe_topic_existence(bot)
        mock_tm.kill_window.assert_not_called()
        mock_cleanup.assert_called_once_with(1, 42, bot, window_id="@5")
        mock_sm.unbind_thread.assert_called_once_with(1, 42)
        assert (
            _window_poll_state.get("@5") is None
            or _window_poll_state["@5"].probe_failures == 0
        )


class TestPruneStaleStatePolling:
    async def test_calls_sync_and_prune(self) -> None:
        mock_win = MagicMock()
        mock_win.window_id = "@1"
        mock_win.window_name = "proj"
        with patch("ccgram.handlers.status_polling.session_manager") as mock_sm:
            mock_sm.window_states = {"@1": MagicMock()}
            mock_sm.iter_thread_bindings.return_value = [(1, 42, "@1")]
            mock_sm.sync_display_names.return_value = False
            mock_sm.prune_stale_state.return_value = False
            await _prune_stale_state([mock_win])
        mock_sm.prune_session_map.assert_called_once_with({"@1"})
        mock_sm.sync_display_names.assert_called_once_with([("@1", "proj")])
        mock_sm.prune_stale_state.assert_called_once_with({"@1"})
        mock_sm.prune_stale_window_states.assert_called_once_with({"@1"})
        mock_sm.prune_stale_offsets.assert_called_once_with({"@1"})

    async def test_empty_window_list(self) -> None:
        with patch("ccgram.handlers.status_polling.session_manager") as mock_sm:
            mock_sm.window_states = {}
            mock_sm.iter_thread_bindings.return_value = []
            mock_sm.sync_display_names.return_value = False
            mock_sm.prune_stale_state.return_value = False
            await _prune_stale_state([])
        mock_sm.prune_session_map.assert_called_once_with(set())
        mock_sm.sync_display_names.assert_called_once_with([])
        mock_sm.prune_stale_state.assert_called_once_with(set())
        mock_sm.prune_stale_window_states.assert_called_once_with(set())
        mock_sm.prune_stale_offsets.assert_called_once_with(set())


class TestProviderSwitchPromptSetup:
    async def test_switch_to_shell_offers_prompt_setup(self) -> None:
        from ccgram.handlers.status_polling import _maybe_discover_transcript

        bot = AsyncMock(spec=Bot)
        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccgram.handlers.status_polling.detect_provider_from_pane",
                new_callable=AsyncMock,
                return_value="shell",
            ),
            patch(
                "ccgram.providers.shell.setup_shell_prompt",
                new_callable=AsyncMock,
            ) as mock_setup,
        ):
            mock_sm.window_states = {
                "@7": MagicMock(
                    session_id="",
                    cwd="/proj",
                    provider_name="claude",
                    transcript_path="",
                )
            }
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(pane_current_command="fish", cwd="/proj")
            )
            await _maybe_discover_transcript("@7", bot=bot, user_id=1, thread_id=42)

        mock_sm.set_window_provider.assert_called_once_with("@7", "shell", cwd="/proj")
        mock_setup.assert_awaited_once_with("@7", clear=False)

    async def test_preserves_explicit_hookless_provider_during_shell_startup(
        self,
    ) -> None:
        from ccgram.handlers.status_polling import _maybe_discover_transcript

        mock_provider = MagicMock()
        mock_provider.capabilities.supports_hook = False
        mock_provider.capabilities.name = "codex"
        mock_provider.discover_transcript.return_value = None

        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccgram.handlers.status_polling.detect_provider_from_pane",
                new_callable=AsyncMock,
                return_value="shell",
            ),
            patch(
                "ccgram.handlers.status_polling.get_provider_for_window",
                return_value=mock_provider,
            ),
        ):
            mock_sm.window_states = {
                "@7": MagicMock(
                    session_id="",
                    cwd="/proj",
                    provider_name="codex",
                    transcript_path="",
                )
            }
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(pane_current_command="bash", cwd="/proj")
            )
            mock_tmux.get_pane_title = AsyncMock(return_value="")
            await _maybe_discover_transcript("@7")

        mock_sm.set_window_provider.assert_not_called()

    async def test_switch_to_claude_does_not_offer_prompt_setup(self) -> None:
        from ccgram.handlers.status_polling import _maybe_discover_transcript

        mock_provider = MagicMock()
        mock_provider.capabilities.supports_hook = True

        bot = AsyncMock(spec=Bot)
        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccgram.handlers.status_polling.detect_provider_from_pane",
                new_callable=AsyncMock,
                return_value="claude",
            ),
            patch(
                "ccgram.handlers.status_polling.get_provider_for_window",
                return_value=mock_provider,
            ),
            patch(
                "ccgram.providers.shell.setup_shell_prompt",
                new_callable=AsyncMock,
            ) as mock_setup,
        ):
            mock_sm.window_states = {
                "@7": MagicMock(
                    session_id="",
                    cwd="/proj",
                    provider_name="shell",
                    transcript_path="",
                )
            }
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(pane_current_command="claude", cwd="/proj")
            )
            await _maybe_discover_transcript("@7", bot=bot, user_id=1, thread_id=42)

        mock_setup.assert_not_awaited()

    async def test_fallback_shell_assignment_offers_prompt_setup(self) -> None:
        from ccgram.handlers.status_polling import _maybe_discover_transcript

        bot = AsyncMock(spec=Bot)
        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tmux,
            patch("ccgram.handlers.status_polling.config") as mock_config,
            patch(
                "ccgram.handlers.status_polling.detect_provider_from_pane",
                new_callable=AsyncMock,
                return_value="",
            ),
            patch(
                "ccgram.providers.shell.setup_shell_prompt",
                new_callable=AsyncMock,
            ) as mock_setup,
        ):
            mock_sm.window_states = {
                "@7": MagicMock(
                    session_id="",
                    cwd="/proj",
                    provider_name="",
                    transcript_path="",
                )
            }
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(pane_current_command="bash", cwd="/proj")
            )
            mock_tmux.get_pane_title = AsyncMock(return_value="")
            mock_config.tmux_session_name = "ccgram"
            await _maybe_discover_transcript("@7", bot=bot, user_id=1, thread_id=42)

        mock_sm.set_window_provider.assert_called_once_with("@7", "shell")
        mock_setup.assert_awaited_once_with("@7", clear=False)

    async def test_fallback_shell_assignment_sets_up_prompt_without_bot(self) -> None:
        from ccgram.handlers.status_polling import _maybe_discover_transcript

        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tmux,
            patch("ccgram.handlers.status_polling.config") as mock_config,
            patch(
                "ccgram.handlers.status_polling.detect_provider_from_pane",
                new_callable=AsyncMock,
                return_value="",
            ),
            patch(
                "ccgram.providers.shell.setup_shell_prompt",
                new_callable=AsyncMock,
            ) as mock_setup,
            patch(
                "ccgram.handlers.status_polling.should_probe_pane_title_for_provider_detection",
                return_value=False,
            ),
        ):
            mock_sm.window_states = {
                "@7": MagicMock(
                    session_id="",
                    cwd="/proj",
                    provider_name="",
                    transcript_path="",
                )
            }
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(pane_current_command="bash", cwd="/proj")
            )
            mock_tmux.get_pane_title = AsyncMock(return_value="")
            mock_config.tmux_session_name = "ccgram"
            await _maybe_discover_transcript("@7")

        mock_setup.assert_awaited_once_with("@7", clear=False)


class TestMaybeDiscoverTranscript:
    @pytest.fixture(autouse=True)
    def _inline_to_thread(self, monkeypatch) -> None:
        async def _run_inline(fn, /, *args, **kwargs):
            return fn(*args, **kwargs)

        monkeypatch.setattr(
            "ccgram.handlers.status_polling.asyncio.to_thread",
            _run_inline,
        )

    async def test_noop_when_discovered_session_matches_current(self) -> None:
        from ccgram.handlers.status_polling import _maybe_discover_transcript
        from ccgram.providers.base import SessionStartEvent

        mock_provider = MagicMock()
        mock_provider.capabilities.supports_hook = False
        mock_provider.capabilities.name = "codex"
        mock_provider.discover_transcript.return_value = SessionStartEvent(
            session_id="existing-id",
            cwd="/proj",
            transcript_path="/path/existing.jsonl",
            window_key="ccgram:@7",
        )

        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.status_polling.get_provider_for_window",
                return_value=mock_provider,
            ),
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tmux,
            patch("ccgram.handlers.status_polling.config") as mock_config,
        ):
            mock_sm.window_states = {
                "@7": MagicMock(
                    session_id="existing-id",
                    cwd="/proj",
                    transcript_path="/path/existing.jsonl",
                    provider_name="codex",
                )
            }
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(pane_current_command="bun")
            )
            mock_tmux.get_pane_title = AsyncMock(return_value="")
            mock_config.tmux_session_name = "ccgram"
            await _maybe_discover_transcript("@7")

        mock_sm.register_hookless_session.assert_not_called()
        mock_sm.write_hookless_session_map.assert_not_called()

    async def test_skips_when_no_cwd_and_no_tmux_window(self) -> None:
        from ccgram.handlers.status_polling import _maybe_discover_transcript

        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tmux,
        ):
            mock_sm.window_states = {"@7": MagicMock(session_id="", cwd="")}
            mock_tmux.find_window_by_id = AsyncMock(return_value=None)
            await _maybe_discover_transcript("@7")
        mock_sm.register_hookless_session.assert_not_called()

    async def test_falls_back_to_tmux_cwd_when_state_cwd_empty(self) -> None:
        from ccgram.handlers.status_polling import _maybe_discover_transcript
        from ccgram.providers.base import SessionStartEvent

        mock_provider = MagicMock()
        mock_provider.capabilities.supports_hook = False
        mock_provider.capabilities.name = "codex"
        event = SessionStartEvent(
            session_id="uuid-xyz",
            cwd="/my/project",
            transcript_path="/path/to/transcript.jsonl",
            window_key="ccgram:@7",
        )
        mock_provider.discover_transcript.return_value = event

        mock_state = MagicMock(session_id="", cwd="", provider_name="codex")
        mock_window = MagicMock(cwd="/my/project", pane_current_command="bun")

        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccgram.handlers.status_polling.get_provider_for_window",
                return_value=mock_provider,
            ),
            patch("ccgram.handlers.status_polling.config") as mock_config,
        ):
            mock_sm.window_states = {"@7": mock_state}
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.get_pane_title = AsyncMock(return_value="")
            mock_config.tmux_session_name = "ccgram"
            await _maybe_discover_transcript("@7")

        mock_sm.set_window_provider.assert_called_once_with(
            "@7", "codex", cwd="/my/project"
        )
        mock_sm.register_hookless_session.assert_called_once()

    async def test_skips_when_provider_has_hooks(self) -> None:
        from ccgram.handlers.status_polling import _maybe_discover_transcript

        mock_provider = MagicMock()
        mock_provider.capabilities.supports_hook = True
        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.status_polling.get_provider_for_window",
                return_value=mock_provider,
            ),
        ):
            mock_sm.window_states = {
                "@7": MagicMock(session_id="", cwd="/proj", provider_name="claude")
            }
            await _maybe_discover_transcript("@7")
        mock_sm.register_hookless_session.assert_not_called()

    async def test_skips_when_window_not_tracked(self) -> None:
        from ccgram.handlers.status_polling import _maybe_discover_transcript

        with patch("ccgram.handlers.status_polling.session_manager") as mock_sm:
            mock_sm.window_states = {}
            await _maybe_discover_transcript("@7")
        mock_sm.register_hookless_session.assert_not_called()

    async def test_registers_when_transcript_found(self) -> None:
        from ccgram.handlers.status_polling import _maybe_discover_transcript
        from ccgram.providers.base import SessionStartEvent

        mock_provider = MagicMock()
        mock_provider.capabilities.supports_hook = False
        mock_provider.capabilities.name = "codex"
        event = SessionStartEvent(
            session_id="uuid-abc",
            cwd="/my/project",
            transcript_path="/path/to/transcript.jsonl",
            window_key="ccgram:@7",
        )
        mock_provider.discover_transcript.return_value = event

        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.status_polling.get_provider_for_window",
                return_value=mock_provider,
            ),
            patch("ccgram.handlers.status_polling.config") as mock_config,
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tmux,
        ):
            mock_sm.window_states = {
                "@7": MagicMock(session_id="", cwd="/my/project", provider_name="codex")
            }
            mock_config.tmux_session_name = "ccgram"
            mock_window = MagicMock(pane_current_command="bun")
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.get_pane_title = AsyncMock(return_value="")
            await _maybe_discover_transcript("@7")

        mock_sm.register_hookless_session.assert_called_once_with(
            window_id="@7",
            session_id="uuid-abc",
            cwd="/my/project",
            transcript_path="/path/to/transcript.jsonl",
            provider_name="codex",
        )
        mock_sm.write_hookless_session_map.assert_called_once_with(
            window_id="@7",
            session_id="uuid-abc",
            cwd="/my/project",
            transcript_path="/path/to/transcript.jsonl",
            provider_name="codex",
        )

    async def test_updates_when_new_session_discovered_for_same_window(self) -> None:
        from ccgram.handlers.status_polling import _maybe_discover_transcript
        from ccgram.providers.base import SessionStartEvent

        mock_provider = MagicMock()
        mock_provider.capabilities.supports_hook = False
        mock_provider.capabilities.name = "codex"
        event = SessionStartEvent(
            session_id="uuid-new",
            cwd="/my/project",
            transcript_path="/path/to/new.jsonl",
            window_key="ccgram:@7",
        )
        mock_provider.discover_transcript.return_value = event

        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.status_polling.get_provider_for_window",
                return_value=mock_provider,
            ),
            patch("ccgram.handlers.status_polling.config") as mock_config,
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tmux,
        ):
            mock_sm.window_states = {
                "@7": MagicMock(
                    session_id="uuid-old",
                    cwd="/my/project",
                    transcript_path="/path/to/old.jsonl",
                    provider_name="codex",
                )
            }
            mock_config.tmux_session_name = "ccgram"
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(pane_current_command="bun")
            )
            mock_tmux.get_pane_title = AsyncMock(return_value="")
            await _maybe_discover_transcript("@7")

        mock_sm.register_hookless_session.assert_called_once_with(
            window_id="@7",
            session_id="uuid-new",
            cwd="/my/project",
            transcript_path="/path/to/new.jsonl",
            provider_name="codex",
        )

    async def test_noop_when_discovery_returns_none(self) -> None:
        from ccgram.handlers.status_polling import _maybe_discover_transcript

        mock_provider = MagicMock()
        mock_provider.capabilities.supports_hook = False
        mock_provider.discover_transcript.return_value = None

        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.status_polling.get_provider_for_window",
                return_value=mock_provider,
            ),
            patch("ccgram.handlers.status_polling.config") as mock_config,
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tmux,
        ):
            mock_sm.window_states = {
                "@7": MagicMock(session_id="", cwd="/proj", provider_name="codex")
            }
            mock_config.tmux_session_name = "ccgram"
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(pane_current_command="bun")
            )
            mock_tmux.get_pane_title = AsyncMock(return_value="")
            await _maybe_discover_transcript("@7")

        mock_sm.register_hookless_session.assert_not_called()
        mock_sm.write_hookless_session_map.assert_not_called()

    async def test_rewrites_session_map_when_matching_state_entry_missing(self) -> None:
        from ccgram.handlers.status_polling import _maybe_discover_transcript
        from ccgram.providers.base import SessionStartEvent

        mock_provider = MagicMock()
        mock_provider.capabilities.supports_hook = False
        mock_provider.capabilities.name = "codex"
        event = SessionStartEvent(
            session_id="uuid-abc",
            cwd="/my/project",
            transcript_path="/path/to/transcript.jsonl",
            window_key="ccgram:@7",
        )
        mock_provider.discover_transcript.return_value = event

        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.status_polling.get_provider_for_window",
                return_value=mock_provider,
            ),
            patch("ccgram.handlers.status_polling.config") as mock_config,
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tmux,
        ):
            mock_sm.window_states = {
                "@7": MagicMock(
                    session_id="uuid-abc",
                    cwd="/my/project",
                    transcript_path="/path/to/transcript.jsonl",
                    provider_name="codex",
                )
            }
            mock_sm.has_session_map_entry.return_value = False
            mock_config.tmux_session_name = "ccgram"
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(pane_current_command="bun")
            )
            mock_tmux.get_pane_title = AsyncMock(return_value="")
            await _maybe_discover_transcript("@7")

        mock_sm.register_hookless_session.assert_not_called()
        mock_sm.write_hookless_session_map.assert_called_once_with(
            window_id="@7",
            session_id="uuid-abc",
            cwd="/my/project",
            transcript_path="/path/to/transcript.jsonl",
            provider_name="codex",
        )

    async def test_session_map_write_runs_in_background_thread(self) -> None:
        """Regression: write_hookless_session_map must run in a thread (flock)."""
        from ccgram.handlers.status_polling import _maybe_discover_transcript
        from ccgram.providers.base import SessionStartEvent

        mock_provider = MagicMock()
        mock_provider.capabilities.supports_hook = False
        mock_provider.capabilities.name = "codex"
        event = SessionStartEvent(
            session_id="uuid-abc",
            cwd="/my/project",
            transcript_path="/path/to/transcript.jsonl",
            window_key="ccgram:@7",
        )
        mock_provider.discover_transcript.return_value = event

        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.status_polling.get_provider_for_window",
                return_value=mock_provider,
            ),
            patch("ccgram.handlers.status_polling.config") as mock_config,
            patch("ccgram.handlers.status_polling.asyncio") as mock_asyncio,
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tmux,
        ):
            mock_sm.window_states = {
                "@7": MagicMock(session_id="", cwd="/my/project", provider_name="codex")
            }
            mock_config.tmux_session_name = "ccgram"
            mock_window = MagicMock(pane_current_command="bun")
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.get_pane_title = AsyncMock(return_value="")
            mock_asyncio.to_thread = AsyncMock(side_effect=[event, None])
            await _maybe_discover_transcript("@7")

        # discover_transcript in thread, write_hookless_session_map in thread
        assert mock_asyncio.to_thread.call_count == 2
        discover_call = mock_asyncio.to_thread.call_args_list[0]
        assert discover_call.args[0] == mock_provider.discover_transcript
        write_call = mock_asyncio.to_thread.call_args_list[1]
        assert write_call.args[0] == mock_sm.write_hookless_session_map
        mock_sm.register_hookless_session.assert_called_once()

    async def test_tries_hookless_providers_when_provider_name_empty(self) -> None:
        """When provider_name is empty (detection failed), try all hookless providers."""
        from ccgram.handlers.status_polling import _maybe_discover_transcript
        from ccgram.providers.base import SessionStartEvent

        event = SessionStartEvent(
            session_id="uuid-found",
            cwd="/proj",
            transcript_path="/path/to/transcript.jsonl",
            window_key="ccgram:@7",
        )

        mock_codex = MagicMock()
        mock_codex.capabilities.supports_hook = False
        mock_codex.capabilities.name = "codex"
        mock_codex.discover_transcript.return_value = event

        mock_gemini = MagicMock()
        mock_gemini.capabilities.supports_hook = False
        mock_gemini.capabilities.name = "gemini"
        mock_gemini.discover_transcript.return_value = None

        mock_claude = MagicMock()
        mock_claude.capabilities.supports_hook = True
        mock_claude.capabilities.name = "claude"

        mock_registry = MagicMock()
        mock_registry.provider_names.return_value = ["claude", "codex", "gemini"]

        def mock_get(name: str) -> MagicMock:
            return {"claude": mock_claude, "codex": mock_codex, "gemini": mock_gemini}[
                name
            ]

        mock_registry.get = mock_get

        mock_window = MagicMock(pane_current_command="bun")

        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tmux,
            patch("ccgram.handlers.status_polling.config") as mock_config,
            patch("ccgram.providers.registry", mock_registry),
        ):
            mock_sm.window_states = {
                "@7": MagicMock(session_id="", cwd="/proj", provider_name="")
            }
            mock_config.tmux_session_name = "ccgram"
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.get_pane_title = AsyncMock(return_value="")
            await _maybe_discover_transcript("@7")

        mock_sm.register_hookless_session.assert_called_once_with(
            window_id="@7",
            session_id="uuid-found",
            cwd="/proj",
            transcript_path="/path/to/transcript.jsonl",
            provider_name="codex",
        )

    async def test_skips_hookless_fallback_when_pane_is_shell(self) -> None:
        """When provider_name is empty and pane is a shell, skip discovery."""
        from ccgram.handlers.status_polling import _maybe_discover_transcript

        mock_window = MagicMock(pane_current_command="bash")

        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tmux,
        ):
            mock_sm.window_states = {
                "@7": MagicMock(session_id="", cwd="/proj", provider_name="")
            }
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            await _maybe_discover_transcript("@7")

        mock_sm.register_hookless_session.assert_not_called()

    async def test_passes_max_age_zero_when_pane_is_alive(self) -> None:
        from ccgram.handlers.status_polling import _maybe_discover_transcript

        mock_provider = MagicMock()
        mock_provider.capabilities.supports_hook = False
        mock_provider.capabilities.name = "codex"
        mock_provider.discover_transcript.return_value = None

        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.status_polling.get_provider_for_window",
                return_value=mock_provider,
            ),
            patch("ccgram.handlers.status_polling.config") as mock_config,
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tmux,
            patch("ccgram.handlers.status_polling.asyncio") as mock_asyncio,
        ):
            mock_sm.window_states = {
                "@7": MagicMock(session_id="", cwd="/proj", provider_name="codex")
            }
            mock_config.tmux_session_name = "ccgram"
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(pane_current_command="bun")
            )
            mock_tmux.get_pane_title = AsyncMock(return_value="")
            mock_asyncio.to_thread = AsyncMock(return_value=None)
            await _maybe_discover_transcript("@7")

        discover_call = mock_asyncio.to_thread.call_args_list[0]
        assert discover_call.args[0] == mock_provider.discover_transcript
        assert discover_call.kwargs["max_age"] == 0

    async def test_passes_max_age_none_when_pane_not_alive(self) -> None:
        from ccgram.handlers.status_polling import _maybe_discover_transcript

        mock_provider = MagicMock()
        mock_provider.capabilities.supports_hook = False
        mock_provider.capabilities.name = "codex"
        mock_provider.discover_transcript.return_value = None

        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.status_polling.get_provider_for_window",
                return_value=mock_provider,
            ),
            patch("ccgram.handlers.status_polling.config") as mock_config,
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tmux,
            patch("ccgram.handlers.status_polling.asyncio") as mock_asyncio,
        ):
            mock_sm.window_states = {
                "@7": MagicMock(session_id="", cwd="/proj", provider_name="codex")
            }
            mock_config.tmux_session_name = "ccgram"
            mock_tmux.find_window_by_id = AsyncMock(return_value=None)
            mock_asyncio.to_thread = AsyncMock(return_value=None)
            await _maybe_discover_transcript("@7")

        discover_call = mock_asyncio.to_thread.call_args_list[0]
        assert discover_call.args[0] == mock_provider.discover_transcript
        assert discover_call.kwargs["max_age"] is None

    async def test_rebinds_stale_codex_window_to_gemini_from_pane_title(self) -> None:
        from ccgram.handlers.status_polling import _maybe_discover_transcript
        from ccgram.providers.base import SessionStartEvent

        mock_codex = MagicMock()
        mock_codex.capabilities.supports_hook = False
        mock_codex.capabilities.name = "codex"
        mock_codex.discover_transcript.return_value = None

        gemini_event = SessionStartEvent(
            session_id="gemini-uuid",
            cwd="/Users/alexei/Workspace/ccgram",
            transcript_path="/Users/alexei/.gemini/tmp/ccgram/chats/session.json",
            window_key="ccgram:@7",
        )
        mock_gemini = MagicMock()
        mock_gemini.capabilities.supports_hook = False
        mock_gemini.capabilities.name = "gemini"
        mock_gemini.discover_transcript.return_value = gemini_event

        mock_state = MagicMock(
            session_id="old-codex-id",
            cwd="/Users/alexei",
            transcript_path="/Users/alexei/.codex/sessions/old.jsonl",
            provider_name="codex",
        )

        def _provider_for_window(_: str) -> MagicMock:
            if mock_state.provider_name == "gemini":
                return mock_gemini
            return mock_codex

        def _set_window_provider(
            window_id: str, provider_name: str, *, cwd: str | None = None
        ) -> None:
            assert window_id == "@7"
            mock_state.provider_name = provider_name
            if cwd:
                mock_state.cwd = cwd

        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.status_polling.get_provider_for_window",
                side_effect=_provider_for_window,
            ),
            patch(
                "ccgram.handlers.status_polling.detect_provider_from_pane",
                new_callable=AsyncMock,
                return_value="",
            ),
            patch("ccgram.handlers.status_polling.config") as mock_config,
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tmux,
        ):
            mock_sm.window_states = {"@7": mock_state}
            mock_sm.set_window_provider.side_effect = _set_window_provider
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(
                    pane_current_command="bun",
                    cwd="/Users/alexei/Workspace/ccgram",
                )
            )
            mock_tmux.get_pane_title = AsyncMock(return_value="◇  Ready (ccbot)")
            mock_config.tmux_session_name = "ccgram"
            await _maybe_discover_transcript("@7")

        mock_codex.discover_transcript.assert_not_called()
        mock_gemini.discover_transcript.assert_called_once()
        mock_sm.set_window_provider.assert_called_once_with(
            "@7",
            "gemini",
            cwd="/Users/alexei/Workspace/ccgram",
        )
        mock_sm.register_hookless_session.assert_called_once_with(
            window_id="@7",
            session_id="gemini-uuid",
            cwd="/Users/alexei/Workspace/ccgram",
            transcript_path="/Users/alexei/.gemini/tmp/ccgram/chats/session.json",
            provider_name="gemini",
        )

    async def test_rebinds_stale_claude_window_to_codex_from_transcript_path(
        self,
    ) -> None:
        from ccgram.handlers.status_polling import _maybe_discover_transcript
        from ccgram.providers.base import SessionStartEvent

        codex_event = SessionStartEvent(
            session_id="codex-uuid",
            cwd="/Users/alexei/Workspace/ccgram",
            transcript_path="/Users/alexei/.codex/sessions/2026/03/23/test.jsonl",
            window_key="ccgram:@7",
        )
        mock_codex = MagicMock()
        mock_codex.capabilities.supports_hook = False
        mock_codex.capabilities.name = "codex"
        mock_codex.discover_transcript.return_value = codex_event

        mock_claude = MagicMock()
        mock_claude.capabilities.supports_hook = True
        mock_claude.capabilities.name = "claude"

        mock_state = MagicMock(
            session_id="old-claude-id",
            cwd="/Users/alexei/Workspace/ccgram",
            transcript_path="/Users/alexei/.codex/sessions/old.jsonl",
            provider_name="claude",
        )

        def _provider_for_window(_: str) -> MagicMock:
            if mock_state.provider_name == "codex":
                return mock_codex
            return mock_claude

        def _set_window_provider(
            window_id: str, provider_name: str, *, cwd: str | None = None
        ) -> None:
            assert window_id == "@7"
            mock_state.provider_name = provider_name
            if cwd:
                mock_state.cwd = cwd

        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.status_polling.get_provider_for_window",
                side_effect=_provider_for_window,
            ),
            patch(
                "ccgram.handlers.status_polling.detect_provider_from_pane",
                new_callable=AsyncMock,
                return_value="",
            ),
            patch(
                "ccgram.handlers.status_polling.should_probe_pane_title_for_provider_detection",
                return_value=False,
            ),
            patch("ccgram.handlers.status_polling.config") as mock_config,
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tmux,
        ):
            mock_sm.window_states = {"@7": mock_state}
            mock_sm.set_window_provider.side_effect = _set_window_provider
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(
                    pane_current_command="node",
                    cwd="/Users/alexei/Workspace/ccgram",
                )
            )
            mock_config.tmux_session_name = "ccgram"
            await _maybe_discover_transcript("@7")

        mock_sm.set_window_provider.assert_called_once_with(
            "@7",
            "codex",
            cwd="/Users/alexei/Workspace/ccgram",
        )
        mock_codex.discover_transcript.assert_called_once()
        mock_sm.register_hookless_session.assert_called_once_with(
            window_id="@7",
            session_id="codex-uuid",
            cwd="/Users/alexei/Workspace/ccgram",
            transcript_path="/Users/alexei/.codex/sessions/2026/03/23/test.jsonl",
            provider_name="codex",
        )


class TestDeadWindowNotification:
    async def test_marks_notified_even_when_send_fails(self) -> None:
        """When rate_limit_send_message returns None (send fails),
        _dead_notified is still populated so we don't retry forever."""
        bot = AsyncMock(spec=Bot)
        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.status_polling.rate_limit_send_message",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "ccgram.handlers.status_polling.update_topic_emoji",
                new_callable=AsyncMock,
            ),
            patch(
                "ccgram.handlers.status_polling.build_recovery_keyboard",
                return_value=None,
            ),
            patch(
                "ccgram.handlers.status_polling.asyncio.to_thread",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            mock_sm.resolve_chat_id.return_value = -100
            mock_sm.get_display_name.return_value = "test"
            mock_sm.get_window_state.return_value = MagicMock(cwd="/proj")
            await _handle_dead_window_notification(bot, 1, 42, "@5")

        assert (1, 42, "@5") in _dead_notified

    async def test_no_retry_after_failed_send(self) -> None:
        """Second call is a no-op because _dead_notified was set on first call."""
        bot = AsyncMock(spec=Bot)
        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch(
                "ccgram.handlers.status_polling.rate_limit_send_message",
                new_callable=AsyncMock,
                return_value=None,
            ) as mock_send,
            patch(
                "ccgram.handlers.status_polling.update_topic_emoji",
                new_callable=AsyncMock,
            ),
            patch(
                "ccgram.handlers.status_polling.build_recovery_keyboard",
                return_value=None,
            ),
            patch(
                "ccgram.handlers.status_polling.asyncio.to_thread",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            mock_sm.resolve_chat_id.return_value = -100
            mock_sm.get_display_name.return_value = "test"
            mock_sm.get_window_state.return_value = MagicMock(cwd="/proj")
            await _handle_dead_window_notification(bot, 1, 42, "@5")
            await _handle_dead_window_notification(bot, 1, 42, "@5")

        mock_send.assert_called_once()

    @pytest.mark.parametrize(
        "error_msg",
        [
            pytest.param("Message thread not found", id="capitalized"),
            pytest.param("message thread not found", id="lowercase"),
            pytest.param("Bad Request: Thread not found", id="thread-variant"),
        ],
    )
    async def test_probe_recreates_notify_topic_on_thread_not_found(
        self, error_msg: str
    ) -> None:
        """Notify topics self-heal instead of killing the running session."""
        bot = AsyncMock(spec=Bot)
        bot.unpin_all_forum_topic_messages.side_effect = BadRequest(error_msg)
        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tm,
            patch(
                "ccgram.handlers.status_polling.recreate_notify_topic_binding",
                new_callable=AsyncMock,
                return_value=77,
            ) as mock_recreate,
        ):
            mock_sm.iter_thread_bindings.return_value = [(1, 42, "@5")]
            mock_sm.resolve_chat_id.return_value = -100
            mock_sm.get_notification_mode.return_value = "notify"
            await _probe_topic_existence(bot)

        mock_recreate.assert_called_once_with(bot, 1, "@5", 42)
        mock_tm.kill_window.assert_not_called()
        mock_sm.unbind_thread.assert_not_called()

    async def test_probe_unbinds_interactive_topic_without_killing_window(self) -> None:
        bot = AsyncMock(spec=Bot)
        bot.unpin_all_forum_topic_messages.side_effect = BadRequest(
            "Message thread not found"
        )
        with (
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tm,
            patch(
                "ccgram.handlers.status_polling.clear_topic_state",
                new_callable=AsyncMock,
            ) as mock_cleanup,
        ):
            mock_sm.iter_thread_bindings.return_value = [(1, 42, "@5")]
            mock_sm.resolve_chat_id.return_value = -100
            mock_sm.get_notification_mode.return_value = "interactive"
            await _probe_topic_existence(bot)

        mock_tm.kill_window.assert_not_called()
        mock_cleanup.assert_called_once_with(1, 42, bot, window_id="@5")
        mock_sm.unbind_thread.assert_called_once_with(1, 42)


# ── Pane alert helpers ─────────────────────────────────────────────────


class TestPaneAlertHelpers:
    def test_has_pane_alert_true_when_present(self) -> None:
        _pane_alert_hashes["%1"] = ("prompt text", 100.0, "@0")
        assert has_pane_alert("%1") is True

    def test_has_pane_alert_false_when_absent(self) -> None:
        assert has_pane_alert("%99") is False

    def test_clear_pane_alerts_removes_for_window(self) -> None:
        _pane_alert_hashes["%1"] = ("prompt A", 100.0, "@0")
        _pane_alert_hashes["%2"] = ("prompt B", 100.0, "@0")
        _pane_alert_hashes["%3"] = ("prompt C", 100.0, "@5")
        clear_pane_alerts("@0")
        assert "%1" not in _pane_alert_hashes
        assert "%2" not in _pane_alert_hashes
        assert "%3" in _pane_alert_hashes


# ── Multi-pane scanning ────────────────────────────────────────────────


def _make_pane(pane_id: str = "%1", *, active: bool = True, index: int = 0) -> PaneInfo:
    return PaneInfo(
        pane_id=pane_id,
        index=index,
        active=active,
        command="claude",
        path="/tmp",
        width=80,
        height=24,
    )


class TestScanWindowPanes:
    async def test_skips_single_pane_window(self) -> None:
        bot = AsyncMock(spec=Bot)
        with (
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tm,
            patch(
                "ccgram.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle,
        ):
            mock_tm.list_panes = AsyncMock(return_value=[_make_pane()])
            await _scan_window_panes(bot, 1, "@0", 42)
        mock_handle.assert_not_called()

    async def test_detects_interactive_prompt_in_non_active_pane(self) -> None:
        from ccgram.providers.base import StatusUpdate

        bot = AsyncMock(spec=Bot)
        interactive = StatusUpdate(
            raw_text="Allow?",
            display_label="Allow?",
            is_interactive=True,
            ui_type="PermissionPrompt",
        )
        mock_provider = MagicMock()
        mock_provider.parse_terminal_status.return_value = interactive
        with (
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tm,
            patch(
                "ccgram.handlers.status_polling.get_provider_for_window",
                return_value=mock_provider,
            ),
            patch(
                "ccgram.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle,
        ):
            mock_tm.list_panes = AsyncMock(
                return_value=[_make_pane(), _make_pane("%2", active=False, index=1)]
            )
            mock_tm.capture_pane_by_id = AsyncMock(return_value="Allow?\nEsc\n")
            await _scan_window_panes(bot, 1, "@0", 42)
        mock_handle.assert_called_once_with(bot, 1, "@0", 42, pane_id="%2")

    async def test_skips_active_pane(self) -> None:
        bot = AsyncMock(spec=Bot)
        mock_provider = MagicMock()
        mock_provider.parse_terminal_status.return_value = None
        with (
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tm,
            patch(
                "ccgram.handlers.status_polling.get_provider_for_window",
                return_value=mock_provider,
            ),
            patch(
                "ccgram.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle,
        ):
            mock_tm.list_panes = AsyncMock(
                return_value=[_make_pane(), _make_pane("%2", active=False, index=1)]
            )
            mock_tm.capture_pane_by_id = AsyncMock(return_value="some text")
            await _scan_window_panes(bot, 1, "@0", 42)
        mock_handle.assert_not_called()
        mock_tm.capture_pane_by_id.assert_called_once_with("%2", window_id="@0")

    async def test_deduplicates_same_prompt(self) -> None:
        from ccgram.providers.base import StatusUpdate

        bot = AsyncMock(spec=Bot)
        interactive = StatusUpdate(
            raw_text="Allow write?",
            display_label="Allow write?",
            is_interactive=True,
            ui_type="PermissionPrompt",
        )
        mock_provider = MagicMock()
        mock_provider.parse_terminal_status.return_value = interactive
        with (
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tm,
            patch(
                "ccgram.handlers.status_polling.get_provider_for_window",
                return_value=mock_provider,
            ),
            patch(
                "ccgram.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle,
        ):
            mock_tm.list_panes = AsyncMock(
                return_value=[_make_pane(), _make_pane("%2", active=False, index=1)]
            )
            mock_tm.capture_pane_by_id = AsyncMock(return_value="Allow write?\nEsc\n")
            await _scan_window_panes(bot, 1, "@0", 42)
            await _scan_window_panes(bot, 1, "@0", 42)
        mock_handle.assert_called_once()

    async def test_clears_stale_alert_when_pane_disappears(self) -> None:
        _pane_alert_hashes["%2"] = ("old prompt", 100.0, "@0")
        bot = AsyncMock(spec=Bot)
        with patch("ccgram.handlers.status_polling.tmux_manager") as mock_tm:
            mock_tm.list_panes = AsyncMock(return_value=[_make_pane()])
            await _scan_window_panes(bot, 1, "@0", 42)
        assert "%2" not in _pane_alert_hashes

    async def test_clears_alert_when_interactive_ui_gone(self) -> None:
        _pane_alert_hashes["%2"] = ("old prompt", 100.0, "@0")
        bot = AsyncMock(spec=Bot)
        mock_provider = MagicMock()
        mock_provider.parse_terminal_status.return_value = None
        with (
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tm,
            patch(
                "ccgram.handlers.status_polling.get_provider_for_window",
                return_value=mock_provider,
            ),
            patch(
                "ccgram.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle,
        ):
            mock_tm.list_panes = AsyncMock(
                return_value=[_make_pane(), _make_pane("%2", active=False, index=1)]
            )
            mock_tm.capture_pane_by_id = AsyncMock(return_value="normal output")
            await _scan_window_panes(bot, 1, "@0", 42)
        assert "%2" not in _pane_alert_hashes
        mock_handle.assert_not_called()

    async def test_cached_pane_count_skips_subprocess(self) -> None:
        bot = AsyncMock(spec=Bot)
        with patch("ccgram.handlers.status_polling.tmux_manager") as mock_tm:
            mock_tm.list_panes = AsyncMock(return_value=[_make_pane()])
            await _scan_window_panes(bot, 1, "@0", 42)
            await _scan_window_panes(bot, 1, "@0", 42)
        mock_tm.list_panes.assert_called_once()


# ── update_status_message edge cases ───────────────────────────────────


@pytest.mark.usefixtures("_reset_pyte")
class TestUpdateStatusMessageEdgeCases:
    async def test_window_gone_enqueues_clear(self) -> None:
        from ccgram.handlers.status_polling import update_status_message

        bot = AsyncMock(spec=Bot)
        with (
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tm,
            patch(
                "ccgram.handlers.status_polling.enqueue_status_update",
                new_callable=AsyncMock,
            ) as mock_enqueue,
        ):
            mock_tm.find_window_by_id = AsyncMock(return_value=None)
            await update_status_message(bot, 1, "@0", thread_id=42)
        mock_enqueue.assert_called_once_with(bot, 1, "@0", None, thread_id=42)

    async def test_empty_capture_keeps_existing_status(self) -> None:
        from ccgram.handlers.status_polling import update_status_message

        bot = AsyncMock(spec=Bot)
        mock_window = MagicMock()
        mock_window.window_id = "@0"
        mock_window.pane_width = 80
        mock_window.pane_height = 24
        with (
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tm,
            patch(
                "ccgram.handlers.status_polling.enqueue_status_update",
                new_callable=AsyncMock,
            ) as mock_enqueue,
        ):
            mock_tm.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tm.capture_pane = AsyncMock(return_value=None)
            await update_status_message(bot, 1, "@0", thread_id=42)
        mock_enqueue.assert_not_called()

    async def test_vim_insert_detected_from_rendered_text(self) -> None:
        from ccgram.handlers.status_polling import update_status_message
        from ccgram.providers.base import StatusUpdate

        _get_window_state("@0").last_rendered_text = "some code\n-- INSERT --\n"
        pyte_status = StatusUpdate(raw_text="Working", display_label="...working")
        mock_window = MagicMock()
        mock_window.window_id = "@0"
        mock_window.pane_width = 80
        mock_window.pane_height = 24
        mock_window.pane_current_command = "node"
        bot = AsyncMock(spec=Bot)
        with (
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tm,
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch("ccgram.handlers.status_polling.update_topic_emoji"),
            patch("ccgram.handlers.status_polling.enqueue_status_update"),
            patch(
                "ccgram.handlers.status_polling.get_interactive_window",
                return_value=None,
            ),
            patch(
                "ccgram.handlers.status_polling._parse_with_pyte",
                return_value=pyte_status,
            ),
            patch("ccgram.tmux_manager.notify_vim_insert_seen") as mock_vim,
            patch("ccgram.tmux_manager._has_insert_indicator", return_value=True),
            patch("ccgram.handlers.status_polling._send_typing_throttled"),
            patch("ccgram.handlers.hook_events.get_subagent_names", return_value=[]),
        ):
            mock_tm.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tm.capture_pane = AsyncMock(return_value="\x1b[1mansi\x1b[0m")
            mock_sm.resolve_chat_id.return_value = -100
            mock_sm.get_display_name.return_value = "project"
            mock_sm.get_notification_mode.return_value = "normal"
            await update_status_message(bot, 1, "@0", thread_id=42)
        mock_vim.assert_called_once_with("@0")

    async def test_status_includes_subagent_names(self) -> None:
        from ccgram.handlers.status_polling import update_status_message
        from ccgram.providers.base import StatusUpdate

        pyte_status = StatusUpdate(
            raw_text="Working", display_label="\u23f3 Working\u2026"
        )
        mock_window = MagicMock()
        mock_window.window_id = "@0"
        mock_window.pane_width = 80
        mock_window.pane_height = 24
        mock_window.pane_current_command = "node"
        bot = AsyncMock(spec=Bot)
        with (
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tm,
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch("ccgram.handlers.status_polling.update_topic_emoji"),
            patch(
                "ccgram.handlers.status_polling.enqueue_status_update",
                new_callable=AsyncMock,
            ) as mock_enqueue,
            patch(
                "ccgram.handlers.status_polling.get_interactive_window",
                return_value=None,
            ),
            patch(
                "ccgram.handlers.status_polling._parse_with_pyte",
                return_value=pyte_status,
            ),
            patch("ccgram.tmux_manager._has_insert_indicator", return_value=False),
            patch("ccgram.tmux_manager.notify_vim_insert_seen"),
            patch("ccgram.handlers.status_polling._send_typing_throttled"),
            patch(
                "ccgram.handlers.hook_events.get_subagent_names",
                return_value=["write-tests"],
            ),
        ):
            mock_tm.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tm.capture_pane = AsyncMock(return_value="some output")
            mock_sm.resolve_chat_id.return_value = -100
            mock_sm.get_display_name.return_value = "project"
            mock_sm.get_notification_mode.return_value = "normal"
            await update_status_message(bot, 1, "@0", thread_id=42)
        status_text = mock_enqueue.call_args[0][3]
        assert "write-tests" in status_text
        assert "\U0001f916" in status_text

    async def test_interactive_window_clears_when_ui_disappears(self) -> None:
        from ccgram.handlers.status_polling import update_status_message
        from ccgram.providers.base import StatusUpdate

        non_interactive = StatusUpdate(raw_text="Working", display_label="...working")
        mock_window = MagicMock()
        mock_window.window_id = "@0"
        mock_window.pane_width = 80
        mock_window.pane_height = 24
        mock_window.pane_current_command = "node"
        bot = AsyncMock(spec=Bot)
        with (
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tm,
            patch("ccgram.handlers.status_polling.session_manager") as mock_sm,
            patch("ccgram.handlers.status_polling.update_topic_emoji"),
            patch("ccgram.handlers.status_polling.enqueue_status_update"),
            patch(
                "ccgram.handlers.status_polling.get_interactive_window",
                return_value="@0",
            ),
            patch(
                "ccgram.handlers.status_polling._parse_with_pyte",
                return_value=non_interactive,
            ),
            patch(
                "ccgram.handlers.status_polling.clear_interactive_msg",
                new_callable=AsyncMock,
            ) as mock_clear,
            patch("ccgram.tmux_manager._has_insert_indicator", return_value=False),
            patch("ccgram.tmux_manager.notify_vim_insert_seen"),
            patch("ccgram.handlers.status_polling._send_typing_throttled"),
            patch("ccgram.handlers.hook_events.get_subagent_names", return_value=[]),
        ):
            mock_tm.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tm.capture_pane = AsyncMock(return_value="some output")
            mock_sm.resolve_chat_id.return_value = -100
            mock_sm.get_display_name.return_value = "project"
            mock_sm.get_notification_mode.return_value = "normal"
            await update_status_message(bot, 1, "@0", thread_id=42)
        mock_clear.assert_called_once_with(1, bot, 42)

    async def test_new_interactive_ui_enters_interactive_mode(self) -> None:
        from ccgram.handlers.status_polling import update_status_message
        from ccgram.providers.base import StatusUpdate

        interactive_status = StatusUpdate(
            raw_text="Allow?",
            display_label="Allow?",
            is_interactive=True,
            ui_type="PermissionPrompt",
        )
        mock_window = MagicMock()
        mock_window.window_id = "@0"
        mock_window.pane_width = 80
        mock_window.pane_height = 24
        mock_window.pane_current_command = "node"
        bot = AsyncMock(spec=Bot)
        with (
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tm,
            patch("ccgram.handlers.status_polling.session_manager"),
            patch("ccgram.handlers.status_polling.update_topic_emoji"),
            patch("ccgram.handlers.status_polling.enqueue_status_update"),
            patch(
                "ccgram.handlers.status_polling.get_interactive_window",
                return_value=None,
            ),
            patch(
                "ccgram.handlers.status_polling._parse_with_pyte",
                return_value=interactive_status,
            ),
            patch(
                "ccgram.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle,
            patch("ccgram.tmux_manager._has_insert_indicator", return_value=False),
            patch("ccgram.tmux_manager.notify_vim_insert_seen"),
        ):
            mock_tm.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tm.capture_pane = AsyncMock(return_value="Allow?\nEsc\n")
            await update_status_message(bot, 1, "@0", thread_id=42)
        mock_handle.assert_called_once_with(bot, 1, "@0", 42)


@pytest.mark.usefixtures("_reset_pyte")
class TestCheckInteractiveOnly:
    """Tests for _check_interactive_only — interactive UI detection during queue backlog."""

    @pytest.mark.parametrize(
        "interactive_window",
        [
            pytest.param(None, id="no_active_ui"),
            pytest.param("@1", id="different_window_active"),
        ],
    )
    async def test_detects_interactive_ui(self, interactive_window: str | None) -> None:
        from ccgram.handlers.status_polling import _check_interactive_only
        from ccgram.providers.base import StatusUpdate

        interactive_status = StatusUpdate(
            raw_text="Allow?",
            display_label="Allow?",
            is_interactive=True,
            ui_type="PermissionPrompt",
        )
        mock_window = MagicMock()
        mock_window.window_id = "@0"
        mock_window.pane_width = 80
        mock_window.pane_height = 24
        bot = AsyncMock(spec=Bot)
        with (
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tm,
            patch(
                "ccgram.handlers.status_polling.get_interactive_window",
                return_value=interactive_window,
            ),
            patch(
                "ccgram.handlers.status_polling._parse_with_pyte",
                return_value=interactive_status,
            ) as mock_pyte,
            patch(
                "ccgram.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle,
            patch(
                "ccgram.handlers.status_polling.set_interactive_mode",
            ) as mock_set,
        ):
            mock_tm.capture_pane = AsyncMock(return_value="Allow?\nEsc\n")
            await _check_interactive_only(bot, 1, "@0", 42, _window=mock_window)
        mock_pyte.assert_called_once_with("@0", "Allow?\nEsc\n", columns=80, rows=24)
        mock_set.assert_called_once_with(1, "@0", 42)
        mock_handle.assert_called_once_with(bot, 1, "@0", 42)

    async def test_clears_interactive_mode_on_handle_failure(self) -> None:
        from ccgram.handlers.status_polling import _check_interactive_only
        from ccgram.providers.base import StatusUpdate

        interactive_status = StatusUpdate(
            raw_text="Allow?",
            display_label="Allow?",
            is_interactive=True,
            ui_type="PermissionPrompt",
        )
        mock_window = MagicMock()
        mock_window.window_id = "@0"
        mock_window.pane_width = 80
        mock_window.pane_height = 24
        bot = AsyncMock(spec=Bot)
        with (
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tm,
            patch(
                "ccgram.handlers.status_polling.get_interactive_window",
                return_value=None,
            ),
            patch(
                "ccgram.handlers.status_polling._parse_with_pyte",
                return_value=interactive_status,
            ),
            patch(
                "ccgram.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "ccgram.handlers.status_polling.set_interactive_mode",
            ) as mock_set,
            patch(
                "ccgram.handlers.status_polling.clear_interactive_mode",
            ) as mock_clear,
        ):
            mock_tm.capture_pane = AsyncMock(return_value="Allow?\nEsc\n")
            await _check_interactive_only(bot, 1, "@0", 42, _window=mock_window)
        mock_set.assert_called_once_with(1, "@0", 42)
        mock_clear.assert_called_once_with(1, 42)

    async def test_skips_when_already_interactive(self) -> None:
        from ccgram.handlers.status_polling import _check_interactive_only

        mock_window = MagicMock()
        mock_window.window_id = "@0"
        bot = AsyncMock(spec=Bot)
        with (
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tm,
            patch(
                "ccgram.handlers.status_polling.get_interactive_window",
                return_value="@0",
            ),
            patch(
                "ccgram.handlers.status_polling._parse_with_pyte",
            ) as mock_pyte,
            patch(
                "ccgram.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle,
        ):
            mock_tm.capture_pane = AsyncMock()
            await _check_interactive_only(bot, 1, "@0", 42, _window=mock_window)
        mock_tm.capture_pane.assert_not_called()
        mock_pyte.assert_not_called()
        mock_handle.assert_not_called()

    async def test_no_action_when_not_interactive(self) -> None:
        from ccgram.handlers.status_polling import _check_interactive_only
        from ccgram.providers.base import StatusUpdate

        normal_status = StatusUpdate(
            raw_text="Reading file", display_label="reading..."
        )
        mock_window = MagicMock()
        mock_window.window_id = "@0"
        mock_window.pane_width = 80
        mock_window.pane_height = 24
        bot = AsyncMock(spec=Bot)
        with (
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tm,
            patch(
                "ccgram.handlers.status_polling.get_interactive_window",
                return_value=None,
            ),
            patch(
                "ccgram.handlers.status_polling._parse_with_pyte",
                return_value=normal_status,
            ),
            patch(
                "ccgram.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle,
        ):
            mock_tm.capture_pane = AsyncMock(return_value="some output")
            await _check_interactive_only(bot, 1, "@0", 42, _window=mock_window)
        mock_handle.assert_not_called()

    async def test_no_action_when_window_gone(self) -> None:
        from ccgram.handlers.status_polling import _check_interactive_only

        bot = AsyncMock(spec=Bot)
        with (
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tm,
            patch(
                "ccgram.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle,
        ):
            mock_tm.find_window_by_id = AsyncMock(return_value=None)
            await _check_interactive_only(bot, 1, "@0", 42)
        mock_handle.assert_not_called()

    async def test_no_action_on_empty_capture(self) -> None:
        from ccgram.handlers.status_polling import _check_interactive_only

        mock_window = MagicMock()
        mock_window.window_id = "@0"
        bot = AsyncMock(spec=Bot)
        with (
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tm,
            patch(
                "ccgram.handlers.status_polling.get_interactive_window",
                return_value=None,
            ),
            patch(
                "ccgram.handlers.status_polling._parse_with_pyte",
            ) as mock_pyte,
            patch(
                "ccgram.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle,
        ):
            mock_tm.capture_pane = AsyncMock(return_value="")
            await _check_interactive_only(bot, 1, "@0", 42, _window=mock_window)
        mock_pyte.assert_not_called()
        mock_handle.assert_not_called()

    @pytest.mark.parametrize(
        ("uses_pane_title", "expected_title"),
        [
            pytest.param(False, "", id="no_pane_title"),
            pytest.param(True, "gemini-title", id="with_pane_title"),
        ],
    )
    async def test_falls_back_to_provider_regex(
        self, uses_pane_title: bool, expected_title: str
    ) -> None:
        from ccgram.handlers.status_polling import _check_interactive_only
        from ccgram.providers.base import StatusUpdate

        interactive_status = StatusUpdate(
            raw_text="Allow?",
            display_label="Allow?",
            is_interactive=True,
            ui_type="PermissionPrompt",
        )
        mock_provider = MagicMock()
        mock_provider.capabilities.uses_pane_title = uses_pane_title
        mock_provider.parse_terminal_status.return_value = interactive_status
        mock_window = MagicMock()
        mock_window.window_id = "@0"
        mock_window.pane_width = 80
        mock_window.pane_height = 24
        bot = AsyncMock(spec=Bot)
        with (
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tm,
            patch(
                "ccgram.handlers.status_polling.get_interactive_window",
                return_value=None,
            ),
            patch(
                "ccgram.handlers.status_polling._parse_with_pyte",
                return_value=None,
            ),
            patch(
                "ccgram.handlers.status_polling.get_provider_for_window",
                return_value=mock_provider,
            ),
            patch(
                "ccgram.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle,
            patch("ccgram.handlers.status_polling.set_interactive_mode"),
        ):
            mock_tm.capture_pane = AsyncMock(return_value="Allow?\nEsc\n")
            mock_tm.get_pane_title = AsyncMock(return_value="gemini-title")
            await _check_interactive_only(bot, 1, "@0", 42, _window=mock_window)
        mock_provider.parse_terminal_status.assert_called_once_with(
            "Allow?\nEsc\n", pane_title=expected_title
        )
        mock_handle.assert_called_once_with(bot, 1, "@0", 42)
        if uses_pane_title:
            mock_tm.get_pane_title.assert_called_once_with("@0")

    async def test_falls_back_to_plain_capture_when_rendered_text_misses(self) -> None:
        from ccgram.handlers.status_polling import _check_interactive_only
        from ccgram.providers.base import StatusUpdate

        interactive_status = StatusUpdate(
            raw_text="Allow?",
            display_label="Allow?",
            is_interactive=True,
            ui_type="PermissionPrompt",
        )
        mock_provider = MagicMock()
        mock_provider.capabilities.uses_pane_title = False
        mock_provider.parse_terminal_status.side_effect = [None, interactive_status]
        mock_window = MagicMock()
        mock_window.window_id = "@0"
        mock_window.pane_width = 80
        mock_window.pane_height = 24
        bot = AsyncMock(spec=Bot)
        with (
            patch("ccgram.handlers.status_polling.tmux_manager") as mock_tm,
            patch(
                "ccgram.handlers.status_polling.get_interactive_window",
                return_value=None,
            ),
            patch(
                "ccgram.handlers.status_polling._parse_with_pyte",
                return_value=None,
            ),
            patch(
                "ccgram.handlers.status_polling.get_provider_for_window",
                return_value=mock_provider,
            ),
            patch(
                "ccgram.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle,
            patch("ccgram.handlers.status_polling.set_interactive_mode"),
        ):
            _get_window_state("@0").last_rendered_text = ""
            mock_tm.capture_pane = AsyncMock(
                side_effect=[
                    "\x1b[1mAllow?\x1b[0m",
                    "Allow?\n1. Yes\n2. No\n",
                ]
            )
            await _check_interactive_only(bot, 1, "@0", 42, _window=mock_window)
        assert mock_provider.parse_terminal_status.call_args_list == [
            (("",), {"pane_title": ""}),
            (("Allow?\n1. Yes\n2. No\n",), {"pane_title": ""}),
        ]
        mock_handle.assert_called_once_with(bot, 1, "@0", 42)
