"""Guided Telegram onboarding for ``ccgram notify install``."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import click

from .notify_shell import (
    _dotenv_path,
    _read_env_value,
    _remove_env_value,
    _set_env_value,
)

_BOT_TOKEN_KEY = "TELEGRAM_BOT_TOKEN"
_ALLOWED_USERS_KEY = "ALLOWED_USERS"
_GROUP_ID_KEY = "CCGRAM_GROUP_ID"
_LEGACY_GROUP_ID_KEY = "CCBOT_GROUP_ID"


@dataclass(frozen=True)
class TelegramSetup:
    """Resolved Telegram config used by ccgram."""

    bot_token: str
    allowed_users: str
    group_id: str


def _env_file_candidates() -> tuple[Path, ...]:
    return (Path(".env"), _dotenv_path())


def _first_present_value(*keys: str) -> str:
    for key in keys:
        value = os.environ.get(key, "").strip()
        if value:
            return value
    for env_path in _env_file_candidates():
        for key in keys:
            value = _read_env_value(env_path, key).strip()
            if value:
                return value
    return ""


def _normalize_allowed_users(raw: str) -> str:
    users: list[str] = []
    for token in raw.split(","):
        stripped = token.strip()
        if not stripped:
            continue
        users.append(str(int(stripped)))
    if not users:
        raise ValueError("ALLOWED_USERS is required")
    return ",".join(users)


def _normalize_group_id(raw: str) -> str:
    stripped = raw.strip()
    if not stripped:
        return ""
    return str(int(stripped))


def resolve_telegram_setup(
    *,
    bot_token: str | None,
    allowed_users: str | None,
    group_id: str | None,
    non_interactive: bool,
) -> TelegramSetup:
    """Resolve Telegram config from flags, env, dotenv, and prompts."""
    resolved_bot_token = (bot_token or "").strip() or _first_present_value(
        _BOT_TOKEN_KEY
    )
    resolved_allowed_users = (allowed_users or "").strip() or _first_present_value(
        _ALLOWED_USERS_KEY
    )
    resolved_group_id = (group_id or "").strip() or _first_present_value(
        _GROUP_ID_KEY,
        _LEGACY_GROUP_ID_KEY,
    )
    prompted_required = False

    if not resolved_bot_token:
        if non_interactive:
            raise click.ClickException(
                "TELEGRAM_BOT_TOKEN is required. Re-run without "
                "`--non-interactive` or provide `--bot-token`."
            )
        resolved_bot_token = click.prompt("Telegram bot token", hide_input=True).strip()
        prompted_required = True

    if not resolved_allowed_users:
        if non_interactive:
            raise click.ClickException(
                "ALLOWED_USERS is required. Re-run without `--non-interactive` "
                "or provide `--allowed-users`."
            )
        resolved_allowed_users = click.prompt(
            "Telegram user IDs allowed to control this bot (comma-separated)"
        ).strip()
        prompted_required = True

    if not resolved_group_id and prompted_required and not non_interactive:
        resolved_group_id = click.prompt(
            "Telegram group ID (optional, press Enter to skip)",
            default="",
            show_default=False,
        ).strip()

    try:
        normalized_allowed_users = _normalize_allowed_users(resolved_allowed_users)
    except ValueError as exc:
        raise click.ClickException(
            "ALLOWED_USERS must be a comma-separated list of numeric Telegram user IDs."
        ) from exc

    try:
        normalized_group_id = _normalize_group_id(resolved_group_id)
    except ValueError as exc:
        raise click.ClickException(
            "CCGRAM_GROUP_ID must be numeric when provided."
        ) from exc

    return TelegramSetup(
        bot_token=resolved_bot_token,
        allowed_users=normalized_allowed_users,
        group_id=normalized_group_id,
    )


def persist_telegram_setup(setup: TelegramSetup) -> Path:
    """Persist resolved Telegram config into ccgram's dotenv file."""
    dotenv_path = _dotenv_path()
    _set_env_value(dotenv_path, _BOT_TOKEN_KEY, setup.bot_token)
    _set_env_value(dotenv_path, _ALLOWED_USERS_KEY, setup.allowed_users)
    if setup.group_id:
        _set_env_value(dotenv_path, _GROUP_ID_KEY, setup.group_id)
    else:
        _remove_env_value(dotenv_path, _GROUP_ID_KEY)
        _remove_env_value(dotenv_path, _LEGACY_GROUP_ID_KEY)
    return dotenv_path
