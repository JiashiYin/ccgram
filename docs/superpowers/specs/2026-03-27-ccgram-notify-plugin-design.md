# CCGram Notify Plugin Design

## Goal

Ship a real open source `ccgram` fork with:

- provider-agnostic `interactive` / `notify` notification modes
- a Codex-first setup path
- one-time setup after which plain `codex` launches default into `notify`
- proactive Telegram delivery for blocking prompts and explicit milestone/final summaries
- reversible disable and uninstall flows

## Product Shape

This is a hybrid product:

- the `ccgram` fork owns the bridge/runtime behavior
- the plugin/setup layer owns installation, shell integration, doctor checks, and user ergonomics

It is not a wrapper-only side project.

## Core Modes

### Interactive

Used for Telegram-origin sessions:

- chatty transcript delivery
- normal status updates
- interactive UI routing

### Notify

Used for unattended or shell-started sessions:

- surface blocking prompts that need action
- surface dead/failure notices
- surface explicit `[CCGRAM_MILESTONE]` summaries
- surface explicit `[CCGRAM_FINAL]` summaries
- suppress routine chatter

## Session Origins

### Telegram-Origin

- created or rebound from Telegram
- defaults to `interactive`

### Local / External-Origin

- created outside Telegram but inside the monitored tmux workflow
- defaults to `notify`

## Shell Integration

The product installs a shell snippet that wraps plain `codex`.

Requirements:

- opt-in and reversible
- preserve a bypass path such as `codex-direct`
- avoid recursion when `ccgram` launches Codex inside tmux
- support at least `bash`, `zsh`, and `fish`

Implementation:

- write a sourced shell snippet under `CCGRAM_DIR`
- write a `codex-direct` launcher under `CCGRAM_DIR/bin`
- persist `CCGRAM_CODEX_COMMAND=<direct-launcher>` in `CCGRAM_DIR/.env`
- let the shell wrapper call `ccgram notify launch --provider codex`

## Notify Contract

Blocking delivery is driven by provider parsers exposing `is_interactive=True`.

Explicit summaries must use these markers:

```text
[CCGRAM_MILESTONE] ...
[CCGRAM_FINAL] ...
```

The bridge strips the marker before Telegram delivery.

## CLI Surface

### `ccgram notify install`

- writes shell integration
- writes direct launcher
- persists `CCGRAM_CODEX_COMMAND`
- enables default `notify` flow for normal `codex` launches

### `ccgram notify status`

- reports whether notify integration is enabled
- reports configured shell and mode

### `ccgram notify disable`

- removes the shell rc hook
- keeps generated artifacts and env override

### `ccgram notify uninstall`

- removes shell rc hook
- removes generated artifacts
- removes the `CCGRAM_CODEX_COMMAND` override

### `ccgram notify launch`

- creates a tmux-managed provider window
- stamps pane title
- records provider name and notification mode
- optionally attaches the local terminal to the tmux session

## Documentation Requirements

The shipped README must explain:

- interactive vs notify
- GitHub install path
- `ccgram notify install / status / disable / uninstall`
- default notify behavior for plain `codex`
- Telegram-opened sessions staying interactive
- milestone/final summary markers

## Verification

Before calling the work done:

- focused tests for session modes, notify routing, and notify CLI pass
- lint passes on touched files
- the repo can be reinstalled from scratch following the README flow
