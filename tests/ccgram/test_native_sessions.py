"""Tests for native notify-session registry and control helpers."""

from __future__ import annotations

import fcntl
import json
import signal
import threading
from collections import deque
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from ccgram.native_sessions import (
    NATIVE_WINDOW_PREFIX,
    _MirrorState,
    _apply_pending_resize,
    find_untracked_direct_launcher_process_groups,
    _make_resize_handler,
    _mirror_native_session,
    reap_untracked_direct_launcher_process_groups,
    capture_native_pane,
    kill_native_session,
    list_native_windows,
    find_orphaned_native_sessions,
    mark_native_session_exited,
    register_native_session,
    send_native_keys,
    update_native_snapshot,
)
from ccgram.screen_buffer import ScreenBuffer


def test_list_native_windows_includes_running_session(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))

    window_id = f"{NATIVE_WINDOW_PREFIX}abc123"
    snapshot_path = tmp_path / "native" / "abc123" / "snapshot.json"
    control_path = tmp_path / "native" / "abc123" / "control.sock"
    register_native_session(
        window_id=window_id,
        window_name="proj",
        cwd=str(tmp_path),
        provider_name="codex",
        pane_current_command="codex",
        pane_tty="/dev/pts/9",
        snapshot_path=snapshot_path,
        control_socket_path=control_path,
        columns=120,
        rows=40,
        pid=1234,
    )

    with (
        patch("ccgram.native_sessions._pid_is_running", return_value=True),
        patch("ccgram.native_sessions._has_live_control_channel", return_value=True),
    ):
        windows = list_native_windows()

    assert len(windows) == 1
    window = windows[0]
    assert window.window_id == window_id
    assert window.window_name == "proj"
    assert window.cwd == str(tmp_path)
    assert window.pane_current_command == "codex"
    assert window.pane_tty == "/dev/pts/9"
    assert window.pane_width == 120
    assert window.pane_height == 40


def test_capture_native_pane_reads_rendered_snapshot(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))

    window_id = f"{NATIVE_WINDOW_PREFIX}pane1"
    snapshot_path = tmp_path / "native" / "pane1" / "snapshot.json"
    control_path = tmp_path / "native" / "pane1" / "control.sock"
    register_native_session(
        window_id=window_id,
        window_name="proj",
        cwd=str(tmp_path),
        provider_name="codex",
        pane_current_command="codex",
        pane_tty="",
        snapshot_path=snapshot_path,
        control_socket_path=control_path,
        columns=100,
        rows=30,
        pid=999,
    )
    update_native_snapshot(
        window_id,
        rendered_text="Do you trust this directory?\n1. Yes\n2. No",
        raw_text="\x1b[1mDo you trust this directory?\x1b[0m\r\n1. Yes\r\n2. No",
        columns=100,
        rows=30,
    )

    assert capture_native_pane(window_id) == "Do you trust this directory?\n1. Yes\n2. No"
    assert capture_native_pane(window_id, with_ansi=True) == (
        "\x1b[1mDo you trust this directory?\x1b[0m\r\n1. Yes\r\n2. No"
    )


def test_update_native_snapshot_skips_registry_write_when_dimensions_unchanged(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))

    window_id = f"{NATIVE_WINDOW_PREFIX}snapshot1"
    snapshot_path = tmp_path / "native" / "snapshot1" / "snapshot.json"
    control_path = tmp_path / "native" / "snapshot1" / "control.sock"
    register_native_session(
        window_id=window_id,
        window_name="proj",
        cwd=str(tmp_path),
        provider_name="codex",
        pane_current_command="codex",
        pane_tty="",
        snapshot_path=snapshot_path,
        control_socket_path=control_path,
        columns=100,
        rows=30,
        pid=999,
    )

    with patch("ccgram.native_sessions._update_record") as update_record:
        update_native_snapshot(
            window_id,
            rendered_text="rendered",
            raw_text="raw",
            columns=100,
            rows=30,
        )

    update_record.assert_not_called()
    payload = json.loads(snapshot_path.read_text())
    assert payload["rendered_text"] == "rendered"
    assert payload["raw_text"] == "raw"


