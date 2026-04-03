"""Tests for notify background service lifecycle."""

import json
import signal
from pathlib import Path
from unittest.mock import MagicMock


def _service_state_path(config_dir: Path) -> Path:
    return config_dir / "notify-service.json"


class TestNotifyService:
    def test_ensure_service_running_starts_detached_process(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))

        from ccgram.notify_service import ensure_notify_service_running

        process = MagicMock(pid=4321)
        process.poll.return_value = None
        popen = MagicMock(return_value=process)

        monkeypatch.setattr(
            "ccgram.notify_service._resolve_ccgram_command",
            lambda: ["/usr/bin/ccgram", "run"],
        )
        monkeypatch.setattr("ccgram.notify_service.subprocess.Popen", popen)
        reaper = MagicMock()
        monkeypatch.setattr("ccgram.notify_service._spawn_child_reaper", reaper)

        status = ensure_notify_service_running()

        assert status.installed is True
        assert status.enabled is True
        assert status.running is True
        assert status.pid == 4321
        assert Path(status.log_path).exists()
        popen.assert_called_once()
        reaper.assert_called_once_with(process)
        stored = json.loads(_service_state_path(tmp_path).read_text())
        assert stored["enabled"] is True
        assert stored["pid"] == 4321

    def test_get_service_status_reports_dead_pid_as_not_running(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        _service_state_path(tmp_path).write_text(
            json.dumps(
                {
                    "installed": True,
                    "enabled": True,
                    "pid": 4321,
                    "log_path": str(tmp_path / "notify.log"),
                    "command": ["/usr/bin/ccgram", "run"],
                }
            )
        )

        from ccgram.notify_service import get_notify_service_status

        monkeypatch.setattr("ccgram.notify_service._pid_is_running", lambda pid: False)

        status = get_notify_service_status()

        assert status.installed is True
        assert status.enabled is True
        assert status.running is False
        assert status.pid is None

    def test_ensure_service_reuses_existing_foreground_instance(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))

        from ccgram.notify_service import ensure_notify_service_running

        popen = MagicMock()
        monkeypatch.setattr(
            "ccgram.notify_service._resolve_ccgram_command",
            lambda: ["/usr/bin/ccgram", "run"],
        )
        monkeypatch.setattr(
            "ccgram.notify_service.get_running_instance_pid",
            lambda: 9876,
        )
        monkeypatch.setattr("ccgram.notify_service.subprocess.Popen", popen)

        status = ensure_notify_service_running()

        assert status.installed is True
        assert status.enabled is True
        assert status.running is True
        assert status.pid == 9876
        popen.assert_not_called()
        stored = json.loads(_service_state_path(tmp_path).read_text())
        assert stored["pid"] == 9876
        assert stored["managed"] is False

    def test_ensure_service_preserves_managed_state_for_same_pid(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        _service_state_path(tmp_path).write_text(
            json.dumps(
                {
                    "installed": True,
                    "enabled": True,
                    "pid": 9876,
                    "managed": True,
                    "log_path": str(tmp_path / "notify.log"),
                    "command": ["/usr/bin/ccgram", "run"],
                }
            )
        )

        from ccgram.notify_service import ensure_notify_service_running

        popen = MagicMock()
        monkeypatch.setattr(
            "ccgram.notify_service.get_running_instance_pid",
            lambda: 9876,
        )
        monkeypatch.setattr("ccgram.notify_service.subprocess.Popen", popen)
        monkeypatch.setattr("ccgram.notify_service._pid_is_running", lambda pid: True)

        status = ensure_notify_service_running()

        assert status.running is True
        assert status.pid == 9876
        assert status.managed is True
        popen.assert_not_called()
        stored = json.loads(_service_state_path(tmp_path).read_text())
        assert stored["managed"] is True

    def test_disable_service_stops_process_but_preserves_installation(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        _service_state_path(tmp_path).write_text(
            json.dumps(
                {
                    "installed": True,
                    "enabled": True,
                    "pid": 4321,
                    "log_path": str(tmp_path / "notify.log"),
                    "command": ["/usr/bin/ccgram", "run"],
                }
            )
        )

        from ccgram.notify_service import disable_notify_service

        kill = MagicMock()
        monkeypatch.setattr("ccgram.notify_service.os.kill", kill)
        monkeypatch.setattr(
            "ccgram.notify_service._wait_for_pid_exit", lambda pid, timeout=5.0: True
        )
        monkeypatch.setattr("ccgram.notify_service._pid_is_running", lambda pid: True)

        status = disable_notify_service()

        kill.assert_called_once_with(4321, signal.SIGTERM)
        assert status.installed is True
        assert status.enabled is False
        assert status.running is False
        stored = json.loads(_service_state_path(tmp_path).read_text())
        assert stored["enabled"] is False
        assert stored["pid"] is None

    def test_disable_service_does_not_kill_unmanaged_instance(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        _service_state_path(tmp_path).write_text(
            json.dumps(
                {
                    "installed": True,
                    "enabled": True,
                    "pid": 9876,
                    "managed": False,
                    "log_path": str(tmp_path / "notify.log"),
                    "command": ["/usr/bin/ccgram", "run"],
                }
            )
        )

        from ccgram.notify_service import disable_notify_service

        kill = MagicMock()
        monkeypatch.setattr("ccgram.notify_service.os.kill", kill)
        monkeypatch.setattr("ccgram.notify_service._pid_is_running", lambda pid: True)

        status = disable_notify_service()

        kill.assert_not_called()
        assert status.installed is True
        assert status.enabled is False
        assert status.running is False
        stored = json.loads(_service_state_path(tmp_path).read_text())
        assert stored["pid"] is None

    def test_uninstall_service_removes_state_file(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        _service_state_path(tmp_path).write_text(
            json.dumps(
                {
                    "installed": True,
                    "enabled": False,
                    "pid": None,
                    "log_path": str(tmp_path / "notify.log"),
                    "command": ["/usr/bin/ccgram", "run"],
                }
            )
        )

        from ccgram.notify_service import uninstall_notify_service

        status = uninstall_notify_service()

        assert status.installed is False
        assert status.enabled is False
        assert status.running is False
        assert not _service_state_path(tmp_path).exists()
