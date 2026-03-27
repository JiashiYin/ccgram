"""CLI commands for Codex-first notify setup and guided onboarding."""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
from pathlib import Path

import click

from .native_sessions import run_native_notify_session
from .notify_onboarding import persist_telegram_setup, resolve_telegram_setup
from .notify_service import (
    disable_notify_service,
    ensure_notify_service_running,
    format_notify_service_status,
    get_notify_service_status,
    uninstall_notify_service,
)
from .notify_shell import (
    any_notify_providers_enabled,
    disable_notify_shell,
    get_notify_status,
    install_notify_shell,
    resolve_notify_launch_command,
    uninstall_notify_shell,
)
from .utils import tmux_session_name

_NOTIFY_MODES = ("notify", "interactive")
_SHELLS = ("bash", "zsh", "fish")


class _LazyTmuxManagerProxy:
    """Delay importing tmux_manager until a launch path actually needs it."""

    def __getattr__(self, name: str) -> object:
        from .tmux_manager import tmux_manager as real_tmux_manager

        return getattr(real_tmux_manager, name)


class _LazySessionManagerProxy:
    """Delay importing session_manager until a launch path actually needs it."""

    def __getattr__(self, name: str) -> object:
        from .session import session_manager as real_session_manager

        return getattr(real_session_manager, name)


tmux_manager = _LazyTmuxManagerProxy()
session_manager = _LazySessionManagerProxy()


def _print_notify_status(provider: str) -> None:
    status = get_notify_status(provider)
    print(f"Provider: {provider}")
    print(
        f"Status: {'enabled' if status.enabled else 'disabled' if status.installed else 'not installed'}"
    )
    if status.installed:
        print(f"Shell: {status.shell}")
        print(f"Mode: {status.mode}")
        print(f"RC file: {status.rc_path}")
        print(f"Snippet: {status.snippet_path}")
        print(f"Direct launcher: {status.direct_launcher_path}")
    for line in format_notify_service_status(get_notify_service_status()):
        print(line)


def _select_and_attach_window(window_id: str) -> None:
    session_name = tmux_session_name()
    subprocess.run(
        ["tmux", "select-window", "-t", f"{session_name}:{window_id}"],
        check=True,
        timeout=5,
        capture_output=True,
        text=True,
    )
    if os.environ.get("TMUX"):
        subprocess.run(
            ["tmux", "switch-client", "-t", session_name],
            check=True,
        )
        return
    subprocess.run(
        ["tmux", "attach-session", "-t", session_name],
        check=True,
    )


async def _launch_tmux_session(
    *,
    provider: str,
    cwd: str,
    mode: str,
    attach: bool,
    agent_args: str,
) -> tuple[str, str]:
    launch_command = resolve_notify_launch_command(provider)
    success, message, _window_name, window_id = await tmux_manager.create_window(
        work_dir=cwd,
        launch_command=launch_command,
        agent_args=agent_args,
    )
    if not success or not window_id:
        raise click.ClickException(message or f"Failed to launch {provider}")

    session_manager.set_window_provider(window_id, provider, cwd=cwd)
    session_manager.set_notification_mode(window_id, mode)
    await tmux_manager.stamp_pane_title(window_id, provider)

    if attach:
        _select_and_attach_window(window_id)
    return window_id, message


@click.group("notify")
def notify_group() -> None:
    """Codex-first setup, onboarding, and shell integration commands."""


@notify_group.command("install")
@click.option("--provider", default="codex", show_default=True)
@click.option("--shell", "shell_name", type=click.Choice(_SHELLS), default=None)
@click.option("--bot-token", default=None, help="Telegram bot token.")
@click.option(
    "--allowed-users",
    default=None,
    help="Comma-separated Telegram user IDs allowed to control the bot.",
)
@click.option(
    "--group-id",
    default=None,
    help="Optional Telegram group ID to restrict the bot to one group.",
)
@click.option(
    "--non-interactive",
    is_flag=True,
    help="Fail instead of prompting for missing Telegram configuration.",
)
def notify_install_cmd(
    provider: str,
    shell_name: str | None,
    bot_token: str | None,
    allowed_users: str | None,
    group_id: str | None,
    non_interactive: bool,
) -> None:
    """Configure Telegram + shell integration so plain launches default to notify."""
    setup = resolve_telegram_setup(
        bot_token=bot_token,
        allowed_users=allowed_users,
        group_id=group_id,
        non_interactive=non_interactive,
    )
    dotenv_path = persist_telegram_setup(setup)
    status = install_notify_shell(provider=provider, shell=shell_name)
    existing_service = get_notify_service_status()
    if existing_service.running and existing_service.managed:
        disable_notify_service()
    service_status = ensure_notify_service_running()
    print(f"Stored Telegram config in {dotenv_path}.")
    print(
        f"Installed notify shell integration for {provider} ({status.shell}). "
        f"New shells will route `{provider}` through ccgram notify."
    )
    print(
        f"Background service {'running' if service_status.running else 'stopped'}."
    )
    _print_notify_status(provider)


@notify_group.command("status")
@click.option("--provider", default="codex", show_default=True)
def notify_status_cmd(provider: str) -> None:
    """Show notify-shell status."""
    _print_notify_status(provider)


@notify_group.command("disable")
@click.option("--provider", default="codex", show_default=True)
def notify_disable_cmd(provider: str) -> None:
    """Disable shell interception without removing installed artifacts."""
    disable_notify_shell(provider)
    if not any_notify_providers_enabled():
        disable_notify_service()
    print(f"Disabled notify shell integration for {provider}.")
    _print_notify_status(provider)


@notify_group.command("uninstall")
@click.option("--provider", default="codex", show_default=True)
def notify_uninstall_cmd(provider: str) -> None:
    """Remove notify shell integration and direct-launch override."""
    uninstall_notify_shell(provider)
    if not any_notify_providers_enabled():
        uninstall_notify_service()
    print(f"Uninstalled notify shell integration for {provider}.")
    _print_notify_status(provider)


@notify_group.command(
    "launch",
    context_settings={"ignore_unknown_options": True},
)
@click.option("--provider", default="codex", show_default=True)
@click.option(
    "--mode",
    type=click.Choice(_NOTIFY_MODES),
    default="notify",
    show_default=True,
)
@click.option(
    "--cwd",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=Path.cwd,
    show_default="current directory",
)
@click.option("--attach/--no-attach", default=False, show_default=True)
@click.argument("agent_args", nargs=-1, type=click.UNPROCESSED)
def notify_launch_cmd(
    provider: str,
    mode: str,
    cwd: Path,
    attach: bool,
    agent_args: tuple[str, ...],
) -> None:
    """Launch a provider session in notify or interactive mode."""
    resolved_cwd = str(cwd.resolve())
    agent_text = shlex.join(list(agent_args))

    ensure_notify_service_running()

    if mode == "notify":
        if attach:
            raise click.ClickException(
                "Notify mode keeps the native terminal. Use --mode interactive for tmux attach."
            )
        run_native_notify_session(
            provider=provider,
            cwd=resolved_cwd,
            launch_command=resolve_notify_launch_command(provider),
            agent_args=agent_text,
        )
        return

    window_id, message = asyncio.run(
        _launch_tmux_session(
            provider=provider,
            cwd=resolved_cwd,
            mode=mode,
            attach=attach,
            agent_args=agent_text,
        )
    )
    print(f"{message} [{window_id}]")
