---
name: faiz-dash
description: Use when the user invokes /faiz-dash or asks inside a session to inspect Faiz quota/reset countdowns, 24h model burn, Cloud/GCE/Drive sync status, Mailbox tasks, APM telemetry, or toggle the on-demand Rex sidebar/dock without interrupting the active conversation.
---

# Faiz Dash In-Session Skill

Provides zero-clutter, on-demand access to the Faiz Control Center via two channels:
1. **In-chat bubble diagnostic** (`faiz-dash inspect`): zero terminal width overhead.
2. **On-demand Rex split pane** (`faiz-dash side` / `faiz-dash dock`): opens or closes a temporary Rex TUI pane without taking focus away from the active session.

## Command Routing

Route based on user arguments or intent:

- **`/faiz-dash` (default / inspect cached snapshots)**
  Run:
  ```bash
  faiz-dash inspect
  ```
  Present the Markdown diagnostic output cleanly in your reply bubble. Do not run an interactive TUI inside the agent command runner.

- **`/faiz-dash live` (refresh probes first)**
  Run:
  ```bash
  faiz-dash inspect --live
  ```
  Wait for the probe snapshot to finish, then present the updated Markdown diagnostic report.

- **`/faiz-dash json`**
  Run:
  ```bash
  faiz-dash inspect --json
  ```
  Return or summarize the exact JSON payload fields requested.

- **`/faiz-dash side` (toggle right sidebar in Rex)**
  Run:
  ```bash
  faiz-dash side
  ```
  Report whether the right sidebar pane (`faiz-dash-side`, 28% width, focus kept on chat) was opened or closed, or relay the macOS Terminal fallback note if not running inside Rex.

- **`/faiz-dash dock` / `/faiz-dash off` / `/faiz-dash on` / `/faiz-dash status`**
  Run the matching `faiz-dash <subcmd>` and report the stdout line concisely.

## Safety Rules

- Never launch interactive `faiz-dash` or `faiz-dash --preset 2` directly inside a headless/agent `run_command` call (it blocks on Textual stdin). Always use `faiz-dash inspect` for in-session display or `faiz-dash side` / `faiz-dash dock` to spawn the Rex split pane.
- Never print raw secrets or bypass backoff cooldowns; rely on `faiz-dash inspect` output which already applies `sanitize_for_json` and `cloud_backoff.json` cooldown reporting.
