"""CLI helpers for ccgram notify setup and launches."""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
from types import SimpleNamespace
from pathlib import Path

import click

from .notify_shell import (
    DEFAULT_NOTIFY_MODE,
    SUPPORTED_SHELLS,
    detect_shell_name,
    disable_notify_integration,
    get_notify_status,
    install_notify_integration,
    uninstall_notify_integration,
)
from .providers import resolve_launch_command
from .utils import tmux_session_name

tmux_manager = SimpleNamespace(create_window=None, stamp_pane_title=None)
session_manager = SimpleNamespace(set_window_provider=None, set_notification_mode=None)


def _ensure_tmux_manager():
    global tmux_manager
    if not hasattr(tmux_manager, "create_window"):
        from .tmux_manager import tmux_manager as live_tmux_manager

        tmux_manager = live_tmux_manager
    return tmux_manager


def _ensure_session_manager():
    global session_manager
    if not hasattr(session_manager, "set_notification_mode"):
        from .session import session_manager as live_session_manager

        session_manager = live_session_manager
    return session_manager


def _select_and_attach_window(window_id: str) -> None:
    session_name = tmux_session_name()
    target = f"{session_name}:{window_id}"
    subprocess.run(["tmux", "select-window", "-t", target], check=False)
    if os.environ.get("TMUX"):
        subprocess.run(["tmux", "switch-client", "-t", session_name], check=False)
    else:
        subprocess.run(["tmux", "attach-session", "-t", session_name], check=False)


async def _launch_window(
    *,
    provider: str,
    cwd: str,
    mode: str,
    agent_args: tuple[str, ...],
    attach: bool,
) -> tuple[bool, str, str, str]:
    tmux = _ensure_tmux_manager()
    sm = _ensure_session_manager()
    launch_command = resolve_launch_command(provider)
    joined_args = shlex.join(agent_args)
    success, message, window_name, window_id = await tmux.create_window(
        work_dir=cwd,
        launch_command=launch_command,
        agent_args=joined_args,
    )
    if not success:
        return success, message, window_name, window_id
    sm.set_window_provider(window_id, provider)
    sm.set_notification_mode(window_id, mode)
    await tmux.stamp_pane_title(window_id, provider)
    if attach:
        _select_and_attach_window(window_id)
    return success, message, window_name, window_id


def notify_install_main(provider: str, shell: str | None, mode: str) -> dict:
    return install_notify_integration(
        provider,
        shell=detect_shell_name(shell),
        mode=mode,
    )


def notify_status_main(provider: str) -> dict:
    return get_notify_status(provider)


def notify_disable_main(provider: str) -> dict:
    return disable_notify_integration(provider)


def notify_uninstall_main(provider: str) -> dict:
    return uninstall_notify_integration(provider)


@click.group("notify")
def notify_group() -> None:
    """Codex-first notify setup and shell integration commands."""


@notify_group.command("install")
@click.option("--provider", default="codex", show_default=True)
@click.option("--shell", type=click.Choice(SUPPORTED_SHELLS), default=None)
@click.option(
    "--mode",
    type=click.Choice(["notify", "interactive"]),
    default=DEFAULT_NOTIFY_MODE,
    show_default=True,
)
def notify_install_cmd(provider: str, shell: str | None, mode: str) -> None:
    status = notify_install_main(provider, shell, mode)
    click.echo(f"Installed notify shell integration for {provider}")
    click.echo(f"Shell: {status['shell']}")
    click.echo(f"Mode: {status['mode']}")
    click.echo(f"RC file: {status['rc_path']}")


@notify_group.command("status")
@click.option("--provider", default="codex", show_default=True)
def notify_status_cmd(provider: str) -> None:
    status = notify_status_main(provider)
    click.echo(f"Provider: {provider}")
    click.echo(f"Status: {'enabled' if status['enabled'] else 'disabled'}")
    click.echo(f"Shell: {status['shell']}")
    click.echo(f"Mode: {status['mode']}")
    click.echo(f"RC file: {status['rc_path']}")


@notify_group.command("disable")
@click.option("--provider", default="codex", show_default=True)
def notify_disable_cmd(provider: str) -> None:
    status = notify_disable_main(provider)
    click.echo(f"Disabled notify shell integration for {provider}")
    click.echo(f"Status: {'enabled' if status['enabled'] else 'disabled'}")


@notify_group.command("uninstall")
@click.option("--provider", default="codex", show_default=True)
def notify_uninstall_cmd(provider: str) -> None:
    notify_uninstall_main(provider)
    click.echo(f"Uninstalled notify shell integration for {provider}")


@notify_group.command("launch")
@click.option("--provider", default="codex", show_default=True)
@click.option("--cwd", type=click.Path(path_type=Path), default=Path.cwd())
@click.option(
    "--mode",
    type=click.Choice(["notify", "interactive"]),
    default=DEFAULT_NOTIFY_MODE,
    show_default=True,
)
@click.option("--attach/--no-attach", default=False, show_default=True)
@click.argument("agent_args", nargs=-1, type=click.UNPROCESSED)
def notify_launch_cmd(
    provider: str,
    cwd: Path,
    mode: str,
    attach: bool,
    agent_args: tuple[str, ...],
) -> None:
    success, message, window_name, window_id = asyncio.run(
        _launch_window(
            provider=provider,
            cwd=str(cwd),
            mode=mode,
            agent_args=agent_args,
            attach=attach,
        )
    )
    if not success:
        raise click.ClickException(message)
    click.echo(message)
    click.echo(f"Window: {window_name} ({window_id})")
