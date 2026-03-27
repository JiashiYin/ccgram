# Notify Service Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Goal

Make `ccgram notify install` production-usable by starting and managing the local background `ccgram` bridge automatically. After install, plain `codex` should not require a second terminal running `ccgram`.

## File Map

- `src/ccgram/notify_service.py`
  Service lifecycle for the local background bridge: state file, start/stop/status helpers, detached process spawn.
- `src/ccgram/notify_cmd.py`
  Wire service management into `notify install/status/disable/uninstall/launch`.
- `src/ccgram/notify_shell.py`
  Small helper for deciding whether any notify providers remain enabled after disable/uninstall.
- `tests/ccgram/test_notify_service.py`
  Unit tests for the service runner/status logic.
- `tests/ccgram/test_notify_cmd.py`
  CLI behavior tests proving install starts the service, launch self-heals it, and disable/uninstall stop it when appropriate.
- `README.md`
  Update local install UX so users are not told to keep a separate `ccgram` terminal open.

## Tasks

- [ ] Add failing unit tests for service status, start, and stop in `tests/ccgram/test_notify_service.py`.
- [ ] Run only the new service tests and verify they fail for the missing module/API.
- [ ] Implement `src/ccgram/notify_service.py` with a managed background runner backed by state in `~/.ccgram`.
- [ ] Run the new service tests and make them pass.
- [ ] Add failing CLI tests in `tests/ccgram/test_notify_cmd.py` for install starting the service, launch ensuring it is running, and disable/uninstall stopping it when no notify providers remain enabled.
- [ ] Run only the new CLI tests and verify they fail.
- [ ] Wire `notify_cmd.py` and `notify_shell.py` to use the service lifecycle helpers.
- [ ] Run the notify-focused pytest matrix and make it pass.
- [ ] Update `README.md` so the documented local flow no longer requires a separate `ccgram` terminal.
- [ ] Run `ruff check` on touched files and the notify-focused pytest matrix again.
