# CCGram Notify Guided Onboarding Design

## Goal

Remove the manual `~/.ccgram/.env` editing step from first-time local setup. A user should be able to run `ccgram notify install` and be guided through any missing Telegram configuration before shell integration is installed.

## Product Shape

`ccgram notify install` remains the single happy-path onboarding command.

When required Telegram settings are missing:

- prompt for `TELEGRAM_BOT_TOKEN`
- prompt for `ALLOWED_USERS`
- prompt for `CCGRAM_GROUP_ID` as optional
- validate values before writing
- persist them to `~/.ccgram/.env`
- continue with existing notify shell installation

Automation remains supported through flags so CI or scripted installs do not require prompts.

## Command Behavior

New `ccgram notify install` behavior:

- accepts optional `--bot-token`, `--allowed-users`, and `--group-id`
- accepts `--non-interactive` to fail instead of prompting
- uses existing environment or `~/.ccgram/.env` values when present
- only prompts for missing required values
- writes the resolved values to `~/.ccgram/.env`
- then installs shell interception exactly as before

Validation rules:

- bot token must be non-empty
- allowed users must be comma-separated numeric Telegram user IDs
- group ID may be blank, otherwise must be numeric

## Architecture

Add a small onboarding helper module that owns:

- reading existing Telegram config from env and dotenv
- prompting for missing values
- validating the resolved values
- writing the final values back to dotenv

`notify_cmd.py` should orchestrate this helper before calling the existing shell-install helper.

`notify_shell.py` remains responsible for shell integration state and direct launcher management.

## Testing

Add unit tests covering:

- interactive install prompts for missing values and writes dotenv
- existing dotenv values skip prompts
- non-interactive install fails when required Telegram config is missing
- optional group ID can be omitted
- existing notify shell installation behavior still works after onboarding

## Documentation

Update README quick start so first-time setup points users at `ccgram notify install` rather than manual dotenv editing.