def test_update_native_snapshot_updates_registry_on_dimension_change(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))

    window_id = f"{NATIVE_WINDOW_PREFIX}snapshot2"
    snapshot_path = tmp_path / "native" / "snapshot2" / "snapshot.json"
    control_path = tmp_path / "native" / "snapshot2" / "control.sock"
    register_native_session(
        window_id=window_id,
        window_name="proj",
        cwd=str(tmp_path),
        provider_name="codex",
        pane_current_command="codex",
        pane_tty="",
        snapshot_path=snapshot_path,
        control_socket_path=control_path,
        columns=100,
        rows=30,
        pid=999,
    )

    with patch("ccgram.native_sessions._update_record") as update_record:
        update_native_snapshot(
            window_id,
            rendered_text="rendered",
            raw_text="raw",
            columns=120,
            rows=33,
        )

    update_record.assert_called_once_with(window_id, columns=120, rows=33)


def test_send_native_keys_writes_json_command_to_socket(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))

    window_id = f"{NATIVE_WINDOW_PREFIX}sock1"
    snapshot_path = tmp_path / "native" / "sock1" / "snapshot.json"
    control_path = tmp_path / "native" / "sock1" / "control.sock"
    register_native_session(
        window_id=window_id,
        window_name="proj",
        cwd=str(tmp_path),
        provider_name="codex",
        pane_current_command="codex",
        pane_tty="",
        snapshot_path=snapshot_path,
        control_socket_path=control_path,
        columns=100,
        rows=30,
        pid=999,
    )

    client = MagicMock()
    socket_cm = MagicMock()
    socket_cm.__enter__.return_value = client

    with patch("ccgram.native_sessions.socket.socket", return_value=socket_cm):
        assert send_native_keys(window_id, "Enter", enter=False, literal=False) is True

    client.connect.assert_called_once_with(str(control_path))
    client.sendall.assert_called_once_with(
        json.dumps(
            {"chars": "Enter", "enter": False, "literal": False}
        ).encode("utf-8")
    )


def test_kill_native_session_uses_term_only_when_process_group_exits(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))

    window_id = f"{NATIVE_WINDOW_PREFIX}kill1"
    snapshot_path = tmp_path / "native" / "kill1" / "snapshot.json"
    control_path = tmp_path / "native" / "kill1" / "control.sock"
    register_native_session(
        window_id=window_id,
        window_name="proj",
        cwd=str(tmp_path),
        provider_name="codex",
        pane_current_command="codex",
        pane_tty="/dev/pts/9",
        snapshot_path=snapshot_path,
        control_socket_path=control_path,
        columns=120,
        rows=40,
        pid=1234,
        process_group_id=777,
        launcher_tty="/dev/pts/5",
    )

    with (
        patch("ccgram.native_sessions.os.killpg") as mock_killpg,
        patch("ccgram.native_sessions._wait_for_process_group_exit", return_value=True),
    ):
        assert kill_native_session(window_id) is True

    assert mock_killpg.call_args_list == [call(777, signal.SIGTERM)]


def test_kill_native_session_escalates_from_term_to_kill_when_process_group_survives(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))

    window_id = f"{NATIVE_WINDOW_PREFIX}kill2"
    snapshot_path = tmp_path / "native" / "kill2" / "snapshot.json"
    control_path = tmp_path / "native" / "kill2" / "control.sock"
    register_native_session(
        window_id=window_id,
        window_name="proj",
        cwd=str(tmp_path),
        provider_name="codex",
        pane_current_command="codex",
        pane_tty="/dev/pts/9",
        snapshot_path=snapshot_path,
        control_socket_path=control_path,
        columns=120,
        rows=40,
        pid=1234,
        process_group_id=777,
        launcher_tty="/dev/pts/5",
    )

    with (
        patch("ccgram.native_sessions.os.killpg") as mock_killpg,
        patch(
            "ccgram.native_sessions._wait_for_process_group_exit",
            side_effect=[False, True],
        ),
    ):
        assert kill_native_session(window_id) is True

    assert mock_killpg.call_args_list == [
        call(777, signal.SIGTERM),
        call(777, signal.SIGKILL),
    ]


