"""Tests for native notify-session registry and control helpers."""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

from ccgram.native_sessions import (
    NATIVE_WINDOW_PREFIX,
    capture_native_pane,
    list_native_windows,
    mark_native_session_exited,
    register_native_session,
    send_native_keys,
    update_native_snapshot,
)


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

    received: dict[str, object] = {}

    def _server() -> None:
        control_path.parent.mkdir(parents=True, exist_ok=True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(control_path))
            server.listen(1)
            conn, _ = server.accept()
            with conn:
                payload = conn.recv(4096)
        received.update(json.loads(payload.decode("utf-8")))

    thread = threading.Thread(target=_server, daemon=True)
    thread.start()

    assert send_native_keys(window_id, "Enter", enter=False, literal=False) is True
    thread.join(timeout=3)

    assert received == {"chars": "Enter", "enter": False, "literal": False}


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
