"""Focused tests for native-window key delivery."""

from unittest.mock import AsyncMock, call, patch

import pytest

from ccgram.tmux_manager import TmuxManager


class TestNativeSendKeys:
    @pytest.mark.asyncio
    async def test_native_multiline_send_waits_for_snapshot_settle_before_enter(
        self,
    ) -> None:
        manager = TmuxManager(session_name="test")

        with (
            patch(
                "ccgram.tmux_manager.send_native_keys",
                side_effect=[True, True],
            ) as mock_send,
            patch.object(
                manager,
                "_capture_native_snapshot",
                side_effect=[
                    None,
                    "partial draft",
                    "@MyBot long multiline draft that ends with tail marker has landed",
                    "@MyBot long multiline draft that ends with tail marker has landed",
                ],
                new_callable=AsyncMock,
            ) as mock_capture,
            patch("ccgram.tmux_manager._NATIVE_SNAPSHOT_POLL_INTERVAL", 0.0),
        ):
            result = await manager.send_keys(
                "native:abc123",
                "@MyBot\nlong multiline draft\nthat ends with tail marker has landed",
                enter=True,
                literal=True,
            )

        assert result is True
        assert mock_send.call_args_list == [
            call(
                "native:abc123",
                "@MyBot\nlong multiline draft\nthat ends with tail marker has landed",
                enter=False,
                literal=True,
            ),
            call(
                "native:abc123",
                "Enter",
                enter=False,
                literal=False,
            ),
        ]
        assert mock_capture.call_count == 4

    @pytest.mark.asyncio
    async def test_native_literal_send_returns_false_if_enter_submit_fails(self) -> None:
        manager = TmuxManager(session_name="test")

        with (
            patch(
                "ccgram.tmux_manager.send_native_keys",
                side_effect=[True, False],
            ) as mock_send,
            patch("ccgram.tmux_manager._LITERAL_SUBMIT_SETTLE_DELAY", 0.0),
        ):
            result = await manager.send_keys(
                "native:abc123",
                "hello",
                enter=True,
                literal=True,
            )

        assert result is False
        assert mock_send.call_count == 2

    @pytest.mark.asyncio
    async def test_native_multiline_send_falls_back_when_snapshot_never_matches(self) -> None:
        manager = TmuxManager(session_name="test")

        with (
            patch(
                "ccgram.tmux_manager.send_native_keys",
                side_effect=[True, True],
            ) as mock_send,
            patch.object(
                manager,
                "_capture_native_snapshot",
                return_value="unrelated screen state",
                new_callable=AsyncMock,
            ) as mock_capture,
            patch("ccgram.tmux_manager._NATIVE_SNAPSHOT_POLL_INTERVAL", 0.0),
            patch("ccgram.tmux_manager._LITERAL_SUBMIT_SETTLE_DELAY", 0.0),
        ):
            result = await manager.send_keys(
                "native:abc123",
                "@MyBot\nhello from telegram",
                enter=True,
                literal=True,
            )

        assert result is True
        assert mock_send.call_count == 2
        assert mock_capture.call_count == 20
