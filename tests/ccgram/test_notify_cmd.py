"""Tests for ccgram notify commands and shell integration."""

import contextlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner

from ccgram.cli import cli
from ccgram.session import SessionManager


def _notify_state_path(config_dir: Path) -> Path:
    return config_dir / "notify-state.json"


def _notify_snippet_path(config_dir: Path, provider: str, shell: str) -> Path:
    return config_dir / "notify" / f"{provider}.{shell}"


def _direct_launcher_path(config_dir: Path, provider: str) -> Path:
    return config_dir / "bin" / f"{provider}-direct"


def _seed_telegram_env(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("ALLOWED_USERS", "12345")


def _clear_telegram_env(monkeypatch) -> None:
    for key in ("TELEGRAM_BOT_TOKEN", "ALLOWED_USERS", "CCGRAM_GROUP_ID"):
        monkeypatch.delenv(key, raising=False)


class TestNotifyInstall:
    def test_install_prompts_for_missing_telegram_config_and_persists_it(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        runner = CliRunner()
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        _clear_telegram_env(monkeypatch)

        result = runner.invoke(
            cli,
            ["notify", "install", "--provider", "codex", "--shell", "bash"],
            input="123456:bot-token\n12345\n-100999\n",
        )

        assert result.exit_code == 0
        dotenv = (tmp_path / ".env").read_text()
        assert "TELEGRAM_BOT_TOKEN=123456:bot-token" in dotenv
        assert "ALLOWED_USERS=12345" in dotenv
        assert "CCGRAM_GROUP_ID=-100999" in dotenv

    def test_install_allows_optional_group_id_to_be_skipped(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        runner = CliRunner()
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        _clear_telegram_env(monkeypatch)

        result = runner.invoke(
            cli,
            ["notify", "install", "--provider", "codex", "--shell", "bash"],
            input="123456:bot-token\n12345\n\n",
        )

        assert result.exit_code == 0
        dotenv = (tmp_path / ".env").read_text()
        assert "TELEGRAM_BOT_TOKEN=123456:bot-token" in dotenv
        assert "ALLOWED_USERS=12345" in dotenv
        assert "CCGRAM_GROUP_ID=" not in dotenv

    def test_install_non_interactive_fails_when_telegram_config_missing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        runner = CliRunner()
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        _clear_telegram_env(monkeypatch)

        result = runner.invoke(
            cli,
            [
                "notify",
                "install",
                "--provider",
                "codex",
                "--shell",
                "bash",
                "--non-interactive",
            ],
        )

        assert result.exit_code != 0
        assert "TELEGRAM_BOT_TOKEN" in result.output

    def test_install_accepts_explicit_telegram_flags_non_interactively(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        runner = CliRunner()
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        _clear_telegram_env(monkeypatch)

        result = runner.invoke(
            cli,
            [
                "notify",
                "install",
                "--provider",
                "codex",
                "--shell",
                "bash",
                "--non-interactive",
                "--bot-token",
                "123456:bot-token",
                "--allowed-users",
                "12345,67890",
                "--group-id",
                "-100999",
            ],
        )

        assert result.exit_code == 0
        dotenv = (tmp_path / ".env").read_text()
        assert "TELEGRAM_BOT_TOKEN=123456:bot-token" in dotenv
        assert "ALLOWED_USERS=12345,67890" in dotenv
        assert "CCGRAM_GROUP_ID=-100999" in dotenv

    def test_install_writes_shell_snippet_and_state(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        runner = CliRunner()
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed_telegram_env(monkeypatch)

        result = runner.invoke(
            cli, ["notify", "install", "--provider", "codex", "--shell", "bash"]
        )

        assert result.exit_code == 0

        state = json.loads(_notify_state_path(tmp_path).read_text())
        provider_state = state["providers"]["codex"]
        assert provider_state["enabled"] is True
        assert provider_state["shell"] == "bash"
        assert Path(provider_state["rc_path"]) == tmp_path / ".bashrc"

        snippet_path = _notify_snippet_path(tmp_path, "codex", "bash")
        direct_path = _direct_launcher_path(tmp_path, "codex")
        assert snippet_path.exists()
        assert direct_path.exists()

        snippet = snippet_path.read_text()
        assert 'ccgram notify launch --provider codex --attach -- "$@"' in snippet
        assert "codex-direct" in snippet

        rc_text = (tmp_path / ".bashrc").read_text()
        assert ">>> ccgram notify >>>" in rc_text
        assert str(snippet_path) in rc_text

        dotenv = (tmp_path / ".env").read_text()
        assert f"CCGRAM_CODEX_COMMAND={direct_path}" in dotenv

    def test_disable_removes_shell_hook_but_keeps_install_artifacts(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        runner = CliRunner()
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed_telegram_env(monkeypatch)

        install = runner.invoke(
            cli, ["notify", "install", "--provider", "codex", "--shell", "bash"]
        )
        assert install.exit_code == 0

        result = runner.invoke(cli, ["notify", "disable", "--provider", "codex"])

        assert result.exit_code == 0

        state = json.loads(_notify_state_path(tmp_path).read_text())
        provider_state = state["providers"]["codex"]
        assert provider_state["enabled"] is False
        assert _notify_snippet_path(tmp_path, "codex", "bash").exists()
        assert _direct_launcher_path(tmp_path, "codex").exists()
        assert ">>> ccgram notify >>>" not in (tmp_path / ".bashrc").read_text()
        assert "CCGRAM_CODEX_COMMAND=" in (tmp_path / ".env").read_text()

    def test_uninstall_removes_shell_hook_and_direct_override(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        runner = CliRunner()
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed_telegram_env(monkeypatch)

        install = runner.invoke(
            cli, ["notify", "install", "--provider", "codex", "--shell", "bash"]
        )
        assert install.exit_code == 0

        result = runner.invoke(cli, ["notify", "uninstall", "--provider", "codex"])

        assert result.exit_code == 0
        assert not _notify_snippet_path(tmp_path, "codex", "bash").exists()
        assert not _direct_launcher_path(tmp_path, "codex").exists()
        assert ">>> ccgram notify >>>" not in (tmp_path / ".bashrc").read_text()
        dotenv = (tmp_path / ".env").read_text()
        assert "CCGRAM_CODEX_COMMAND=" not in dotenv

    def test_status_reports_enabled_shell_integration(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        runner = CliRunner()
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed_telegram_env(monkeypatch)

        install = runner.invoke(
            cli, ["notify", "install", "--provider", "codex", "--shell", "bash"]
        )
        assert install.exit_code == 0

        result = runner.invoke(cli, ["notify", "status", "--provider", "codex"])

        assert result.exit_code == 0
        assert "Provider: codex" in result.output
        assert "Status: enabled" in result.output
        assert "Shell: bash" in result.output
        assert "Mode: notify" in result.output

    def test_reinstall_with_new_shell_cleans_previous_rc_hook(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        runner = CliRunner()
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed_telegram_env(monkeypatch)

        first = runner.invoke(
            cli, ["notify", "install", "--provider", "codex", "--shell", "bash"]
        )
        assert first.exit_code == 0

        second = runner.invoke(
            cli, ["notify", "install", "--provider", "codex", "--shell", "zsh"]
        )
        assert second.exit_code == 0

        assert ">>> ccgram notify >>>" not in (tmp_path / ".bashrc").read_text()
        assert ">>> ccgram notify >>>" in (tmp_path / ".zshrc").read_text()
        assert not _notify_snippet_path(tmp_path, "codex", "bash").exists()
        assert _notify_snippet_path(tmp_path, "codex", "zsh").exists()

    def test_reinstall_refreshes_direct_command_from_env_override(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        runner = CliRunner()
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed_telegram_env(monkeypatch)

        first = runner.invoke(
            cli, ["notify", "install", "--provider", "codex", "--shell", "bash"]
        )
        assert first.exit_code == 0

        monkeypatch.setenv("CCGRAM_CODEX_COMMAND", "/opt/codex/bin/codex --fast")
        second = runner.invoke(
            cli, ["notify", "install", "--provider", "codex", "--shell", "bash"]
        )
        assert second.exit_code == 0

        direct_launcher = _direct_launcher_path(tmp_path, "codex")
        assert "/opt/codex/bin/codex --fast" in direct_launcher.read_text()

    def test_resolve_notify_launch_command_prefers_installed_direct_launcher(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        runner = CliRunner()
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed_telegram_env(monkeypatch)

        install = runner.invoke(
            cli, ["notify", "install", "--provider", "codex", "--shell", "bash"]
        )
        assert install.exit_code == 0

        from ccgram.notify_shell import resolve_notify_launch_command

        assert resolve_notify_launch_command("codex") == str(
            _direct_launcher_path(tmp_path, "codex")
        )


class TestNotifyLaunch:
    def test_launch_creates_window_and_sets_notify_mode(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        runner = CliRunner()
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))

        create_window = AsyncMock(return_value=(True, "Created window", "proj", "@12"))
        stamp_pane_title = AsyncMock()
        select_window = MagicMock()
        session_manager = MagicMock()

        monkeypatch.setattr(
            "ccgram.notify_cmd.tmux_manager.create_window", create_window
        )
        monkeypatch.setattr(
            "ccgram.notify_cmd.tmux_manager.stamp_pane_title", stamp_pane_title
        )
        monkeypatch.setattr("ccgram.notify_cmd.session_manager", session_manager)
        monkeypatch.setattr(
            "ccgram.notify_cmd.resolve_notify_launch_command",
            lambda provider: "/usr/bin/codex",
        )
        monkeypatch.setattr(
            "ccgram.notify_cmd._select_and_attach_window", select_window
        )

        result = runner.invoke(
            cli,
            [
                "notify",
                "launch",
                "--provider",
                "codex",
                "--cwd",
                str(tmp_path),
                "--attach",
                "--",
                "--model",
                "gpt-5",
            ],
        )

        assert result.exit_code == 0
        create_window.assert_awaited_once()
        kwargs = create_window.await_args.kwargs
        assert kwargs["work_dir"] == str(tmp_path)
        assert kwargs["launch_command"] == "/usr/bin/codex"
        assert kwargs["agent_args"] == "--model gpt-5"
        session_manager.set_window_provider.assert_called_once_with(
            "@12", "codex", cwd=str(tmp_path)
        )
        session_manager.set_notification_mode.assert_called_once_with("@12", "notify")
        stamp_pane_title.assert_awaited_once_with("@12", "codex")
        select_window.assert_called_once_with("@12")

    @pytest.mark.parametrize("provider_name", ["claude", "codex", "gemini", "shell"])
    def test_launch_registers_providers_for_fresh_process(
        self, tmp_path: Path, monkeypatch, provider_name: str
    ) -> None:
        import ccgram.providers as providers_module
        from ccgram.providers.registry import registry

        runner = CliRunner()
        create_window = AsyncMock(
            return_value=(True, "Created window", "example", "@12")
        )
        stamp_pane_title = AsyncMock()

        providers_snapshot = dict(registry._providers)
        instances_snapshot = dict(registry._instances)
        registered_snapshot = providers_module._registered
        registry._providers.clear()
        registry._instances.clear()
        providers_module._registered = False

        monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
        monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
        session_manager = SessionManager()
        monkeypatch.setattr(
            "ccgram.notify_cmd.tmux_manager.create_window", create_window
        )
        monkeypatch.setattr(
            "ccgram.notify_cmd.tmux_manager.stamp_pane_title", stamp_pane_title
        )
        monkeypatch.setattr("ccgram.notify_cmd.session_manager", session_manager)
        monkeypatch.setattr(
            "ccgram.notify_cmd.resolve_notify_launch_command",
            lambda provider: f"/usr/bin/{provider}",
        )

        try:
            result = runner.invoke(
                cli,
                [
                    "notify",
                    "launch",
                    "--provider",
                    provider_name,
                    "--cwd",
                    str(tmp_path),
                ],
            )
        finally:
            registry._providers.clear()
            registry._providers.update(providers_snapshot)
            registry._instances.clear()
            registry._instances.update(instances_snapshot)
            providers_module._registered = registered_snapshot

        assert result.exit_code == 0
        assert session_manager.get_window_state("@12").provider_name == provider_name
        assert session_manager.get_notification_mode("@12") == "notify"
        stamp_pane_title.assert_awaited_once_with("@12", provider_name)

    def test_attach_uses_interactive_tmux_calls(self, monkeypatch) -> None:
        calls = []

        def _run(*args, **kwargs):
            calls.append((args, kwargs))
            return MagicMock()

        monkeypatch.delenv("TMUX", raising=False)
        monkeypatch.setenv("TMUX_SESSION_NAME", "ccgram")
        monkeypatch.setattr("ccgram.notify_cmd.subprocess.run", _run)

        from ccgram.notify_cmd import _select_and_attach_window

        _select_and_attach_window("@7")

        assert calls[0][0][0] == ["tmux", "select-window", "-t", "ccgram:@7"]
        assert calls[0][1]["capture_output"] is True
        assert calls[1][0][0] == ["tmux", "attach-session", "-t", "ccgram"]
        assert "capture_output" not in calls[1][1]
        assert "timeout" not in calls[1][1]


class TestNotifyStatusMain:
    def test_status_main_shows_notify_integration(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
        monkeypatch.setenv("TMUX_SESSION_NAME", "test-session")
        monkeypatch.setattr("ccgram.status_cmd._list_tmux_windows", lambda _: [])

        state = {
            "providers": {
                "codex": {
                    "enabled": True,
                    "shell": "bash",
                    "mode": "notify",
                    "rc_path": str(tmp_path / ".bashrc"),
                }
            }
        }
        _notify_state_path(tmp_path).write_text(json.dumps(state))

        from ccgram.status_cmd import status_main

        with contextlib.suppress(SystemExit):
            status_main()

        captured = capsys.readouterr()
        assert "Notify shell: codex enabled (bash, notify)" in captured.out