def test_kill_native_session_falls_back_to_pid_when_killpg_fails(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))

    window_id = f"{NATIVE_WINDOW_PREFIX}kill3"
    snapshot_path = tmp_path / "native" / "kill3" / "snapshot.json"
    control_path = tmp_path / "native" / "kill3" / "control.sock"
    register_native_session(
        window_id=window_id,
        window_name="proj",
        cwd=str(tmp_path),
        provider_name="codex",
        pane_current_command="codex",
        pane_tty="/dev/pts/9",
        snapshot_path=snapshot_path,
        control_socket_path=control_path,
        columns=120,
        rows=40,
        pid=1234,
        process_group_id=777,
        launcher_tty="/dev/pts/5",
    )

    with (
        patch("ccgram.native_sessions.os.killpg", side_effect=OSError),
        patch("ccgram.native_sessions.os.kill") as mock_kill,
        patch("ccgram.native_sessions._wait_for_process_group_exit", return_value=True),
    ):
        assert kill_native_session(window_id) is True

    mock_kill.assert_called_once_with(1234, signal.SIGTERM)


def test_list_native_windows_hides_old_exited_sessions(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))

    window_id = f"{NATIVE_WINDOW_PREFIX}done1"
    snapshot_path = tmp_path / "native" / "done1" / "snapshot.json"
    control_path = tmp_path / "native" / "done1" / "control.sock"
    register_native_session(
        window_id=window_id,
        window_name="proj",
        cwd=str(tmp_path),
        provider_name="codex",
        pane_current_command="codex",
        pane_tty="",
        snapshot_path=snapshot_path,
        control_socket_path=control_path,
        columns=100,
        rows=30,
        pid=999,
    )
    mark_native_session_exited(window_id, exit_code=0, ended_at=0.0)

    assert list_native_windows(now=600.0) == []


def test_list_native_windows_hides_recent_exited_sessions_from_live_view(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))

    window_id = f"{NATIVE_WINDOW_PREFIX}done-now"
    snapshot_path = tmp_path / "native" / "done-now" / "snapshot.json"
    control_path = tmp_path / "native" / "done-now" / "control.sock"
    register_native_session(
        window_id=window_id,
        window_name="proj",
        cwd=str(tmp_path),
        provider_name="codex",
        pane_current_command="codex",
        pane_tty="",
        snapshot_path=snapshot_path,
        control_socket_path=control_path,
        columns=100,
        rows=30,
        pid=999,
    )
    mark_native_session_exited(window_id, exit_code=0, ended_at=10.0)

    assert list_native_windows(now=11.0) == []


def test_list_native_windows_marks_missing_pid_exited(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))

    window_id = f"{NATIVE_WINDOW_PREFIX}gone1"
    snapshot_path = tmp_path / "native" / "gone1" / "snapshot.json"
    control_path = tmp_path / "native" / "gone1" / "control.sock"
    register_native_session(
        window_id=window_id,
        window_name="proj",
        cwd=str(tmp_path),
        provider_name="codex",
        pane_current_command="codex",
        pane_tty="",
        snapshot_path=snapshot_path,
        control_socket_path=control_path,
        columns=100,
        rows=30,
        pid=999,
    )

    with patch("ccgram.native_sessions.os.kill", side_effect=OSError):
        assert list_native_windows(now=50.0) == []

    registry = json.loads((tmp_path / "native-sessions.json").read_text())
    record = registry["sessions"][window_id]
    assert record["running"] is False
    assert record["ended_at"] == 50.0


