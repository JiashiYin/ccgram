# Notify Project Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn CCGram’s current notification system into a productized `interactive` / `notify` workflow, and add a Codex-first setup flow so normal `codex` launches can default into Telegram-backed notify mode with reversible disable/uninstall.

**Architecture:** Build on the existing `alexei-led/ccgram` multi-provider core. First rename and migrate the mode model from `all / errors_only / muted` to `interactive / notify / errors_only / muted`, then port the already proven VM-only passive-session behavior into source as `notify` with explicit `[CCGRAM_MILESTONE]` / `[CCGRAM_FINAL]` summaries. Finally add a first-class `ccgram notify ...` CLI flow plus shell integration helpers that make plain `codex` launches enter the monitored tmux workflow by default.

**Tech Stack:** Python 3.14, Click, python-telegram-bot, tmux, uv, pytest, ruff, pyright

---

### Task 1: Reframe Notification Modes Around `interactive` / `notify`

**Files:**
- Modify: `src/ccgram/handlers/callback_data.py`
- Modify: `src/ccgram/session.py`
- Modify: `src/ccgram/handlers/message_queue.py`
- Modify: `src/ccgram/handlers/screenshot_callbacks.py`
- Modify: `tests/ccgram/test_session_notification_mode.py`
- Modify: `tests/integration/test_state_roundtrip.py`
- Test: `tests/ccgram/test_session_notification_mode.py`

- [ ] **Step 1: Write the failing mode-migration tests**

```python
def test_get_default_mode_returns_interactive(mgr: SessionManager) -> None:
    assert mgr.get_notification_mode("@0") == "interactive"


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"session_id": "s1", "cwd": "/tmp"}, "interactive"),
        ({"session_id": "s1", "cwd": "/tmp", "notification_mode": "all"}, "interactive"),
        ({"session_id": "s1", "cwd": "/tmp", "notification_mode": "passive"}, "notify"),
        ({"session_id": "s1", "cwd": "/tmp", "notification_mode": "notify"}, "notify"),
    ],
)
def test_window_state_from_dict_normalizes_legacy_modes(
    data: dict[str, str], expected: str
) -> None:
    assert WindowState.from_dict(data).notification_mode == expected
```

- [ ] **Step 2: Run the notification-mode tests to verify red**

Run:

```bash
cd /mnt/c/Users/jacob/projects/helloworld/ML/cloud/ccgram-upstream/.worktrees/notify-v1
uv run pytest tests/ccgram/test_session_notification_mode.py -q
```

Expected:

```text
FAIL ... expected "interactive" but got "all"
```

- [ ] **Step 3: Implement the new mode vocabulary and legacy normalization**

```python
def _normalize_notification_mode(mode: str) -> str:
    if mode in ("", "all", "normal"):
        return "interactive"
    if mode == "passive":
        return "notify"
    if mode in NOTIFICATION_MODES:
        return mode
    return "interactive"
```

Also update:

```python
NOTIFICATION_MODES = ("interactive", "notify", "errors_only", "muted")
NOTIFY_MODE_ICONS = {
    "interactive": "🔔",
    "notify": "📣",
    "errors_only": "⚠️",
    "muted": "🔕",
}
```

- [ ] **Step 4: Re-run the focused tests to verify green**

Run:

```bash
uv run pytest tests/ccgram/test_session_notification_mode.py -q
```

Expected:

```text
... passed
```

## Verification Log

### 2026-03-27 Task 4

- `CCGRAM_DIR=/tmp/ccgram-test TELEGRAM_BOT_TOKEN=test-token ALLOWED_USERS=1 .venv/bin/pytest tests/ccgram/test_cli.py tests/ccgram/test_notify_cmd.py tests/ccgram/test_doctor_cmd.py tests/ccgram/test_status_cmd.py -q`
  - `58 passed in 4.49s`
- `.venv/bin/ruff check src/ccgram/cli.py src/ccgram/notify_cmd.py src/ccgram/notify_shell.py src/ccgram/doctor_cmd.py src/ccgram/status_cmd.py tests/ccgram/test_cli.py tests/ccgram/test_notify_cmd.py tests/ccgram/test_doctor_cmd.py tests/ccgram/test_status_cmd.py`
  - `All checks passed!`
