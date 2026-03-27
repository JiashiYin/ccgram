# Notify Guided Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `ccgram notify install` guide first-time Telegram setup instead of requiring manual `.env` editing.

**Architecture:** Add a focused onboarding helper that resolves Telegram config from flags/env/dotenv, prompts for missing values, validates them, and writes them back to `~/.ccgram/.env` before existing shell integration runs. Keep shell integration and launch behavior unchanged.

**Tech Stack:** Click, python-dotenv-compatible dotenv writing, existing notify shell helpers, pytest/CliRunner

---

### Task 1: Cover onboarding behavior with tests

**Files:**
- Modify: `tests/ccgram/test_notify_cmd.py`

- [ ] Add failing tests for prompted first-time install, non-interactive failure, and optional group ID omission.
- [ ] Run only the new onboarding tests and confirm they fail for the expected missing behavior.

### Task 2: Implement Telegram onboarding resolution

**Files:**
- Create: `src/ccgram/notify_onboarding.py`
- Modify: `src/ccgram/notify_cmd.py`

- [ ] Add a helper for loading current Telegram config, prompting for missing values, validating them, and persisting them to dotenv.
- [ ] Update `notify install` to use that helper before shell integration.
- [ ] Add CLI flags for explicit non-interactive setup.

### Task 3: Update docs and verify

**Files:**
- Modify: `README.md`

- [ ] Update quick start and notify install docs to describe guided onboarding.
- [ ] Run the notify-focused verification matrix and confirm it passes.
- [ ] Push the updated fork branch only after verification is clean again.
