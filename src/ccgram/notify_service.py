"""Managed background service helpers for local notify installs."""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .utils import atomic_write_json, ccgram_dir

_STATE_FILE = "notify-service.json"
_LOG_DIR = "logs"
_LOG_FILE = "notify-service.log"


@dataclass
class NotifyServiceStatus:
    installed: bool
    enabled: bool
    running: bool
    pid: int | None
    log_path: str
    command: list[str]


def _state_path() -> Path:
    return ccgram_dir() / _STATE_FILE


def _default_log_path() -> Path:
    return ccgram_dir() / _LOG_DIR / _LOG_FILE


def _load_state() -> dict[str, object]:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError, OSError:
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_state(data: dict[str, object]) -> None:
    atomic_write_json(_state_path(), data)


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _wait_for_pid_exit(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_is_running(pid):
            return True
        time.sleep(0.1)
    return not _pid_is_running(pid)


def _resolve_ccgram_command() -> list[str]:
    installed = shutil.which("ccgram")
    if installed:
        return [installed, "run"]

    argv0 = Path(sys.argv[0]).expanduser()
    if argv0.exists() and argv0.name.startswith("ccgram"):
        return [str(argv0.resolve()), "run"]

    if sys.executable:
        return [sys.executable, "-m", "ccgram", "run"]

    raise RuntimeError("Unable to resolve the ccgram executable for background start")


def get_notify_service_status() -> NotifyServiceStatus:
    raw = _load_state()
    installed = bool(raw.get("installed", False))
    enabled = bool(raw.get("enabled", False))
    pid_raw = raw.get("pid")
    pid = pid_raw if isinstance(pid_raw, int) and pid_raw > 0 else None
    running = pid is not None and _pid_is_running(pid)
    command = raw.get("command")
    log_path = str(raw.get("log_path", "")) or str(_default_log_path())
    return NotifyServiceStatus(
        installed=installed,
        enabled=enabled,
        running=running,
        pid=pid if running else None,
        log_path=log_path,
        command=list(command) if isinstance(command, list) else [],
    )


def ensure_notify_service_running() -> NotifyServiceStatus:
    status = get_notify_service_status()
    command = status.command or _resolve_ccgram_command()
    log_path = Path(status.log_path or str(_default_log_path()))
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if status.running and status.pid is not None:
        _save_state(
            {
                "installed": True,
                "enabled": True,
                "pid": status.pid,
                "log_path": str(log_path),
                "command": command,
            }
        )
        return NotifyServiceStatus(
            installed=True,
            enabled=True,
            running=True,
            pid=status.pid,
            log_path=str(log_path),
            command=command,
        )

    env = os.environ.copy()
    env.setdefault("CCGRAM_DIR", str(ccgram_dir()))

    with open(log_path, "ab") as log_f:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            close_fds=True,
        )

    if process.poll() is not None:
        raise RuntimeError("Failed to start the ccgram background service")

    _save_state(
        {
            "installed": True,
            "enabled": True,
            "pid": process.pid,
            "log_path": str(log_path),
            "command": command,
        }
    )
    return NotifyServiceStatus(
        installed=True,
        enabled=True,
        running=True,
        pid=process.pid,
        log_path=str(log_path),
        command=command,
    )


def disable_notify_service() -> NotifyServiceStatus:
    raw = _load_state()
    status = get_notify_service_status()
    if status.running and status.pid is not None:
        with contextlib.suppress(OSError):
            os.kill(status.pid, signal.SIGTERM)
        if not _wait_for_pid_exit(status.pid):
            with contextlib.suppress(OSError):
                os.kill(status.pid, signal.SIGKILL)
            _wait_for_pid_exit(status.pid, timeout=1.0)

    raw.update(
        {
            "installed": bool(raw.get("installed", False)),
            "enabled": False,
            "pid": None,
            "log_path": raw.get("log_path", str(_default_log_path())),
            "command": raw.get("command", []),
        }
    )
    _save_state(raw)
    return get_notify_service_status()


def uninstall_notify_service() -> NotifyServiceStatus:
    status = disable_notify_service()
    with contextlib.suppress(OSError):
        _state_path().unlink()
    return NotifyServiceStatus(
        installed=False,
        enabled=False,
        running=False,
        pid=None,
        log_path=status.log_path,
        command=status.command,
    )


def format_notify_service_status(status: NotifyServiceStatus) -> list[str]:
    state = "running" if status.running else "stopped" if status.installed else "not installed"
    lines = [f"Service: {state}"]
    if status.pid is not None:
        lines.append(f"PID: {status.pid}")
    if status.log_path:
        lines.append(f"Log: {status.log_path}")
    if status.command:
        lines.append(f"Command: {shlex.join(status.command)}")
    return lines