- `CCGRAM_DIR=/tmp/ccgram-test TELEGRAM_BOT_TOKEN=test-token ALLOWED_USERS=1 .venv/bin/pytest tests/ccgram/test_provider_registry.py -q`
  - `30 passed in 3.61s`

### 2026-03-27 Task 5

- `CCGRAM_DIR=/tmp/ccgram-test TELEGRAM_BOT_TOKEN=test-token ALLOWED_USERS=1 .venv/bin/pytest tests/ccgram/test_cli.py -q`
  - `23 passed in 5.22s`
- `.venv/bin/python -m json.tool .codex-plugin/plugin.json`
- `.venv/bin/python -m json.tool .claude-plugin/plugin.json`
- `.venv/bin/python -m json.tool .claude-plugin/marketplace.json`
  - all three parsed successfully

### 2026-03-27 Task 6

- `HOME=/tmp/ccgram-notify-home UV_CACHE_DIR=/tmp/ccgram-notify-cache uv tool install --editable . --python .venv/bin/python`
  - clean install succeeded and installed `ccgram` into `/tmp/ccgram-notify-home/.local/bin`
- `HOME=/tmp/ccgram-notify-home /tmp/ccgram-notify-home/.local/bin/ccgram notify --help`
  - exited `0` and showed `install`, `status`, `disable`, `uninstall`, and `launch`
- `CCGRAM_DIR=/tmp/ccgram-test TELEGRAM_BOT_TOKEN=test-token ALLOWED_USERS=1 .venv/bin/pytest tests/ccgram/test_session_notification_mode.py -q`
  - `39 passed in 2.36s`
- `CCGRAM_DIR=/tmp/ccgram-test TELEGRAM_BOT_TOKEN=test-token ALLOWED_USERS=1 .venv/bin/pytest tests/ccgram/test_notify_mode.py -q`
  - `13 passed in 7.17s`
- `CCGRAM_DIR=/tmp/ccgram-test TELEGRAM_BOT_TOKEN=test-token ALLOWED_USERS=1 .venv/bin/pytest tests/ccgram/test_notify_cmd.py -q`
  - `6 passed in 4.44s`
- `CCGRAM_DIR=/tmp/ccgram-test TELEGRAM_BOT_TOKEN=test-token ALLOWED_USERS=1 .venv/bin/pytest tests/integration/test_state_roundtrip.py -q`
  - `9 passed in 4.23s`

Notes:

- Combined long-running pytest invocations that mixed large files sometimes hung after printing progress in this repo. When that happened, narrower per-file invocations completed and produced clean pass results.
- `tests/ccgram/test_status_polling.py` and `tests/integration/test_message_dispatch.py` both advanced significantly under full-file runs during Task 6 but did not exit cleanly in this terminal harness, matching the earlier repo behavior seen during Task 2.

- [ ] **Step 5: Verify state round-trip still works**

Run:

```bash
uv run pytest tests/integration/test_state_roundtrip.py -q
```

Expected:

```text
... passed
```

- [ ] **Step 6: Commit**

```bash
git add src/ccgram/handlers/callback_data.py src/ccgram/session.py src/ccgram/handlers/message_queue.py src/ccgram/handlers/screenshot_callbacks.py tests/ccgram/test_session_notification_mode.py tests/integration/test_state_roundtrip.py
git commit -m "refactor: rename notification modes to interactive and notify"
```

### Task 2: Port Passive-Session Routing Into Source As `notify`

**Files:**
- Modify: `src/ccgram/bot.py`
- Modify: `src/ccgram/handlers/status_polling.py`
- Modify: `src/ccgram/handlers/directory_callbacks.py`
- Modify: `src/ccgram/handlers/window_callbacks.py`
- Modify: `src/ccgram/handlers/recovery_callbacks.py`
- Modify: `src/ccgram/handlers/resume_command.py`
- Modify: `src/ccgram/handlers/restore_command.py`
- Modify: `tests/ccgram/test_handle_new_window.py`
- Modify: `tests/ccgram/test_status_polling.py`
- Add: `tests/ccgram/test_notify_mode.py`
- Test: `tests/ccgram/test_handle_new_window.py`
- Test: `tests/ccgram/test_status_polling.py`
- Test: `tests/ccgram/test_notify_mode.py`

