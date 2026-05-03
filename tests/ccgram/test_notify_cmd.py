"""Tests for ccgram notify commands and shell integration."""

import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import subprocess

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


@pytest.fixture(autouse=True)
def _stub_notify_service(monkeypatch, tmp_path: Path) -> None:
    state = {
        "installed": False,
        "enabled": False,
        "running": False,
        "pid": None,
        "managed": False,
        "log_path": str(tmp_path / "notify.log"),
        "command": ["/usr/bin/ccgram", "run"],
    }

    def _status() -> SimpleNamespace:
        return SimpleNamespace(**state)

    def _ensure() -> SimpleNamespace:
        state.update(
            {
                "installed": True,
                "enabled": True,
                "running": True,
                "pid": 4321,
                "managed": True,
            }
        )
        return _status()

    def _disable() -> SimpleNamespace:
        state.update(
            {
                "installed": True,
                "enabled": False,
                "running": False,
                "pid": None,
                "managed": False,
            }
        )
        return _status()

    def _uninstall() -> SimpleNamespace:
        state.update(
            {
                "installed": False,
                "enabled": False,
                "running": False,
                "pid": None,
                "managed": False,
            }
        )
        return _status()

    monkeypatch.setattr(
        "ccgram.notify_cmd.ensure_notify_service_running", _ensure, raising=False
    )
    monkeypatch.setattr(
        "ccgram.notify_cmd.get_notify_service_status", _status, raising=False
    )
    monkeypatch.setattr(
        "ccgram.notify_cmd.disable_notify_service", _disable, raising=False
    )
    monkeypatch.setattr(
        "ccgram.notify_cmd.uninstall_notify_service", _uninstall, raising=False
    )


