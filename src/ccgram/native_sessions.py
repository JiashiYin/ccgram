"""Native local-session support for notify-mode launches.

Runs provider CLIs in the user's current terminal while exposing enough state
for the background bridge to detect blockers and inject responses from Telegram.
"""

from __future__ import annotations

import contextlib
import json
import os
import pty
import select
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
import tty
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from .screen_buffer import ScreenBuffer
from .utils import atomic_write_json, ccgram_dir

logger = structlog.get_logger()

NATIVE_WINDOW_PREFIX = "native:"
_REGISTRY_FILE = "native-sessions.json"
_RETENTION_SECS = 300.0
_RAW_HISTORY_LIMIT = 200_000
_CONTROL_SOCKET_GRACE_SECS = 5.0


@dataclass
class NativeWindow:
    """Window-like view for a native session."""

    window_id: str
    window_name: str
    cwd: str
    pane_current_command: str = ""
    pane_tty: str = ""
    pane_width: int = 0
    pane_height: int = 0


@dataclass
class _MirrorState:
    window_id: str
    master_fd: int
    columns: int
    rows: int
    buffer: ScreenBuffer
    raw_chunks: deque[str]
    write_lock: threading.Lock


def is_native_window(window_id: str) -> bool:
    return window_id.startswith(NATIVE_WINDOW_PREFIX)


def _registry_path() -> Path:
    return ccgram_dir() / _REGISTRY_FILE


def _native_session_dir(session_key: str) -> Path:
    return ccgram_dir() / "native" / session_key


def _load_registry() -> dict[str, Any]:
    path = _registry_path()
    if not path.exists():
        return {"sessions": {}}
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"sessions": {}}
    if not isinstance(raw, dict):
        return {"sessions": {}}
    sessions = raw.get("sessions")
    if not isinstance(sessions, dict):
        raw["sessions"] = {}
    return raw


def _save_registry(data: dict[str, Any]) -> None:
    atomic_write_json(_registry_path(), data)


def _update_record(window_id: str, **changes: object) -> None:
    data = _load_registry()
    sessions = data.setdefault("sessions", {})
    assert isinstance(sessions, dict)
    record = sessions.get(window_id)
    if not isinstance(record, dict):
        return
    record.update(changes)
    sessions[window_id] = record
    _save_registry(data)


def _pid_is_running(pid: object) -> bool:
    """Return whether a recorded native-session PID still exists."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _has_live_control_channel(record: dict[str, Any], *, now: float) -> bool:
    """Return whether a running native session still has a usable control socket.

    Fresh sessions get a short grace window while the control thread binds the
    Unix socket. After that grace period, a missing socket means the session is
    no longer actionable from Telegram and should be treated as dead.
    """
    control_socket = record.get("control_socket_path")
    if not isinstance(control_socket, str) or not control_socket:
        return False
    path = Path(control_socket)
    if path.exists():
        return _control_socket_accepts_connections(path)
    started_at = record.get("started_at")
    return bool(
        isinstance(started_at, (int, float))
        and now - float(started_at) < _CONTROL_SOCKET_GRACE_SECS
    )


def _control_socket_accepts_connections(path: Path) -> bool:
    """Return whether a native-session control socket is accepting clients."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.2)
            client.connect(str(path))
        return True
    except (ConnectionRefusedError, FileNotFoundError, socket.timeout, OSError):
        return False


def _read_record(window_id: str) -> dict[str, Any] | None:
    data = _load_registry()
    sessions = data.get("sessions", {})
    if not isinstance(sessions, dict):
        return None
    record = sessions.get(window_id)
    return record if isinstance(record, dict) else None


