"""Tests for shared-topic bot routing helpers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccgram.handlers.topic_routing import (
    classify_topic_routing,
    designated_prompt_owner,
    extract_default_responder,
    format_topic_name_with_default_responder,
    has_legacy_default_responder,
)


class TestTopicRoutingMarkers:
    def test_extract_default_responder(self) -> None:
        base, responder = extract_default_responder("helloworld [@BotOne]")
        assert base == "helloworld"
        assert responder == "@BotOne"

    def test_extract_default_responder_accepts_legacy_marker(self) -> None:
        base, responder = extract_default_responder("helloworld [ccgram:@BotOne]")
        assert base == "helloworld"
        assert responder == "@BotOne"

    def test_detects_legacy_default_responder_marker(self) -> None:
        assert has_legacy_default_responder("helloworld [ccgram:@BotOne]") is True
        assert has_legacy_default_responder("helloworld [@BotOne]") is False

    def test_format_topic_name_with_default_responder(self) -> None:
        assert (
            format_topic_name_with_default_responder("helloworld", "BotOne")
            == "helloworld [@BotOne]"
        )

    def test_designated_prompt_owner_uses_stable_order(self) -> None:
        assert designated_prompt_owner(("@zbot", "@Abot")) == "@Abot"


class TestClassifyTopicRouting:
    @pytest.mark.asyncio
    async def test_single_bot_chat_handles_unaddressed_message(self) -> None:
        bot = MagicMock()
        with patch(
            "ccgram.handlers.topic_routing.get_admin_bot_usernames",
            new_callable=AsyncMock,
            return_value=("@MyBot",),
        ):
            decision, claim_default = await classify_topic_routing(
                bot=bot,
                chat_id=-100,
                display_name="helloworld",
                bot_username="MyBot",
                explicit_self_target=False,
            )

        assert decision == "handle"
        assert claim_default is False

    @pytest.mark.asyncio
    async def test_shared_chat_prompts_only_designated_owner_when_unmarked(self) -> None:
        bot = MagicMock()
        with patch(
            "ccgram.handlers.topic_routing.get_admin_bot_usernames",
            new_callable=AsyncMock,
            return_value=("@AlphaBot", "@BetaBot"),
        ):
            decision, claim_default = await classify_topic_routing(
                bot=bot,
                chat_id=-100,
                display_name="helloworld",
                bot_username="AlphaBot",
                explicit_self_target=False,
            )

        assert decision == "prompt"
        assert claim_default is False

    @pytest.mark.asyncio
    async def test_shared_chat_ignores_unaddressed_message_for_non_default_bot(self) -> None:
        bot = MagicMock()
        with patch(
            "ccgram.handlers.topic_routing.get_admin_bot_usernames",
            new_callable=AsyncMock,
            return_value=("@AlphaBot", "@BetaBot"),
        ):
            decision, claim_default = await classify_topic_routing(
                bot=bot,
                chat_id=-100,
                display_name="helloworld [@BetaBot]",
                bot_username="AlphaBot",
                explicit_self_target=False,
            )

        assert decision == "ignore"
        assert claim_default is False

    @pytest.mark.asyncio
    async def test_explicit_self_target_claims_default_in_shared_chat(self) -> None:
        bot = MagicMock()
        with patch(
            "ccgram.handlers.topic_routing.get_admin_bot_usernames",
            new_callable=AsyncMock,
            return_value=("@AlphaBot", "@BetaBot"),
        ):
            decision, claim_default = await classify_topic_routing(
                bot=bot,
                chat_id=-100,
                display_name="helloworld",
                bot_username="AlphaBot",
                explicit_self_target=True,
            )

        assert decision == "handle"
        assert claim_default is True

    @pytest.mark.asyncio
    async def test_explicit_self_target_does_not_claim_default_in_single_bot_chat(self) -> None:
        bot = MagicMock()
        with patch(
            "ccgram.handlers.topic_routing.get_admin_bot_usernames",
            new_callable=AsyncMock,
            return_value=("@AlphaBot",),
        ):
            decision, claim_default = await classify_topic_routing(
                bot=bot,
                chat_id=-100,
                display_name="helloworld",
                bot_username="AlphaBot",
                explicit_self_target=True,
            )

        assert decision == "handle"
        assert claim_default is False

    @pytest.mark.asyncio
    async def test_explicit_self_target_rewrites_legacy_marker(self) -> None:
        bot = MagicMock()
        with patch(
            "ccgram.handlers.topic_routing.get_admin_bot_usernames",
            new_callable=AsyncMock,
            return_value=("@AlphaBot", "@BetaBot"),
        ):
            decision, claim_default = await classify_topic_routing(
                bot=bot,
                chat_id=-100,
                display_name="helloworld [ccgram:@AlphaBot]",
                bot_username="AlphaBot",
                explicit_self_target=True,
            )

        assert decision == "handle"
        assert claim_default is True