class TestNotifyInstall:
    def test_install_starts_background_service(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        runner = CliRunner()
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed_telegram_env(monkeypatch)

        ensure_service = MagicMock(
            return_value=SimpleNamespace(
                installed=True,
                enabled=True,
                running=True,
                pid=4321,
                managed=True,
                log_path=str(tmp_path / "notify.log"),
            )
        )
        monkeypatch.setattr(
            "ccgram.notify_cmd.ensure_notify_service_running",
            ensure_service,
            raising=False,
        )

        result = runner.invoke(
            cli, ["notify", "install", "--provider", "codex", "--shell", "bash"]
        )

        assert result.exit_code == 0
        ensure_service.assert_called_once_with()

    def test_install_restarts_existing_managed_service(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        runner = CliRunner()
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed_telegram_env(monkeypatch)

        ensure_service = MagicMock(
            return_value=SimpleNamespace(
                installed=True,
                enabled=True,
                running=True,
                pid=4321,
                managed=True,
                log_path=str(tmp_path / "notify.log"),
                command=["/usr/bin/ccgram", "run"],
            )
        )
        disable_service = MagicMock()
        get_status = MagicMock(
            return_value=SimpleNamespace(
                installed=True,
                enabled=True,
                running=True,
                pid=1111,
                managed=True,
                log_path=str(tmp_path / "notify.log"),
                command=["/usr/bin/ccgram", "run"],
            )
        )
        monkeypatch.setattr(
            "ccgram.notify_cmd.get_notify_service_status",
            get_status,
            raising=False,
        )
        monkeypatch.setattr(
            "ccgram.notify_cmd.disable_notify_service",
            disable_service,
            raising=False,
        )
        monkeypatch.setattr(
            "ccgram.notify_cmd.ensure_notify_service_running",
            ensure_service,
            raising=False,
        )

        result = runner.invoke(
            cli, ["notify", "install", "--provider", "codex", "--shell", "bash"]
        )

        assert result.exit_code == 0
        assert get_status.call_count >= 1
        disable_service.assert_called_once_with()
        ensure_service.assert_called_once_with()

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
        assert 'ccgram notify launch --provider codex --mode notify -- "$@"' in snippet
        assert (
            'ccgram notify launch --provider codex --mode interactive --attach -- "$@"'
            not in snippet
        )
        assert "codex-direct" not in snippet

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
        assert "Direct launcher:" not in result.output

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
        launcher_text = direct_launcher.read_text()
        assert "/opt/codex/bin/codex --fast" in launcher_text
        assert "--ask-for-approval never" in launcher_text
        assert "--sandbox workspace-write" in launcher_text

    def test_install_translates_legacy_full_auto_from_existing_bash_function(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        runner = CliRunner()
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed_telegram_env(monkeypatch)

        function_text = """codex ()
{
    command codex --full-auto \\
        --add-dir /home/jacob/.agents/skills \\
        --add-dir /home/jacob/.codex/rules \\
        \"$@\"
}
"""

        def _run(*_args, **_kwargs):
            return SimpleNamespace(returncode=0, stdout=function_text, stderr="")

        monkeypatch.setattr("ccgram.notify_shell.subprocess.run", _run)
        monkeypatch.setattr(
            "ccgram.notify_shell.shutil.which",
            lambda name: f"/usr/bin/{name}",
        )

        result = runner.invoke(
            cli, ["notify", "install", "--provider", "codex", "--shell", "bash"]
        )

        assert result.exit_code == 0
        direct_launcher = _direct_launcher_path(tmp_path, "codex")
        launcher_text = direct_launcher.read_text()
        assert "/usr/bin/codex" in launcher_text
        assert "--full-auto" not in launcher_text
        assert "--ask-for-approval never" in launcher_text
        assert "--sandbox workspace-write" in launcher_text
        assert "--add-dir /home/jacob/.agents/skills" in launcher_text
        assert "--add-dir /home/jacob/.codex/rules" in launcher_text

    def test_reinstall_refreshes_direct_command_from_bash_function_when_state_is_stale(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        runner = CliRunner()
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed_telegram_env(monkeypatch)

        state = {
            "providers": {
                "codex": {
                    "provider": "codex",
                    "enabled": False,
                    "shell": "bash",
                    "mode": "notify",
                    "rc_path": str(tmp_path / ".bashrc"),
                    "snippet_path": str(_notify_snippet_path(tmp_path, "codex", "bash")),
                    "direct_launcher_path": str(_direct_launcher_path(tmp_path, "codex")),
                    "direct_command": "/usr/bin/codex",
                }
            }
        }
        _notify_state_path(tmp_path).write_text(json.dumps(state))

        function_text = """codex ()
{
    command codex --full-auto \\
        --add-dir /home/jacob/.agents/skills \\
        \"$@\"
}
"""

        def _run(*_args, **_kwargs):
            return SimpleNamespace(returncode=0, stdout=function_text, stderr="")

        monkeypatch.setattr("ccgram.notify_shell.subprocess.run", _run)
        monkeypatch.setattr(
            "ccgram.notify_shell.shutil.which",
            lambda name: f"/usr/bin/{name}",
        )

        result = runner.invoke(
            cli, ["notify", "install", "--provider", "codex", "--shell", "bash"]
        )

        assert result.exit_code == 0
        launcher_text = _direct_launcher_path(tmp_path, "codex").read_text()
        assert "/usr/bin/codex" in launcher_text
        assert "--full-auto" not in launcher_text
        assert "--ask-for-approval never" in launcher_text
        assert "--sandbox workspace-write" in launcher_text

    def test_resolve_notify_launch_command_repairs_stale_codex_direct_launcher(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        runner = CliRunner()
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed_telegram_env(monkeypatch)

        state = {
            "providers": {
                "codex": {
                    "provider": "codex",
                    "enabled": True,
                    "shell": "bash",
                    "mode": "notify",
                    "rc_path": str(tmp_path / ".bashrc"),
                    "snippet_path": str(_notify_snippet_path(tmp_path, "codex", "bash")),
                    "direct_launcher_path": str(_direct_launcher_path(tmp_path, "codex")),
                    "direct_command": "/usr/bin/codex --full-auto --add-dir /home/jacob/.agents/skills",
                }
            }
        }
        _notify_state_path(tmp_path).write_text(json.dumps(state))
        _direct_launcher_path(tmp_path, "codex").parent.mkdir(parents=True, exist_ok=True)
        _direct_launcher_path(tmp_path, "codex").write_text(
            "#!/usr/bin/env bash\nexec /usr/bin/codex --full-auto --add-dir /home/jacob/.agents/skills \"$@\"\n"
        )
        (tmp_path / ".env").write_text(
            f"CCGRAM_CODEX_COMMAND={_direct_launcher_path(tmp_path, 'codex')}\n"
        )

        install = runner.invoke(
            cli, ["notify", "install", "--provider", "codex", "--shell", "bash"]
        )
        assert install.exit_code == 0

        from ccgram.notify_shell import get_notify_status, resolve_notify_launch_command

        command = resolve_notify_launch_command("codex")
        assert command == str(_direct_launcher_path(tmp_path, "codex"))

        launcher_text = _direct_launcher_path(tmp_path, "codex").read_text()
        assert "--full-auto" not in launcher_text
        assert "--ask-for-approval never" in launcher_text
        assert "--sandbox workspace-write" in launcher_text
        assert "--add-dir /home/jacob/.agents/skills" in launcher_text

        status = get_notify_status("codex")
        assert "--full-auto" not in status.direct_command
        assert "--ask-for-approval never" in status.direct_command
        assert "--sandbox workspace-write" in status.direct_command

    def test_install_falls_back_when_bash_function_probe_times_out(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        runner = CliRunner()
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed_telegram_env(monkeypatch)

        def _run(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(["bash", "-ic", "declare -f codex"], 5)

        monkeypatch.setattr("ccgram.notify_shell.subprocess.run", _run)
        monkeypatch.setattr(
            "ccgram.notify_shell.shutil.which",
            lambda name: f"/usr/bin/{name}",
        )

        result = runner.invoke(
            cli, ["notify", "install", "--provider", "codex", "--shell", "bash"]
        )

        assert result.exit_code == 0
        launcher_text = _direct_launcher_path(tmp_path, "codex").read_text()
        assert "exec /usr/bin/codex --ask-for-approval never --sandbox workspace-write \"$@\"" in launcher_text

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

    def test_resolve_notify_launch_command_dangerous_uses_direct_command_not_full_auto(
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

        command = resolve_notify_launch_command("codex", dangerous=True)
        assert str(_direct_launcher_path(tmp_path, "codex")) not in command
        assert "--full-auto" not in command
        assert "--ask-for-approval" not in command
        assert "--sandbox" not in command
        assert "--dangerously-bypass-approvals-and-sandbox" in command


class TestNotifyLaunch:
    def test_launch_ensures_background_service_is_running(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        runner = CliRunner()
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))

        run_native = MagicMock(return_value=("native:1", "Started native session"))
        ensure_service = MagicMock()

        monkeypatch.setattr(
            "ccgram.notify_cmd.run_native_notify_session", run_native
        )
        monkeypatch.setattr(
            "ccgram.notify_cmd.resolve_notify_launch_command",
            lambda provider: "/usr/bin/codex",
        )
        monkeypatch.setattr(
            "ccgram.notify_cmd.ensure_notify_service_running",
            ensure_service,
            raising=False,
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
            ],
        )

        assert result.exit_code == 0
        ensure_service.assert_called_once_with()
        run_native.assert_called_once()

    def test_launch_notify_mode_runs_native_session(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        runner = CliRunner()
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))

        run_native = MagicMock(return_value=("native:1", "Started native session"))
        create_window = AsyncMock()

        monkeypatch.setattr(
            "ccgram.notify_cmd.run_native_notify_session", run_native
        )
        monkeypatch.setattr(
            "ccgram.notify_cmd.tmux_manager.create_window", create_window
        )
        monkeypatch.setattr(
            "ccgram.notify_cmd.resolve_notify_launch_command",
            lambda provider: "/usr/bin/codex",
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
                "--",
                "--model",
                "gpt-5",
            ],
        )

        assert result.exit_code == 0
        run_native.assert_called_once_with(
            provider="codex",
            cwd=str(tmp_path.resolve()),
            launch_command="/usr/bin/codex",
            agent_args="--model gpt-5",
        )
        create_window.assert_not_called()

    def test_launch_notify_mode_uses_dangerous_launch_command(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        runner = CliRunner()
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))

        run_native = MagicMock(return_value=("native:1", "Started native session"))
        resolved_commands: list[tuple[str, str]] = []

        monkeypatch.setattr(
            "ccgram.notify_cmd.run_native_notify_session", run_native
        )
        monkeypatch.setattr(
            "ccgram.notify_cmd.resolve_notify_launch_command",
            lambda provider, dangerous=False: resolved_commands.append(
                (provider, dangerous)
            )
            or "/usr/bin/codex --dangerously-bypass-approvals-and-sandbox",
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
                "--",
                "resume",
                "abc",
                "--dangerously-bypass-approvals-and-sandbox",
            ],
        )

        assert result.exit_code == 0
        assert resolved_commands == [("codex", True)]
        run_native.assert_called_once_with(
            provider="codex",
            cwd=str(tmp_path.resolve()),
            launch_command="/usr/bin/codex --dangerously-bypass-approvals-and-sandbox",
            agent_args="resume abc",
        )

    def test_launch_interactive_mode_creates_tmux_window(
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
                "--mode",
                "interactive",
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
        session_manager.set_notification_mode.assert_called_once_with(
            "@12", "interactive"
        )
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
                    "--mode",
                    "interactive",
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
        assert session_manager.get_notification_mode("@12") == "interactive"
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
    def test_status_reports_background_service(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        runner = CliRunner()
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed_telegram_env(monkeypatch)

        monkeypatch.setattr(
            "ccgram.notify_cmd.get_notify_service_status",
            lambda: SimpleNamespace(
                installed=True,
                enabled=True,
                running=True,
                pid=4321,
                managed=True,
                log_path=str(tmp_path / "notify.log"),
                command=["/usr/bin/ccgram", "run"],
            ),
            raising=False,
        )

        install = runner.invoke(
            cli, ["notify", "install", "--provider", "codex", "--shell", "bash"]
        )
        assert install.exit_code == 0

        result = runner.invoke(cli, ["notify", "status", "--provider", "codex"])

        assert result.exit_code == 0
        assert "Service: running" in result.output
        assert "PID: 4321" in result.output

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


class TestNotifyDisableAndUninstall:
    def test_disable_stops_service_when_no_notify_providers_remain_enabled(
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

        disable_service = MagicMock()
        monkeypatch.setattr(
            "ccgram.notify_cmd.any_notify_providers_enabled",
            lambda: False,
            raising=False,
        )
        monkeypatch.setattr(
            "ccgram.notify_cmd.disable_notify_service",
            disable_service,
            raising=False,
        )

        result = runner.invoke(cli, ["notify", "disable", "--provider", "codex"])

        assert result.exit_code == 0
        disable_service.assert_called_once_with()

    def test_uninstall_stops_service_when_no_notify_providers_remain_enabled(
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

        uninstall_service = MagicMock()
        monkeypatch.setattr(
            "ccgram.notify_cmd.any_notify_providers_enabled",
            lambda: False,
            raising=False,
        )
        monkeypatch.setattr(
            "ccgram.notify_cmd.uninstall_notify_service",
            uninstall_service,
            raising=False,
        )

        result = runner.invoke(cli, ["notify", "uninstall", "--provider", "codex"])

        assert result.exit_code == 0
        uninstall_service.assert_called_once_with()
