# agent-discord-webhook

Push notifications on your phone when a local Claude Code or Codex CLI session
finishes a turn or is waiting on you. Each message names the session and shows
the tail of what the agent said. Several sessions finishing together become one
message. Short interactive turns are not reported unless you walk away.

```
**[2] claude · my-project** ✅ done · 4m12s
…wrote the migration and ran the tests, 14 passed. Ready for review.

**[1] codex · my-project** ⏸ needs permission
Bash: rm -rf build/ && make all
```

One-way only. There is no daemon: each hook event runs a short `adw` process
that records state, spools the notification, and hands off to a detached
flusher that posts to a Discord webhook.

## Install

Requires Linux, Python 3.12, and `uv` (or `pipx`).

```
uv tool install .
adw install --webhook-url https://discord.com/api/webhooks/...
```

`adw install` writes `~/.config/agent-discord-webhook/config.toml` (mode 0600)
and adds hook entries to `~/.claude/settings.json` and `~/.codex/hooks.json`.
Existing hooks are kept. Each file is backed up to `<file>.bak-<timestamp>`
first. Re-running updates the hook entries in place and keeps your config,
replacing only the `webhook_url` line; without `--webhook-url` it reuses the
configured URL. The URL must start with `https://`. Run `adw test` to post a
sample message.

Codex asks you to trust new hooks the next time it starts ("Hooks need
review"; press `t` to trust all). It asks again after any change to the
entries, including re-running `adw install` from a new `adw` path. Claude Code
picks the hooks up in sessions started after the install.

When upgrading from a checkout, use `uv tool install . --reinstall`; a plain
reinstall of the same version can reuse a cached build.

### Getting a webhook URL

In Discord: open the channel you want notifications in, Edit Channel,
Integrations, Webhooks, New Webhook, Copy Webhook URL. Treat the URL as a
secret: anyone holding it can post to that channel.

### Push on your phone

In the Discord mobile app, open the channel, tap its name, Notification
Settings, and choose All Messages. Make sure the server is not muted.

## Configuration

`~/.config/agent-discord-webhook/config.toml`:

| Key | Default | Meaning |
|---|---|---|
| `webhook_url` | | Discord webhook URL. `ADW_WEBHOOK_URL` in the environment overrides it. |
| `tail_chars` | `100` | How many trailing characters of the agent's last message to show. |
| `min_turn_seconds` | `45` | Turns shorter than this are not reported immediately. |
| `batch_window_seconds` | `3` | How long the flusher waits to combine notifications from several sessions. |
| `always_notify` | `false` | Report every turn regardless of length. |
| `skip_cwd_patterns` | `[]` | Glob patterns on the session's working directory; matching sessions never notify. |

## Commands

| Command | Does |
|---|---|
| `adw install [--webhook-url URL]` | Writes config, merges hooks into both agents' settings files. Without the flag, reuses the configured URL or prompts for one when run interactively. |
| `adw uninstall` | Removes only the hook entries adw added. Leaves config and state. |
| `adw test` | Posts a sample two-block message. |
| `adw status [--log]` | Config path, hook install state per tool, live sessions with aliases, mute state, spool depth. `--log` appends the last 50 log lines. |
| `adw mute <duration> [--all]` | Silence `done` notifications for `30m`, `2h`, `1d`. `--all` also silences permission prompts and questions. |
| `adw unmute` | Clear the mute. |
| `adw hook --tool claude\|codex` | The hook entrypoint. Reads the event JSON on stdin. Installed by `adw install`. |
| `adw flush` | Internal. Waits the batch window and drains the spool. |

## What gets reported

| Agent | Event | Message |
|---|---|---|
| Claude Code | turn ends | `✅ done` with duration and message tail |
| Claude Code | permission prompt | `⏸ needs permission` |
| Claude Code | question or elicitation dialog | `❓ needs your answer` |
| Codex CLI | turn ends | `✅ done` |
| Codex CLI | permission request | `⏸ needs permission` with the tool and command (not yet: Codex 0.145 shows the approval dialog without firing `PermissionRequest`) |

Suppression: a turn shorter than `min_turn_seconds` is not reported when it
ends, because you probably asked something quick and are still looking at
the answer. Claude Code fires an idle notification about a minute later if
you have not typed anything, and that sends the pending `done`. A long turn is
never reported twice. Codex has no idle event, so short Codex turns are only
reported with `always_notify = true`.

Permission prompts and questions are never suppressed and still send while
muted, unless you used `adw mute --all`.

Sessions nobody is watching do not notify: `claude -p` (from Claude's own
environment variables), `codex exec` (from the `exec` subcommand on the codex
process; its hook payload looks the same as an interactive one), and an agent
started from inside another agent (from the process tree). Use
`skip_cwd_patterns` for anything the detection misses, and `adw status --log`
to see why a session was skipped.

## Where things live

- Config: `~/.config/agent-discord-webhook/config.toml`
- State: `~/.local/state/agent-discord-webhook/` with `sessions.json`,
  `spool/` (pending notifications), `lock`, `mute_until`, and `log`
  (rotated at 1 MB). The webhook URL never appears in the log.

## Uninstall

```
adw uninstall
uv tool uninstall agent-discord-webhook
rm -r ~/.config/agent-discord-webhook ~/.local/state/agent-discord-webhook
```

## Development

```
uv sync
uv run pytest
```