def test_list_native_windows_marks_missing_control_socket_exited_after_grace(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))

    window_id = f"{NATIVE_WINDOW_PREFIX}gone-sock"
    snapshot_path = tmp_path / "native" / "gone-sock" / "snapshot.json"
    control_path = tmp_path / "native" / "gone-sock" / "control.sock"
    register_native_session(
        window_id=window_id,
        window_name="proj",
        cwd=str(tmp_path),
        provider_name="codex",
        pane_current_command="codex",
        pane_tty="",
        snapshot_path=snapshot_path,
        control_socket_path=control_path,
        columns=100,
        rows=30,
        pid=999,
    )
    registry_path = tmp_path / "native-sessions.json"
    registry = json.loads(registry_path.read_text())
    registry["sessions"][window_id]["started_at"] = 0.0
    registry_path.write_text(json.dumps(registry))

    with patch("ccgram.native_sessions._pid_is_running", return_value=True):
        assert list_native_windows(now=10.0) == []

    registry = json.loads((tmp_path / "native-sessions.json").read_text())
    record = registry["sessions"][window_id]
    assert record["running"] is False
    assert record["ended_at"] == 10.0


def test_list_native_windows_keeps_recent_running_session_without_socket(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))

    window_id = f"{NATIVE_WINDOW_PREFIX}startup-sock"
    snapshot_path = tmp_path / "native" / "startup-sock" / "snapshot.json"
    control_path = tmp_path / "native" / "startup-sock" / "control.sock"
    register_native_session(
        window_id=window_id,
        window_name="proj",
        cwd=str(tmp_path),
        provider_name="codex",
        pane_current_command="codex",
        pane_tty="",
        snapshot_path=snapshot_path,
        control_socket_path=control_path,
        columns=100,
        rows=30,
        pid=999,
    )
    registry_path = tmp_path / "native-sessions.json"
    registry = json.loads(registry_path.read_text())
    registry["sessions"][window_id]["started_at"] = 0.0
    registry_path.write_text(json.dumps(registry))

    with patch("ccgram.native_sessions._pid_is_running", return_value=True):
        windows = list_native_windows(now=1.0)

    assert [window.window_id for window in windows] == [window_id]


def test_list_native_windows_marks_stale_control_socket_exited(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))

    window_id = f"{NATIVE_WINDOW_PREFIX}stale-sock"
    snapshot_path = tmp_path / "native" / "stale-sock" / "snapshot.json"
    control_path = tmp_path / "native" / "stale-sock" / "control.sock"
    control_path.parent.mkdir(parents=True, exist_ok=True)
    control_path.touch()
    register_native_session(
        window_id=window_id,
        window_name="proj",
        cwd=str(tmp_path),
        provider_name="codex",
        pane_current_command="codex",
        pane_tty="",
        snapshot_path=snapshot_path,
        control_socket_path=control_path,
        columns=100,
        rows=30,
        pid=999,
    )
    registry_path = tmp_path / "native-sessions.json"
    registry = json.loads(registry_path.read_text())
    registry["sessions"][window_id]["started_at"] = 0.0
    registry_path.write_text(json.dumps(registry))

    with (
        patch("ccgram.native_sessions._pid_is_running", return_value=True),
        patch(
            "ccgram.native_sessions._control_socket_accepts_connections",
            return_value=False,
        ),
        patch("ccgram.native_sessions.os.killpg") as killpg,
    ):
        assert list_native_windows(now=10.0) == []

    registry = json.loads((tmp_path / "native-sessions.json").read_text())
    record = registry["sessions"][window_id]
    assert record["running"] is False
    killpg.assert_called_once_with(999, signal.SIGKILL)


def test_list_native_windows_marks_missing_bridge_process_exited(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))

    window_id = f"{NATIVE_WINDOW_PREFIX}bridge-gone"
    snapshot_path = tmp_path / "native" / "bridge-gone" / "snapshot.json"
    control_path = tmp_path / "native" / "bridge-gone" / "control.sock"
    register_native_session(
        window_id=window_id,
        window_name="proj",
        cwd=str(tmp_path),
        provider_name="codex",
        pane_current_command="codex",
        pane_tty="",
        snapshot_path=snapshot_path,
        control_socket_path=control_path,
        columns=100,
        rows=30,
        pid=999,
    )

    def _pid_side_effect(pid: object) -> bool:
        return pid == 999

    with (
        patch("ccgram.native_sessions._pid_is_running", side_effect=_pid_side_effect),
        patch("ccgram.native_sessions._has_live_control_channel", return_value=True),
        patch("ccgram.native_sessions.os.killpg") as killpg,
    ):
        assert list_native_windows(now=10.0) == []

    registry = json.loads((tmp_path / "native-sessions.json").read_text())
    record = registry["sessions"][window_id]
    assert record["running"] is False
    killpg.assert_called_once_with(999, signal.SIGKILL)