- [ ] **Step 1: Write failing tests for session-origin defaults and quiet routing**

```python
async def test_external_window_defaults_to_notify(...) -> None:
    await handle_new_window(mock_bot, event)
    assert session_manager.get_notification_mode(event.window_id) == "notify"


async def test_telegram_created_window_stays_interactive(...) -> None:
    assert session_manager.get_notification_mode(created_wid) == "interactive"


async def test_notify_mode_filters_routine_assistant_output(...) -> None:
    session_manager.set_notification_mode("@7", "notify")
    await handle_new_message(mock_bot, new_message_event)
    enqueue_content_message.assert_not_called()
```

- [ ] **Step 2: Run the new focused tests to verify red**

Run:

```bash
uv run pytest tests/ccgram/test_handle_new_window.py tests/ccgram/test_status_polling.py tests/ccgram/test_notify_mode.py -q
```

Expected:

```text
FAIL ... expected notify default for external windows
```

- [ ] **Step 3: Implement `notify` defaults and filtering**

Port the proven VM behavior into source:

```python
if current_mode in ("", "all", "normal"):
    session_manager.set_notification_mode(event.window_id, "notify")
```

and in message routing:

```python
elif notif_mode == "notify":
    if is_interactive_tool:
        pass
    elif is_notify_summary and filtered_text:
        pass
    else:
        continue
```

and in status polling:

```python
if queue and not queue.empty():
    if session_manager.get_notification_mode(wid) == "notify":
        await update_status_message(bot, user_id, wid, thread_id=thread_id)
    else:
        await update_status_message(
            bot, user_id, wid, thread_id=thread_id, skip_status=True
        )
    continue
```

- [ ] **Step 4: Ensure Telegram-origin flows explicitly force `interactive`**

Add or preserve:

```python
session_manager.set_notification_mode(created_wid, "interactive")
```

in:

- new directory-created sessions
- bind/rebind flows
- resume/restore/recovery-created sessions

- [ ] **Step 5: Re-run the focused tests to verify green**

Run:

```bash
uv run pytest tests/ccgram/test_handle_new_window.py tests/ccgram/test_status_polling.py tests/ccgram/test_notify_mode.py -q
```

Expected:

```text
... passed
```

- [ ] **Step 6: Commit**

```bash
git add src/ccgram/bot.py src/ccgram/handlers/status_polling.py src/ccgram/handlers/directory_callbacks.py src/ccgram/handlers/window_callbacks.py src/ccgram/handlers/recovery_callbacks.py src/ccgram/handlers/resume_command.py src/ccgram/handlers/restore_command.py tests/ccgram/test_handle_new_window.py tests/ccgram/test_status_polling.py tests/ccgram/test_notify_mode.py
git commit -m "feat: add notify-mode session routing"
```

### Task 3: Add Explicit Milestone and Final Summary Delivery

**Files:**
- Modify: `src/ccgram/bot.py`
- Modify: `tests/ccgram/test_notify_mode.py`
- Modify: `README.md`
- Test: `tests/ccgram/test_notify_mode.py`

- [ ] **Step 1: Write failing tests for explicit summary markers**

```python
def test_notify_mode_strips_and_delivers_final_summary(...) -> None:
    text = "[CCGRAM_FINAL] finished sync and tests passed"
    is_summary, filtered = _extract_notify_summary(text)
    assert is_summary is True
    assert filtered == "finished sync and tests passed"
```

and:

```python
async def test_notify_mode_allows_explicit_milestone_message(...) -> None:
    session_manager.set_notification_mode("@7", "notify")
    message.text = "[CCGRAM_MILESTONE] shell integration installed"
    await handle_new_message(mock_bot, event)
    enqueue_content_message.assert_called_once()
```

- [ ] **Step 2: Run the focused tests to verify red**

Run:

```bash
uv run pytest tests/ccgram/test_notify_mode.py -q
```

Expected:

```text
FAIL ... _extract_notify_summary not defined
```

- [ ] **Step 3: Implement the summary marker contract**

