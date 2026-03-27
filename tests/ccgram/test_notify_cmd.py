"""Tests for ccgram notify commands and shell integration."""

import contextlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from click.testing import CliRunner

from ccgram.cli import cli


def _notify_state_path(config_dir: Path) -> Path:
    return config_dir / "notify-state.json"


def _notify_snippet_path(config_dir: Path, provider: str, shell: str) -> Path:
    return config_dir / "notify" / f"{provider}.{shell}"


def _direct_launcher_path(config_dir: Path, provider: str) -> Path:
    return config_dir / "bin" / f"{provider}-direct"


class TestNotifyInstall:
    def test_install_writes_shell_snippet_and_state(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        runner = CliRunner()
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))

        result = runner.invoke(cli, ["notify", "install", "--provider", "codex", "--shell", "bash"])

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


class TestNotifyLaunch:
    def test_launch_creates_window_and_sets_notify_mode(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        runner = CliRunner()
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))

        create_window = AsyncMock(
            return_value=(True, "Created window", "proj", "@12")
        )
        stamp_pane_title = AsyncMock()
        select_window = MagicMock()
        session_manager = MagicMock()

        monkeypatch.setattr("ccgram.notify_cmd.tmux_manager.create_window", create_window)
        monkeypatch.setattr(
            "ccgram.notify_cmd.tmux_manager.stamp_pane_title", stamp_pane_title
        )
        monkeypatch.setattr("ccgram.notify_cmd.session_manager", session_manager)
        monkeypatch.setattr(
            "ccgram.notify_cmd.resolve_launch_command",
            lambda provider, approval_mode="normal": "/usr/bin/codex",
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
        session_manager.set_window_provider.assert_called_once_with("@12", "codex")
        session_manager.set_notification_mode.assert_called_once_with("@12", "notify")
        stamp_pane_title.assert_awaited_once_with("@12", "codex")
        select_window.assert_called_once_with("@12")


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