def test_register_native_session_records_launcher_tty(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))

    window_id = f"{NATIVE_WINDOW_PREFIX}tty1"
    snapshot_path = tmp_path / "native" / "tty1" / "snapshot.json"
    control_path = tmp_path / "native" / "tty1" / "control.sock"
    register_native_session(
        window_id=window_id,
        window_name="proj",
        cwd=str(tmp_path),
        provider_name="codex",
        pane_current_command="codex",
        pane_tty="/dev/pts/9",
        snapshot_path=snapshot_path,
        control_socket_path=control_path,
        columns=120,
        rows=40,
        pid=1234,
        process_group_id=1234,
        launcher_tty="/dev/pts/5",
    )

    registry = json.loads((tmp_path / "native-sessions.json").read_text())
    assert registry["sessions"][window_id]["launcher_tty"] == "/dev/pts/5"
    assert registry["sessions"][window_id]["orphaned_at"] is None


def test_find_untracked_direct_launcher_process_groups_skips_registered_groups(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
    launcher_path = tmp_path / "bin" / "codex-direct"
    notify_state = {
        "providers": {
            "codex": {"direct_launcher_path": str(launcher_path)},
        }
    }
    (tmp_path / "notify-state.json").write_text(json.dumps(notify_state))

    window_id = f"{NATIVE_WINDOW_PREFIX}tracked-direct"
    snapshot_path = tmp_path / "native" / "tracked-direct" / "snapshot.json"
    control_path = tmp_path / "native" / "tracked-direct" / "control.sock"
    register_native_session(
        window_id=window_id,
        window_name="proj",
        cwd=str(tmp_path),
        provider_name="codex",
        pane_current_command="codex",
        pane_tty="",
        snapshot_path=snapshot_path,
        control_socket_path=control_path,
        columns=100,
        rows=30,
        pid=999,
        process_group_id=200,
    )

    ps_output = "\n".join(
        [
            f"101 99 200 ? /bin/sh -c {launcher_path}",
            f"102 1 300 ? /bin/sh -c {launcher_path}",
            f"103 1 400 pts/4 /bin/sh -c {launcher_path}",
            f"104 42 500 ? /bin/sh -c {launcher_path}",
            "105 1 600 ? /bin/sh -c /tmp/bin/other-direct",
        ]
    )
    proc = MagicMock(stdout=ps_output)

    with patch("ccgram.native_sessions.subprocess.run", return_value=proc):
        orphans = find_untracked_direct_launcher_process_groups()

    assert [orphan.process_group_id for orphan in orphans] == [300]
    assert str(launcher_path) in orphans[0].command


def test_reap_untracked_direct_launcher_process_groups_kills_each_group() -> None:
    orphan_a = MagicMock(process_group_id=300, command="a")
    orphan_b = MagicMock(process_group_id=400, command="b")

    with (
        patch(
            "ccgram.native_sessions.find_untracked_direct_launcher_process_groups",
            return_value=[orphan_a, orphan_b],
        ),
        patch("ccgram.native_sessions.os.killpg") as killpg,
    ):
        reaped = reap_untracked_direct_launcher_process_groups()

    assert reaped == [orphan_a, orphan_b]
    assert killpg.call_args_list == [
        call(300, signal.SIGKILL),
        call(400, signal.SIGKILL),
    ]


def test_find_orphaned_native_sessions_requires_grace_period(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))

    window_id = f"{NATIVE_WINDOW_PREFIX}orphan1"
    snapshot_path = tmp_path / "native" / "orphan1" / "snapshot.json"
    control_path = tmp_path / "native" / "orphan1" / "control.sock"
    register_native_session(
        window_id=window_id,
        window_name="proj",
        cwd=str(tmp_path),
        provider_name="codex",
        pane_current_command="codex",
        pane_tty="/dev/pts/9",
        snapshot_path=snapshot_path,
        control_socket_path=control_path,
        columns=120,
        rows=40,
        pid=1234,
        process_group_id=1234,
        launcher_tty="/dev/pts/5",
    )

    with (
        patch("ccgram.native_sessions._pid_is_running", return_value=True),
        patch("ccgram.native_sessions._has_live_control_channel", return_value=True),
        patch("ccgram.native_sessions._ps_tty_for_pid", return_value="?"),
    ):
        assert find_orphaned_native_sessions(now=100.0, grace_secs=30.0) == []
        orphans = find_orphaned_native_sessions(now=131.0, grace_secs=30.0)

    assert [orphan.window_id for orphan in orphans] == [window_id]

    registry = json.loads((tmp_path / "native-sessions.json").read_text())
    assert registry["sessions"][window_id]["orphaned_at"] == 100.0