def register_native_session(
    *,
    window_id: str,
    window_name: str,
    cwd: str,
    provider_name: str,
    pane_current_command: str,
    pane_tty: str,
    snapshot_path: Path,
    control_socket_path: Path,
    columns: int,
    rows: int,
    pid: int,
    process_group_id: int | None = None,
) -> None:
    """Persist a newly launched native session."""
    data = _load_registry()
    sessions = data.setdefault("sessions", {})
    assert isinstance(sessions, dict)
    sessions[window_id] = {
        "window_id": window_id,
        "window_name": window_name,
        "cwd": cwd,
        "provider_name": provider_name,
        "pane_current_command": pane_current_command,
        "pane_tty": pane_tty,
        "snapshot_path": str(snapshot_path),
        "control_socket_path": str(control_socket_path),
        "columns": columns,
        "rows": rows,
        "pid": pid,
        "process_group_id": process_group_id if process_group_id is not None else pid,
        "bridge_pid": os.getpid(),
        "running": True,
        "started_at": time.time(),
        "ended_at": None,
        "exit_code": None,
    }
    _save_registry(data)


def update_native_snapshot(
    window_id: str,
    *,
    rendered_text: str,
    raw_text: str,
    columns: int,
    rows: int,
) -> None:
    """Persist the latest rendered/raw terminal snapshot for a native session."""
    record = _read_record(window_id)
    if not record:
        return
    path = Path(str(record["snapshot_path"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        path,
        {
            "rendered_text": rendered_text,
            "raw_text": raw_text,
            "columns": columns,
            "rows": rows,
            "updated_at": time.time(),
        },
    )
    _update_record(window_id, columns=columns, rows=rows)


def mark_native_session_exited(
    window_id: str,
    *,
    exit_code: int,
    ended_at: float | None = None,
) -> None:
    """Mark a native session as exited but keep it briefly for bridge discovery."""
    _update_record(
        window_id,
        running=False,
        exit_code=exit_code,
        ended_at=time.time() if ended_at is None else ended_at,
    )


def _kill_native_process_group(record: dict[str, Any]) -> None:
    """Best-effort reap of a stale detached native-session process group."""
    pgid = record.get("process_group_id")
    if not isinstance(pgid, int) or pgid <= 0:
        pid = record.get("pid")
        pgid = pid if isinstance(pid, int) and pid > 0 else None
    if pgid is None:
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        pid = record.get("pid")
        if isinstance(pid, int) and pid > 0:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


def remove_native_session(window_id: str) -> None:
    """Remove a native session from the registry and delete its socket file."""
    data = _load_registry()
    sessions = data.get("sessions", {})
    if not isinstance(sessions, dict):
        return
    record = sessions.pop(window_id, None)
    if isinstance(record, dict):
        control_socket = record.get("control_socket_path")
        if isinstance(control_socket, str) and control_socket:
            with contextlib.suppress(OSError):
                Path(control_socket).unlink(missing_ok=True)
    _save_registry(data)


def list_native_windows(
    *, now: float | None = None, include_exited: bool = False
) -> list[NativeWindow]:
    """Return live native sessions as window-like objects.

    Exited sessions stay in the registry briefly for diagnostics and cleanup,
    but they are hidden from the live window view unless ``include_exited`` is
    explicitly requested.
    """
    ts = time.time() if now is None else now
    data = _load_registry()
    sessions = data.get("sessions", {})
    if not isinstance(sessions, dict):
        return []

    windows: list[NativeWindow] = []
    stale: list[str] = []
    for window_id, record in sessions.items():
        if not isinstance(record, dict):
            continue
        running = bool(record.get("running", False))
        agent_pid = record.get("pid")
        bridge_pid = record.get("bridge_pid")
        if running and (
            not _pid_is_running(agent_pid)
            or (
                bridge_pid is not None
                and not _pid_is_running(bridge_pid)
            )
            or not _has_live_control_channel(record, now=ts)
        ):
            _kill_native_process_group(record)
            mark_native_session_exited(window_id, exit_code=-1, ended_at=ts)
            record["running"] = False
            record["ended_at"] = ts
            record["exit_code"] = -1
            running = False
        ended_at = record.get("ended_at")
        if (
            not running
            and isinstance(ended_at, (int, float))
            and ts - float(ended_at) > _RETENTION_SECS
        ):
            stale.append(window_id)
            continue
        if not running and not include_exited:
            continue

        windows.append(
            NativeWindow(
                window_id=window_id,
                window_name=str(record.get("window_name", "")),
                cwd=str(record.get("cwd", "")),
                pane_current_command=str(record.get("pane_current_command", "")),
                pane_tty=str(record.get("pane_tty", "")),
                pane_width=int(record.get("columns", 0) or 0),
                pane_height=int(record.get("rows", 0) or 0),
            )
        )

    if stale:
        for window_id in stale:
            remove_native_session(window_id)
    return windows


def get_native_window(window_id: str) -> NativeWindow | None:
    for window in list_native_windows():
        if window.window_id == window_id:
            return window
    return None


def _snapshot_payload(window_id: str) -> dict[str, Any] | None:
    record = _read_record(window_id)
    if not record:
        return None
    snapshot_path = record.get("snapshot_path")
    if not isinstance(snapshot_path, str) or not snapshot_path:
        return None
    path = Path(snapshot_path)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return raw if isinstance(raw, dict) else None


def capture_native_pane(window_id: str, *, with_ansi: bool = False) -> str | None:
    """Read the most recent snapshot text for a native session."""
    payload = _snapshot_payload(window_id)
    if not payload:
        return None
    key = "raw_text" if with_ansi else "rendered_text"
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def capture_native_pane_raw(window_id: str) -> tuple[str, int, int] | None:
    """Return raw snapshot text and dimensions for pyte-style consumers."""
    payload = _snapshot_payload(window_id)
    if not payload:
        return None
    raw_text = payload.get("raw_text")
    columns = payload.get("columns", 0)
    rows = payload.get("rows", 0)
    if not isinstance(raw_text, str) or not raw_text:
        return None
    return raw_text, int(columns or 0), int(rows or 0)


def _encode_control_bytes(chars: str, *, enter: bool, literal: bool) -> bytes:
    if literal:
        data = chars.encode("utf-8")
    else:
        special = {
            "Enter": b"\r",
            "Escape": b"\x1b",
            "Tab": b"\t",
            "Space": b" ",
            "Up": b"\x1b[A",
            "Down": b"\x1b[B",
            "Right": b"\x1b[C",
            "Left": b"\x1b[D",
            "BSpace": b"\x7f",
            "C-c": b"\x03",
        }
        data = special.get(chars, chars.encode("utf-8"))
    if enter:
        data += b"\r"
    return data


def send_native_keys(
    window_id: str,
    chars: str,
    *,
    enter: bool,
    literal: bool,
) -> bool:
    """Deliver a keystroke command to a running native session."""
    record = _read_record(window_id)
    if not record:
        return False
    control_socket = record.get("control_socket_path")
    if not isinstance(control_socket, str) or not control_socket:
        return False
    payload = json.dumps(
        {"chars": chars, "enter": enter, "literal": literal}
    ).encode("utf-8")
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2.0)
                client.connect(control_socket)
                client.sendall(payload)
            return True
        except (FileNotFoundError, ConnectionRefusedError):
            time.sleep(0.05)
        except OSError:
            logger.exception("Failed to send keys to native session %s", window_id)
            return False
    logger.warning("Timed out connecting to native session control socket %s", window_id)
    return False


def _current_terminal_size() -> tuple[int, int]:
    try:
        size = shutil.get_terminal_size()
        return max(20, size.columns), max(5, size.lines)
    except OSError:
        return 120, 40


def _set_winsize(fd: int, columns: int, rows: int) -> None:
    try:
        fcntl = __import__("fcntl")
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
    except OSError:
        pass


def _session_window_name(cwd: str, provider_name: str) -> str:
    name = Path(cwd).name.strip()
    return name or provider_name


def _launch_command_text(launch_command: str, agent_args: str) -> str:
    if agent_args:
        return f"{launch_command} {agent_args}"
    return launch_command


def _run_control_server(
    *,
    control_socket_path: Path,
    master_fd: int,
    stop_event: threading.Event,
    write_lock: threading.Lock,
) -> None:
    control_socket_path.parent.mkdir(parents=True, exist_ok=True)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        with contextlib.suppress(OSError):
            control_socket_path.unlink(missing_ok=True)
        server.bind(str(control_socket_path))
        server.listen(1)
        server.settimeout(0.2)
        while not stop_event.is_set():
            try:
                conn, _ = server.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            with conn:
                try:
                    payload = conn.recv(4096)
                    if not payload:
                        continue
                    data = json.loads(payload.decode("utf-8"))
                    chars = str(data.get("chars", ""))
                    enter = bool(data.get("enter", True))
                    literal = bool(data.get("literal", True))
                    encoded = _encode_control_bytes(
                        chars, enter=enter, literal=literal
                    )
                    with write_lock:
                        os.write(master_fd, encoded)
                except (json.JSONDecodeError, OSError, TypeError, ValueError):
                    logger.exception("Failed to handle native control payload")


def _append_raw_text(raw_chunks: deque[str], text: str) -> str:
    raw_chunks.append(text)
    total = sum(len(part) for part in raw_chunks)
    while total > _RAW_HISTORY_LIMIT:
        total -= len(raw_chunks.popleft())
    return "".join(raw_chunks)


def _record_output_chunk(
    state: _MirrorState,
    *,
    stdout_fd: int,
    chunk: bytes,
) -> None:
    os.write(stdout_fd, chunk)
    text = chunk.decode("utf-8", errors="replace")
    raw_text = _append_raw_text(state.raw_chunks, text)
    state.buffer.feed(text)
    update_native_snapshot(
        state.window_id,
        rendered_text=state.buffer.rendered_text,
        raw_text=raw_text,
        columns=state.columns,
        rows=state.rows,
    )


def _make_resize_handler(state: _MirrorState):
    def _refresh_winsize(*_args: object) -> None:
        state.columns, state.rows = _current_terminal_size()
        _set_winsize(state.master_fd, state.columns, state.rows)
        state.buffer = ScreenBuffer(columns=state.columns, rows=state.rows)
        raw_text = "".join(state.raw_chunks)
        if raw_text:
            state.buffer.feed(raw_text)
        update_native_snapshot(
            state.window_id,
            rendered_text=state.buffer.rendered_text,
            raw_text=raw_text,
            columns=state.columns,
            rows=state.rows,
        )

    return _refresh_winsize


def _prepare_terminal(stdin_fd: int | None, resize_handler):
    if stdin_fd is None:
        return None, None
    old_termios = termios.tcgetattr(stdin_fd)
    tty.setraw(stdin_fd)
    old_handler = signal.getsignal(signal.SIGWINCH)
    signal.signal(signal.SIGWINCH, resize_handler)
    return old_termios, old_handler


def _restore_terminal(
    stdin_fd: int | None,
    old_termios: list[Any] | None,
    old_handler,
) -> None:
    if stdin_fd is not None and old_termios is not None:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_termios)
    if old_handler is not None:
        signal.signal(signal.SIGWINCH, old_handler)


