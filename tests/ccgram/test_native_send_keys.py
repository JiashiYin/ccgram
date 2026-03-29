"""Focused tests for native-window key delivery."""

from unittest.mock import AsyncMock, call, patch

import pytest

from ccgram.tmux_manager import TmuxManager


class TestNativeSendKeys:
    @pytest.mark.asyncio
    async def test_native_literal_send_separates_enter(self) -> None:
        manager = TmuxManager(session_name="test")

        with (
            patch(
                "ccgram.tmux_manager.send_native_keys",
                side_effect=[True, True],
            ) as mock_send,
            patch("ccgram.tmux_manager.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await manager.send_keys(
                "native:abc123",
                "@MyBot \nhello from telegram",
                enter=True,
                literal=True,
            )

        assert result is True
        assert mock_send.call_args_list == [
            call(
                "native:abc123",
                "@MyBot \nhello from telegram",
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

    @pytest.mark.asyncio
    async def test_native_literal_send_returns_false_if_enter_submit_fails(self) -> None:
        manager = TmuxManager(session_name="test")

        with (
            patch(
                "ccgram.tmux_manager.send_native_keys",
                side_effect=[True, False],
            ) as mock_send,
            patch("ccgram.tmux_manager.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await manager.send_keys(
                "native:abc123",
                "hello",
                enter=True,
                literal=True,
            )

        assert result is False
        assert mock_send.call_count == 2