def test_find_orphaned_native_sessions_uses_registry_lock(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))

    window_id = f"{NATIVE_WINDOW_PREFIX}orphan-lock"
    snapshot_path = tmp_path / "native" / "orphan-lock" / "snapshot.json"
    control_path = tmp_path / "native" / "orphan-lock" / "control.sock"
    register_native_session(
        window_id=window_id,
        window_name="proj",
        cwd=str(tmp_path),
        provider_name="codex",
        pane_current_command="codex",
        pane_tty="/dev/pts/9",
        snapshot_path=snapshot_path,
        control_socket_path=control_path,
        columns=120,
        rows=40,
        pid=1234,
        process_group_id=1234,
        launcher_tty="/dev/pts/5",
    )

    lock_ops: list[int] = []

    def _record_lock(_handle, op: int) -> None:
        lock_ops.append(op)

    with (
        patch("ccgram.native_sessions._pid_is_running", return_value=True),
        patch("ccgram.native_sessions._has_live_control_channel", return_value=True),
        patch("ccgram.native_sessions._ps_tty_for_pid", return_value="?"),
        patch("ccgram.native_sessions.fcntl.flock", side_effect=_record_lock),
    ):
        assert find_orphaned_native_sessions(now=100.0, grace_secs=30.0) == []

    assert lock_ops == [fcntl.LOCK_EX, fcntl.LOCK_UN]
    registry = json.loads((tmp_path / "native-sessions.json").read_text())
    assert registry["sessions"][window_id]["orphaned_at"] == 100.0


def test_find_orphaned_native_sessions_skips_without_launcher_tty_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))

    window_id = f"{NATIVE_WINDOW_PREFIX}legacy1"
    snapshot_path = tmp_path / "native" / "legacy1" / "snapshot.json"
    control_path = tmp_path / "native" / "legacy1" / "control.sock"
    register_native_session(
        window_id=window_id,
        window_name="proj",
        cwd=str(tmp_path),
        provider_name="codex",
        pane_current_command="codex",
        pane_tty="/dev/pts/9",
        snapshot_path=snapshot_path,
        control_socket_path=control_path,
        columns=120,
        rows=40,
        pid=1234,
        process_group_id=1234,
    )
    registry_path = tmp_path / "native-sessions.json"
    registry = json.loads(registry_path.read_text())
    registry["sessions"][window_id].pop("launcher_tty", None)
    registry_path.write_text(json.dumps(registry))

    with (
        patch("ccgram.native_sessions._pid_is_running", return_value=True),
        patch("ccgram.native_sessions._has_live_control_channel", return_value=True),
        patch("ccgram.native_sessions._ps_tty_for_pid", return_value="pts/99"),
    ):
        assert find_orphaned_native_sessions(now=131.0, grace_secs=30.0) == []

    registry = json.loads(registry_path.read_text())
    assert registry["sessions"][window_id]["orphaned_at"] is None