def _mirror_native_session(
    *,
    state: _MirrorState,
    child: subprocess.Popen[str],
    stdin_fd: int | None,
    stdout_fd: int,
) -> None:
    while True:
        read_fds = [state.master_fd]
        if stdin_fd is not None:
            read_fds.append(stdin_fd)
        ready, _, _ = select.select(read_fds, [], [], 0.05)

        if state.master_fd in ready:
            with contextlib.suppress(OSError):
                chunk = os.read(state.master_fd, 65536)
                if chunk:
                    _record_output_chunk(state, stdout_fd=stdout_fd, chunk=chunk)

        if stdin_fd is not None and stdin_fd in ready:
            data = os.read(stdin_fd, 4096)
            if data:
                with state.write_lock:
                    os.write(state.master_fd, data)

        if child.poll() is not None and not ready:
            break


def _drain_native_output(
    *,
    state: _MirrorState,
    stdout_fd: int,
) -> None:
    while True:
        try:
            chunk = os.read(state.master_fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        _record_output_chunk(state, stdout_fd=stdout_fd, chunk=chunk)


def _spawn_native_child(
    *,
    cwd: str,
    command_text: str,
    columns: int,
    rows: int,
) -> tuple[subprocess.Popen[str], int, str]:
    master_fd, slave_fd = pty.openpty()
    pane_tty = ""
    try:
        pane_tty = os.ttyname(slave_fd)
    except OSError:
        pane_tty = ""

    _set_winsize(master_fd, columns, rows)
    child = subprocess.Popen(
        command_text,
        shell=True,
        cwd=cwd,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        start_new_session=True,
    )
    os.close(slave_fd)
    return child, master_fd, pane_tty


def run_native_notify_session(
    *,
    provider: str,
    cwd: str,
    launch_command: str,
    agent_args: str,
) -> tuple[str, str]:
    """Run a provider in the current terminal while mirroring state for notify."""
    session_key = uuid.uuid4().hex[:12]
    window_id = f"{NATIVE_WINDOW_PREFIX}{session_key}"
    session_dir = _native_session_dir(session_key)
    session_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = session_dir / "snapshot.json"
    control_socket_path = session_dir / "control.sock"
    columns, rows = _current_terminal_size()
    window_name = _session_window_name(cwd, provider)
    command_text = _launch_command_text(launch_command, agent_args)
    child, master_fd, pane_tty = _spawn_native_child(
        cwd=cwd,
        command_text=command_text,
        columns=columns,
        rows=rows,
    )

    register_native_session(
        window_id=window_id,
        window_name=window_name,
        cwd=cwd,
        provider_name=provider,
        pane_current_command=shlex.split(launch_command)[0],
        pane_tty=pane_tty,
        snapshot_path=snapshot_path,
        control_socket_path=control_socket_path,
        columns=columns,
        rows=rows,
        pid=child.pid,
        process_group_id=child.pid,
    )
    update_native_snapshot(
        window_id,
        rendered_text="",
        raw_text="",
        columns=columns,
        rows=rows,
    )

    stop_event = threading.Event()
    state = _MirrorState(
        window_id=window_id,
        master_fd=master_fd,
        columns=columns,
        rows=rows,
        buffer=ScreenBuffer(columns=columns, rows=rows),
        raw_chunks=deque(),
        write_lock=threading.Lock(),
    )
    control_thread = threading.Thread(
        target=_run_control_server,
        kwargs={
            "control_socket_path": control_socket_path,
            "master_fd": master_fd,
            "stop_event": stop_event,
            "write_lock": state.write_lock,
        },
        daemon=True,
    )
    control_thread.start()

    stdin_fd = sys.stdin.fileno() if sys.stdin.isatty() else None
    stdout_fd = sys.stdout.fileno()
    resize_handler = _make_resize_handler(state)
    old_termios, old_handler = _prepare_terminal(stdin_fd, resize_handler)

    try:
        _mirror_native_session(
            state=state,
            child=child,
            stdin_fd=stdin_fd,
            stdout_fd=stdout_fd,
        )
        _drain_native_output(state=state, stdout_fd=stdout_fd)
    finally:
        stop_event.set()
        control_thread.join(timeout=1.0)
        with contextlib.suppress(OSError):
            control_socket_path.unlink(missing_ok=True)
        os.close(master_fd)
        _restore_terminal(stdin_fd, old_termios, old_handler)

    exit_code = child.wait()
    mark_native_session_exited(window_id, exit_code=exit_code)
    if exit_code != 0:
        raise SystemExit(exit_code)
    return window_id, f"Started native session '{window_name}'"
