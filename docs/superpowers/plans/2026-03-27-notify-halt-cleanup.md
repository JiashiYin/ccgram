# Notify Halt Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make notify mode surface all real halts without marker tokens and guarantee cleanup of confirmed dead notify sessions.

**Architecture:** Reuse the existing polling/runtime state machine instead of LLM-emitted tokens. `bot.handle_new_message()` will cache suppressed notify-mode assistant report-backs, and `status_polling` will release the cached report when a topic transitions from active to idle. Dead notify topics will be confirmed across a short missing-window grace window and then cleaned automatically with topic deletion/close + state unbind/prune, while interactive topics keep the original recovery-oriented behavior.

**Tech Stack:** Python, asyncio, python-telegram-bot, pytest

---

### Task 1: Replace Marker-Based Notify Delivery With Halt-State Delivery

**Files:**
- Modify: `src/ccgram/bot.py`
- Modify: `src/ccgram/handlers/status_polling.py`
- Test: `tests/ccgram/test_notify_mode.py`
- Test: `tests/ccgram/test_status_polling.py`

- [ ] Add failing tests that prove notify-mode plain assistant replies are cached and delivered only on active→idle halt.
- [ ] Add failing tests that prove old `[CCGRAM_MILESTONE]` / `[CCGRAM_FINAL]` marker routing is no longer required.
- [ ] Implement a notify runtime cache for the latest assistant report-back candidate per topic.
- [ ] Trigger cached notify delivery from the existing active→idle transition logic.
- [ ] Run focused tests for notify routing and idle transition behavior.

### Task 2: Guarantee Cleanup For Confirmed Dead Notify Sessions

**Files:**
- Modify: `src/ccgram/handlers/status_polling.py`
- Modify: `src/ccgram/handlers/sync_command.py`
- Modify: `src/ccgram/native_sessions.py`
- Modify: `src/ccgram/tmux_manager.py`
- Test: `tests/ccgram/test_status_polling.py`
- Test: `tests/ccgram/test_native_sessions.py`

- [ ] Add failing tests that prove notify-mode ghost bindings are automatically deleted/unbound after a short missing-window confirmation window.
- [ ] Add failing tests that prove exited native sessions stop appearing as live windows for dead-session detection.
- [ ] Extract/reuse topic removal helpers so automatic cleanup and `/sync` use the same delete/close + unbind behavior.
- [ ] Implement notify-only dead-session confirmation and immediate cleanup.
- [ ] Keep interactive dead-session behavior recovery-oriented to avoid overhauling original ccgram UX.
- [ ] Run focused cleanup/dead-session tests.

### Task 3: Remove Marker Docs And Re-verify Local Notify UX

**Files:**
- Modify: `README.md`
- Modify: `tests/ccgram/test_notify_mode.py`
- Modify: `tests/ccgram/test_notify_cmd.py` (only if expectations need adjustment)

- [ ] Remove public docs that instruct agents/users to emit `[CCGRAM_MILESTONE]` / `[CCGRAM_FINAL]`.
- [ ] Remove marker-specific tests that no longer match the product contract.
- [ ] Run the broader touched-suite and lint checks.
- [ ] Refresh the local notify install and verify the generated shell snippet still matches the intended UX.