```python
_NOTIFY_SUMMARY_MARKERS = ("[CCGRAM_MILESTONE]", "[CCGRAM_FINAL]")


def _extract_notify_summary(text: str) -> tuple[bool, str]:
    for marker in _NOTIFY_SUMMARY_MARKERS:
        if text.startswith(marker):
            return True, text[len(marker):].strip()
    return False, text
```

Use the stripped text when building response parts.

- [ ] **Step 4: Re-run the focused tests to verify green**

Run:

```bash
uv run pytest tests/ccgram/test_notify_mode.py -q
```

Expected:

```text
... passed
```

- [ ] **Step 5: Document the marker contract**

Add README examples such as:

```text
[CCGRAM_MILESTONE] Shell integration installed; ready for dry run
[CCGRAM_FINAL] Notify setup verified on VM and Telegram
```

- [ ] **Step 6: Commit**

```bash
git add src/ccgram/bot.py tests/ccgram/test_notify_mode.py README.md
git commit -m "feat: support explicit notify milestone and final summaries"
```

### Task 4: Add First-Class `ccgram notify` Setup / Disable / Uninstall Commands

**Files:**
- Modify: `src/ccgram/cli.py`
- Add: `src/ccgram/notify_cmd.py`
- Add: `src/ccgram/notify_shell.py`
- Modify: `src/ccgram/doctor_cmd.py`
- Modify: `src/ccgram/status_cmd.py`
- Add: `tests/ccgram/test_notify_cmd.py`
- Modify: `tests/ccgram/test_cli.py`
- Modify: `tests/ccgram/test_doctor_cmd.py`
- Test: `tests/ccgram/test_notify_cmd.py`
- Test: `tests/ccgram/test_cli.py`
- Test: `tests/ccgram/test_doctor_cmd.py`

- [ ] **Step 1: Write failing CLI tests for install/status/disable/uninstall**

```python
def test_notify_install_command_is_registered(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["notify", "--help"])
    assert result.exit_code == 0
    assert "install" in result.output
    assert "disable" in result.output
    assert "uninstall" in result.output
```

and:

```python
def test_notify_install_writes_shell_snippet(tmp_path: Path, monkeypatch) -> None:
    result = runner.invoke(cli, ["notify", "install", "--provider", "codex", "--shell", "bash"])
    assert result.exit_code == 0
    assert (tmp_path / "codex-notify.sh").exists()
```

- [ ] **Step 2: Run the focused CLI tests to verify red**

Run:

```bash
uv run pytest tests/ccgram/test_cli.py tests/ccgram/test_notify_cmd.py tests/ccgram/test_doctor_cmd.py -q
```

Expected:

```text
FAIL ... No such command 'notify'
```

- [ ] **Step 3: Implement the `notify` command group**

Add Click subcommands with behavior roughly like:

```python
@cli.group("notify")
def notify_group() -> None:
    """Codex-first setup and shell integration commands."""


@notify_group.command("install")
@click.option("--provider", default="codex")
@click.option("--shell", type=click.Choice(["bash", "zsh", "fish"]))
def notify_install(provider: str, shell: str | None) -> None:
    notify_install_main(provider=provider, shell=shell)
```

Implement helpers that:

- generate a shell snippet wrapping plain `codex`
- preserve access to the direct binary (for example via `codex-direct`)
- enable or update status in a reversible way
- report clear next steps and test instructions

- [ ] **Step 4: Extend doctor/status output to include notify-shell health**

Add checks for:

- shell snippet installed
- wrapper target resolves a real Codex executable
- configured mode default is `notify`

- [ ] **Step 5: Re-run the focused CLI tests to verify green**

Run:

```bash
uv run pytest tests/ccgram/test_cli.py tests/ccgram/test_notify_cmd.py tests/ccgram/test_doctor_cmd.py -q
```

Expected:

```text
... passed
```

- [ ] **Step 6: Commit**

```bash
git add src/ccgram/cli.py src/ccgram/notify_cmd.py src/ccgram/notify_shell.py src/ccgram/doctor_cmd.py src/ccgram/status_cmd.py tests/ccgram/test_notify_cmd.py tests/ccgram/test_cli.py tests/ccgram/test_doctor_cmd.py
git commit -m "feat: add codex-first notify setup commands"
```

