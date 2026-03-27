"""Shell integration helpers for ``ccgram notify`` commands."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from .providers import resolve_capabilities, resolve_launch_command
from .utils import atomic_write_json, ccgram_dir

_SUPPORTED_SHELLS = ("bash", "zsh", "fish")
_STATE_FILE = "notify-state.json"
_STATE_PROVIDERS_KEY = "providers"
_RC_BLOCK_BEGIN = "# >>> ccgram notify >>>"
_RC_BLOCK_END = "# <<< ccgram notify <<<"
_MODE_NOTIFY = "notify"
_ENV_KEY_TEMPLATE = "CCGRAM_{provider}_COMMAND"
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")


@dataclass
class NotifyStatus:
    """Persisted shell-integration status for a provider."""

    provider: str
    installed: bool
    enabled: bool
    shell: str
    mode: str
    rc_path: str
    snippet_path: str
    direct_launcher_path: str
    direct_command: str
    env_key: str
    env_value: str
    rc_hook_present: bool
    snippet_exists: bool
    direct_launcher_exists: bool


def _state_path() -> Path:
    return ccgram_dir() / _STATE_FILE


def _load_state() -> dict[str, object]:
    path = _state_path()
    if not path.exists():
        return {_STATE_PROVIDERS_KEY: {}}
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError, OSError:
        return {_STATE_PROVIDERS_KEY: {}}
    if not isinstance(raw, dict):
        return {_STATE_PROVIDERS_KEY: {}}
    providers = raw.get(_STATE_PROVIDERS_KEY)
    if not isinstance(providers, dict):
        raw[_STATE_PROVIDERS_KEY] = {}
    return raw


def _save_state(data: dict[str, object]) -> None:
    atomic_write_json(_state_path(), data)


def _normalize_shell(shell: str | None) -> str:
    raw = shell or os.environ.get("SHELL", "") or "bash"
    normalized = Path(raw).name.strip().lower() or "bash"
    if normalized not in _SUPPORTED_SHELLS:
        raise ValueError(
            f"Unsupported shell: {shell or raw!r}. Supported: {', '.join(_SUPPORTED_SHELLS)}"
        )
    return normalized


def _default_rc_path(shell: str) -> Path:
    home = Path.home()
    if shell == "bash":
        return home / ".bashrc"
    if shell == "zsh":
        return home / ".zshrc"
    return home / ".config" / "fish" / "config.fish"


def _snippet_path(provider: str, shell: str) -> Path:
    return ccgram_dir() / "notify" / f"{provider}.{shell}"


def _direct_launcher_path(provider: str) -> Path:
    return ccgram_dir() / "bin" / f"{provider}-direct"


def _env_key(provider: str) -> str:
    return _ENV_KEY_TEMPLATE.format(provider=provider.upper())


def _dotenv_path() -> Path:
    return ccgram_dir() / ".env"


def _read_dotenv(path: Path) -> list[str]:
    try:
        return path.read_text().splitlines()
    except OSError:
        return []


def _write_dotenv(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines).strip()
    if text:
        text += "\n"
    path.write_text(text)


def _set_env_value(path: Path, key: str, value: str) -> None:
    updated = []
    replaced = False
    prefix = f"{key}="
    for line in _read_dotenv(path):
        if line.startswith(prefix):
            if not replaced:
                updated.append(f"{key}={value}")
                replaced = True
            continue
        updated.append(line)
    if not replaced:
        updated.append(f"{key}={value}")
    _write_dotenv(path, updated)


def _remove_env_value(path: Path, key: str) -> None:
    prefix = f"{key}="
    updated = [line for line in _read_dotenv(path) if not line.startswith(prefix)]
    _write_dotenv(path, updated)


def _read_env_value(path: Path, key: str) -> str:
    prefix = f"{key}="
    for line in _read_dotenv(path):
        if line.startswith(prefix):
            return line[len(prefix) :]
    return ""


def _rc_block_text(snippet_path: Path) -> str:
    quoted = shlex.quote(str(snippet_path))
    return f"{_RC_BLOCK_BEGIN}\nsource {quoted}\n{_RC_BLOCK_END}\n"


def _strip_rc_block(content: str) -> str:
    start = content.find(_RC_BLOCK_BEGIN)
    if start == -1:
        return content
    end = content.find(_RC_BLOCK_END, start)
    if end == -1:
        return content[:start].rstrip() + ("\n" if content[:start].strip() else "")
    after = end + len(_RC_BLOCK_END)
    remainder = content[:start] + content[after:]
    return remainder.lstrip("\n")


def _write_rc_block(rc_path: Path, snippet_path: Path) -> None:
    rc_path.parent.mkdir(parents=True, exist_ok=True)
    content = ""
    if rc_path.exists():
        content = rc_path.read_text()
    stripped = _strip_rc_block(content).rstrip()
    block = _rc_block_text(snippet_path).rstrip()
    final = f"{stripped}\n\n{block}\n" if stripped else f"{block}\n"
    rc_path.write_text(final)


def _remove_rc_block(rc_path: Path) -> None:
    if not rc_path.exists():
        return
    rc_path.write_text(_strip_rc_block(rc_path.read_text()))


def _rc_hook_present(rc_path: Path) -> bool:
    try:
        return _RC_BLOCK_BEGIN in rc_path.read_text()
    except OSError:
        return False


def _looks_like_assignment(token: str) -> bool:
    return bool(_ASSIGNMENT_RE.match(token))


def _render_direct_launcher(command: str) -> str:
    tokens = shlex.split(command)
    env_tokens: list[str] = []
    while tokens and _looks_like_assignment(tokens[0]):
        env_tokens.append(tokens.pop(0))
    if not tokens:
        raise ValueError(f"Could not resolve executable from command: {command!r}")

    lines = ["#!/usr/bin/env bash", "set -euo pipefail"]
    for assignment in env_tokens:
        key, _, value = assignment.partition("=")
        lines.append(f"export {key}={shlex.quote(value)}")
    quoted_command = " ".join(shlex.quote(token) for token in tokens)
    lines.append(f'exec {quoted_command} "$@"')
    return "\n".join(lines) + "\n"


def _capture_existing_shell_function(provider: str, shell: str) -> str:
    if shell != "bash":
        return ""
    try:
        proc = subprocess.run(
            ["bash", "-ic", f"declare -f {provider}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except OSError:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _command_from_shell_function(provider: str, function_text: str) -> str:
    if not function_text or "ccgram notify launch" in function_text:
        return ""
    compact = " ".join(
        line.strip().rstrip("\\")
        for line in function_text.splitlines()
        if line.strip() and line.strip() not in {"{", "}"}
    )
    marker = f"command {provider}"
    idx = compact.find(marker)
    if idx == -1:
        return ""
    suffix = compact[idx + len(marker) :].strip()
    suffix = suffix.removesuffix('"$@"').strip()
    suffix = suffix.removesuffix('"$@"').strip()
    suffix = suffix.removesuffix("$@").strip()
    path = shutil.which(provider) or provider
    return f"{path} {suffix}".strip() if suffix else path


def _write_direct_launcher(path: Path, command: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_direct_launcher(command))
    path.chmod(0o755)


def _shell_wrapper(provider: str, shell: str, direct_path: Path) -> str:
    quoted_direct = shlex.quote(str(direct_path))
    if shell == "fish":
        return (
            f"function {provider}\n"
            f"    command ccgram notify launch --provider {provider} --mode notify -- $argv\n"
            "end\n\n"
            f"function {provider}-direct\n"
            f"    command {quoted_direct} $argv\n"
            "end\n"
        )
    return (
        f"{provider}() {{\n"
        f'  command ccgram notify launch --provider {provider} --mode notify -- "$@"\n'
        "}\n\n"
        f"alias {provider}-direct={quoted_direct}\n"
    )


def _write_shell_wrapper(
    path: Path, provider: str, shell: str, direct_path: Path
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_shell_wrapper(provider, shell, direct_path))


def _resolve_direct_command(
    provider: str,
    existing: dict[str, object] | None,
    *,
    shell_name: str,
) -> str:
    env_key = _env_key(provider)
    direct_path = _direct_launcher_path(provider)
    override = os.environ.get(env_key, "")
    if override and override != str(direct_path):
        return override

    function_command = _command_from_shell_function(
        provider,
        _capture_existing_shell_function(provider, shell_name),
    )
    if function_command:
        return function_command

    if existing:
        direct_command = existing.get("direct_command", "")
        if isinstance(direct_command, str) and direct_command:
            return direct_command

    path = shutil.which(provider)
    if path:
        return path
    return resolve_capabilities(provider).launch_command


def install_notify_shell(
    provider: str = "codex", shell: str | None = None
) -> NotifyStatus:
    """Install or refresh shell integration for a provider."""
    provider = provider.lower()
    shell_name = _normalize_shell(shell)
    rc_path = _default_rc_path(shell_name)
    snippet_path = _snippet_path(provider, shell_name)
    direct_path = _direct_launcher_path(provider)

    state = _load_state()
    providers = state.setdefault(_STATE_PROVIDERS_KEY, {})
    assert isinstance(providers, dict)
    existing = providers.get(provider)
    existing_dict = existing if isinstance(existing, dict) else None
    direct_command = _resolve_direct_command(
        provider,
        existing_dict,
        shell_name=shell_name,
    )

    if existing_dict:
        old_rc_path = (
            Path(str(existing_dict.get("rc_path", "")))
            if existing_dict.get("rc_path")
            else None
        )
        old_snippet_path = (
            Path(str(existing_dict.get("snippet_path", "")))
            if existing_dict.get("snippet_path")
            else None
        )
        if old_rc_path and old_rc_path != rc_path:
            _remove_rc_block(old_rc_path)
        if old_snippet_path and old_snippet_path != snippet_path:
            with contextlib.suppress(OSError):
                old_snippet_path.unlink(missing_ok=True)

    _write_direct_launcher(direct_path, direct_command)
    _write_shell_wrapper(snippet_path, provider, shell_name, direct_path)
    _write_rc_block(rc_path, snippet_path)
    _set_env_value(_dotenv_path(), _env_key(provider), str(direct_path))

    providers[provider] = {
        "provider": provider,
        "enabled": True,
        "shell": shell_name,
        "mode": _MODE_NOTIFY,
        "rc_path": str(rc_path),
        "snippet_path": str(snippet_path),
        "direct_launcher_path": str(direct_path),
        "direct_command": direct_command,
    }
    _save_state(state)
    return get_notify_status(provider)


def disable_notify_shell(provider: str = "codex") -> NotifyStatus:
    """Disable rc-file integration without removing installed artifacts."""
    provider = provider.lower()
    state = _load_state()
    providers = state.setdefault(_STATE_PROVIDERS_KEY, {})
    assert isinstance(providers, dict)
    existing = providers.get(provider)
    if not isinstance(existing, dict):
        return get_notify_status(provider)

    rc_path = (
        Path(str(existing.get("rc_path", ""))) if existing.get("rc_path") else None
    )
    if rc_path:
        _remove_rc_block(rc_path)
    existing["enabled"] = False
    providers[provider] = existing
    _save_state(state)
    return get_notify_status(provider)


def uninstall_notify_shell(provider: str = "codex") -> NotifyStatus:
    """Remove installed shell integration and direct-launch override."""
    provider = provider.lower()
    state = _load_state()
    providers = state.setdefault(_STATE_PROVIDERS_KEY, {})
    assert isinstance(providers, dict)
    existing = providers.get(provider)
    if not isinstance(existing, dict):
        _remove_env_value(_dotenv_path(), _env_key(provider))
        return get_notify_status(provider)

    for key in ("rc_path", "snippet_path", "direct_launcher_path"):
        raw = existing.get(key, "")
        if not isinstance(raw, str) or not raw:
            continue
        path = Path(raw)
        if key == "rc_path":
            _remove_rc_block(path)
            continue
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)

    _remove_env_value(_dotenv_path(), _env_key(provider))
    providers.pop(provider, None)
    _save_state(state)
    return get_notify_status(provider)


def get_notify_status(provider: str = "codex") -> NotifyStatus:
    """Return current notify-shell status for a provider."""
    provider = provider.lower()
    state = _load_state()
    providers = state.get(_STATE_PROVIDERS_KEY, {})
    entry = providers.get(provider) if isinstance(providers, dict) else None
    if not isinstance(entry, dict):
        return NotifyStatus(
            provider=provider,
            installed=False,
            enabled=False,
            shell="",
            mode=_MODE_NOTIFY,
            rc_path="",
            snippet_path="",
            direct_launcher_path="",
            direct_command="",
            env_key=_env_key(provider),
            env_value="",
            rc_hook_present=False,
            snippet_exists=False,
            direct_launcher_exists=False,
        )

    rc_path = Path(str(entry.get("rc_path", "")))
    snippet_path = Path(str(entry.get("snippet_path", "")))
    direct_path = Path(str(entry.get("direct_launcher_path", "")))
    env_key = _env_key(provider)
    env_value = _read_env_value(_dotenv_path(), env_key)

    return NotifyStatus(
        provider=provider,
        installed=True,
        enabled=bool(entry.get("enabled", False)),
        shell=str(entry.get("shell", "")),
        mode=str(entry.get("mode", _MODE_NOTIFY)),
        rc_path=str(rc_path),
        snippet_path=str(snippet_path),
        direct_launcher_path=str(direct_path),
        direct_command=str(entry.get("direct_command", "")),
        env_key=env_key,
        env_value=env_value,
        rc_hook_present=_rc_hook_present(rc_path),
        snippet_exists=snippet_path.exists(),
        direct_launcher_exists=direct_path.exists(),
    )


def iter_notify_statuses() -> list[NotifyStatus]:
    """Return statuses for every installed notify-shell provider."""
    state = _load_state()
    providers = state.get(_STATE_PROVIDERS_KEY, {})
    if not isinstance(providers, dict):
        return []
    return [get_notify_status(provider) for provider in sorted(providers)]


def any_notify_providers_enabled() -> bool:
    """Return True when any provider still has notify shell integration enabled."""
    return any(status.enabled for status in iter_notify_statuses())


def resolve_notify_launch_command(provider: str) -> str:
    """Resolve the command used for notify-managed provider launches."""
    status = get_notify_status(provider)
    if status.installed and status.direct_launcher_exists:
        return status.env_value or status.direct_launcher_path
    return resolve_launch_command(provider)


def status_to_dict(status: NotifyStatus) -> dict[str, object]:
    """Serialize status for tests or diagnostics."""
    return asdict(status)
