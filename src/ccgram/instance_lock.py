"""Process-level singleton lock for one ccgram bot per config directory."""

from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path

from .utils import ccgram_dir

_LOCK_FILE = "bot-instance.lock"
_lock_handle = None


class InstanceAlreadyRunningError(RuntimeError):
    """Raised when another bot instance already owns the config lock."""

    def __init__(self, pid: int | None) -> None:
        self.pid = pid
        detail = f" (pid {pid})" if pid else ""
        super().__init__(f"Another ccgram instance is already running{detail}.")


def _lock_path() -> Path:
    return ccgram_dir() / _LOCK_FILE


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def get_running_instance_pid() -> int | None:
    """Return the live pid recorded in the lock file, if any."""
    path = _lock_path()
    if not path.exists():
        return None
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 and _pid_is_running(pid) else None


def acquire_instance_lock() -> None:
    """Acquire the singleton bot lock for the current config directory."""
    global _lock_handle
    if _lock_handle is not None:
        return

    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")  # noqa: SIM115 - handle stays open for lock lifetime
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise InstanceAlreadyRunningError(get_running_instance_pid()) from exc

    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    os.fsync(handle.fileno())
    _lock_handle = handle


def release_instance_lock() -> None:
    """Release the singleton bot lock if this process owns it."""
    global _lock_handle
    if _lock_handle is None:
        return

    handle = _lock_handle
    _lock_handle = None
    try:
        handle.seek(0)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())
    except OSError:
        pass
    with contextlib.suppress(OSError):
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()
    with contextlib.suppress(OSError):
        _lock_path().unlink()