### Task 5: Add Plugin Metadata and Product Docs

**Files:**
- Modify: `README.md`
- Add: `.codex-plugin/plugin.json`
- Add: `.claude-plugin/plugin.json`
- Add: `.claude-plugin/marketplace.json`
- Add: `docs/superpowers/specs/2026-03-27-ccgram-notify-plugin-design.md`
- Test: `tests/ccgram/test_cli.py`

- [ ] **Step 1: Create the failing documentation/metadata checklist**

Use this review checklist as the failing target:

```text
- README documents interactive vs notify
- README documents ccgram notify install / disable / uninstall
- README documents Codex default notify behavior
- README documents [CCGRAM_MILESTONE] and [CCGRAM_FINAL]
- repo includes plugin manifest(s) for marketplace-style installation
```

- [ ] **Step 2: Add plugin metadata files**

Create root manifests such as:

```json
{
  "name": "ccgram-notify",
  "displayName": "CCGram Notify",
  "description": "Telegram notify and remote-control workflow for agent CLIs, with Codex-first setup",
  "version": "0.0.0",
  "repository": "https://github.com/<your-org-or-user>/ccgram-notify"
}
```

- [ ] **Step 3: Rewrite README for the shipped product**

Cover:

- install from GitHub
- `ccgram notify install`
- `ccgram notify status`
- `ccgram notify disable`
- `ccgram notify uninstall`
- default `notify` behavior for plain `codex`
- how Telegram-opened sessions stay `interactive`
- milestone/final summary markers

- [ ] **Step 4: Re-run a lightweight CLI/help check**

Run:

```bash
uv run pytest tests/ccgram/test_cli.py -q
```

Expected:

```text
... passed
```

- [ ] **Step 5: Commit**

```bash
git add README.md .codex-plugin/plugin.json .claude-plugin/plugin.json .claude-plugin/marketplace.json docs/superpowers/specs/2026-03-27-ccgram-notify-plugin-design.md
git commit -m "docs: package ccgram notify as a plugin-style product"
```

### Task 6: Verify on a Python 3.14 Host and Reinstall From Scratch

**Files:**
- Modify: `docs/superpowers/plans/2026-03-27-notify-project.md`
- Modify: `README.md`
- Test: `tests/ccgram/test_session_notification_mode.py`
- Test: `tests/ccgram/test_notify_mode.py`
- Test: `tests/ccgram/test_notify_cmd.py`
- Test: `tests/ccgram/test_status_polling.py`
- Test: `tests/integration/test_message_dispatch.py`
- Test: `tests/integration/test_state_roundtrip.py`

- [ ] **Step 1: Run focused verification on a Python 3.14 machine**

Run:

```bash
cd /path/to/ccgram-notify
uv sync --extra dev
uv run pytest tests/ccgram/test_session_notification_mode.py tests/ccgram/test_notify_mode.py tests/ccgram/test_notify_cmd.py tests/ccgram/test_status_polling.py -q
```

Expected:

```text
... passed
```

- [ ] **Step 2: Run the project’s standard quality gates**

Run:

```bash
make lint
make typecheck
make test
make test-integration
```

Expected:

```text
All commands exit 0
```

- [ ] **Step 3: Perform a clean install validation**

Run from a clean directory or VM:

```bash
git clone https://github.com/<your-org-or-user>/ccgram-notify.git
cd ccgram-notify
uv sync --extra dev
ccgram notify install --provider codex --shell bash
ccgram notify status
```

Expected:

```text
notify shell integration installed
doctor/status reports healthy configuration
```

- [ ] **Step 4: Perform a real Telegram flow smoke test**

Validate:

```text
1. normal codex launch defaults to notify
2. blocking prompt auto-creates or uses a topic and reaches Telegram
3. final summary marker reaches Telegram
4. Telegram-created topic remains interactive
5. disable stops shell interception
6. uninstall removes the integration cleanly
```

- [ ] **Step 5: Commit**

```bash
git add README.md docs/superpowers/plans/2026-03-27-notify-project.md
git commit -m "test: verify notify workflow end to end"
```