def test_find_orphaned_native_sessions_skips_on_empty_ps_result(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))

    window_id = f"{NATIVE_WINDOW_PREFIX}psfail1"
    snapshot_path = tmp_path / "native" / "psfail1" / "snapshot.json"
    control_path = tmp_path / "native" / "psfail1" / "control.sock"
    register_native_session(
        window_id=window_id,
        window_name="proj",
        cwd=str(tmp_path),
        provider_name="codex",
        pane_current_command="codex",
        pane_tty="/dev/pts/9",
        snapshot_path=snapshot_path,
        control_socket_path=control_path,
        columns=120,
        rows=40,
        pid=1234,
        process_group_id=1234,
        launcher_tty="/dev/pts/5",
    )

    with (
        patch("ccgram.native_sessions._pid_is_running", return_value=True),
        patch("ccgram.native_sessions._has_live_control_channel", return_value=True),
        patch("ccgram.native_sessions._ps_tty_for_pid", return_value=""),
    ):
        assert find_orphaned_native_sessions(now=131.0, grace_secs=30.0) == []

    registry = json.loads((tmp_path / "native-sessions.json").read_text())
    assert registry["sessions"][window_id]["orphaned_at"] is None


def test_mirror_native_session_exits_when_child_is_dead_and_stdin_is_eof() -> None:
    state = _MirrorState(
        window_id=f"{NATIVE_WINDOW_PREFIX}mirror-eof",
        master_fd=11,
        columns=80,
        rows=24,
        buffer=ScreenBuffer(columns=80, rows=24),
        raw_chunks=deque(),
        write_lock=threading.Lock(),
    )
    child = MagicMock()
    child.poll.return_value = 0

    with (
        patch(
            "ccgram.native_sessions.select.select",
            side_effect=[
                ([10], [], []),
                AssertionError("_mirror_native_session looped after EOF"),
            ],
        ),
        patch("ccgram.native_sessions.os.read", return_value=b""),
    ):
        _mirror_native_session(
            state=state,
            child=child,
            stdin_fd=10,
            stdout_fd=1,
        )


def test_resize_handler_only_marks_pending() -> None:
    state = _MirrorState(
        window_id=f"{NATIVE_WINDOW_PREFIX}resize-flag",
        master_fd=11,
        columns=80,
        rows=24,
        buffer=ScreenBuffer(columns=80, rows=24),
        raw_chunks=deque(["hello"]),
        write_lock=threading.Lock(),
    )

    with (
        patch("ccgram.native_sessions._current_terminal_size") as current_size,
        patch("ccgram.native_sessions._set_winsize") as set_winsize,
        patch("ccgram.native_sessions.update_native_snapshot") as update_snapshot,
    ):
        _make_resize_handler(state)()

    assert state.resize_pending is True
    current_size.assert_not_called()
    set_winsize.assert_not_called()
    update_snapshot.assert_not_called()


def test_apply_pending_resize_rebuilds_snapshot_outside_signal_handler() -> None:
    state = _MirrorState(
        window_id=f"{NATIVE_WINDOW_PREFIX}resize-apply",
        master_fd=11,
        columns=80,
        rows=24,
        buffer=ScreenBuffer(columns=80, rows=24),
        raw_chunks=deque(["hello"]),
        write_lock=threading.Lock(),
        resize_pending=True,
    )
    state.buffer.feed("hello")

    with (
        patch("ccgram.native_sessions._current_terminal_size", return_value=(100, 40)),
        patch("ccgram.native_sessions._set_winsize") as set_winsize,
        patch("ccgram.native_sessions.update_native_snapshot") as update_snapshot,
    ):
        applied = _apply_pending_resize(state)

    assert applied is True
    assert state.resize_pending is False
    assert state.columns == 100
    assert state.rows == 40
    assert state.buffer.rendered_text == "hello"
    set_winsize.assert_called_once_with(11, 100, 40)
    update_snapshot.assert_called_once_with(
        state.window_id,
        rendered_text="hello",
        raw_text="hello",
        columns=100,
        rows=40,
    )
