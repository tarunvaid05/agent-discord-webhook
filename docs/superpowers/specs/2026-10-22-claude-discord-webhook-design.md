# claude-discord-webhook — design

Date: 2026-10-22
Status: draft, awaiting approval

## Intent

Get a Discord push notification on your phone when any locally running Claude
Code or Codex CLI session finishes a turn or is blocked waiting on you. You run
1–5 sessions at once, sometimes several in the same directory, so each
notification must say which session it is about and show the tail of what the
agent said.

Nothing existing does this well: desktop notifiers (code-notify) mirror a
fixed "task completed" string to Discord with no excerpt or session identity;
the official Discord channel plugin is an inbound chat bridge, per-session,
and breaks with several sessions on one bot. SMS was ruled out: US carrier
email gateways are dead and Twilio needs paid registration. Discord webhooks
are free, need no bot, and the Discord mobile app pushes channel messages.

## Goals

- One-way notifications to a single Discord webhook, from both Claude Code and
  Codex CLI, with one shared hook implementation.
- Each message identifies the session: tool, directory, and a short stable
  numeric alias so two sessions in the same directory are distinguishable.
- Body shows the last ~100 characters of the agent's final message, or the
  whole message when it is short.
- Blockers (permission prompts, questions to the user, idle waiting) are
  reported as such, with what the agent is waiting on.
- Several sessions finishing at the same time produce one combined message,
  not three.
- Does not spam while you are sitting at the terminal: short, interactive
  turns are suppressed by default.
- One-command install that merges hooks into the existing Claude and Codex
  configs without clobbering hooks already there.

## Non-goals

- Replying from the phone / injecting input into sessions. Decided out.
- SMS, Telegram, Slack, desktop toasts, sounds, quota/usage alerts.
- Per-tool-call "progress" messages.
- Subagent events (`SubagentStop`, `SubagentStart`). Ignored to avoid noise.
- Supporting Gemini CLI or other agents (structure allows it later; not built).

## Anti-goals (would make it a failure even if it "works")

- Losing a blocker notification because a batching window or a mute swallowed
  it. Blockers always send.
- Posting secrets: never log or echo the webhook URL; never include tool input
  beyond a truncated command summary.
- Slowing the agent: hooks must return instantly from the agent's point of
  view (async hooks, no network on the agent's critical path).
- Breaking the user's existing hooks in `~/.claude/settings.json` or
  `~/.codex/hooks.json` on install or uninstall.

## Constraints

- Linux host (Ubuntu-class), Python 3.12 available, `uv` available. No Node
  dependency.
- Claude Code 2.1.x hook contract: JSON on stdin with common fields
  `session_id`, `cwd`, `transcript_path`, `hook_event_name`; `Stop` adds
  `last_assistant_message`; `Notification` adds `message` and supports
  matchers `permission_prompt`, `idle_prompt`, `agent_needs_input`,
  `elicitation_dialog`. `SessionEnd` has matchers (`clear`, `logout`,
  `prompt_input_exit`, `other`). Default hook budget is short (seconds), so
  hooks run with `"async": true` and a timeout of 30 seconds.
- Codex CLI 0.145 hook contract (`~/.codex/hooks.json`, same JSON-on-stdin
  shape): events `Stop` (with `last_assistant_message`), `PermissionRequest`
  (with `tool_name`, `tool_input`), `UserPromptSubmit`, `SessionStart`,
  `SessionEnd`, `Interrupt`. No idle/question event exists in Codex; only
  permission prompts are detectable as blockers there. Command hooks support
  `async`. Hooks are enabled by default in 0.145 (`features.hooks = true`
  already set on this machine).
- Discord webhook: POST JSON `{"content": ...}` to the URL, 2000-char content
  limit, HTTP 429 with `retry_after` on rate limit, HTTP 204 on success.
- The user's existing `~/.claude/settings.json` already has a `Stop` hook
  (progress logger) and a `UserPromptSubmit` hook (context check). Install
  must append, not replace.

## How it is made (decided)

### Shape: no daemon

Original idea had an always-running server. Without replies there is nothing
for a server to listen to, so there is none. Each hook invocation is a short
Python process:

1. Read the event JSON from stdin.
2. Update per-session state on disk.
3. Decide whether this event produces a notification.
4. If so, append it to a spool directory, then try to become the "flusher":
   take a file lock; the winner sleeps for the batch window, drains the spool
   into one Discord message, posts it, releases the lock. Losers exit at once.

Hooks are registered `async`, so the sleep never blocks the agent. State lives
in `~/.local/state/claude-discord-webhook/`:

- `sessions.json` — map of `session_id` → `{alias, tool, cwd, last_prompt_at,
  last_seen_at}`.
- `spool/` — one JSON file per pending notification.
- `lock` — flock file for the flusher.
- `mute_until` — optional file holding a Unix timestamp.

### Session identity and aliases

- A session is keyed by `session_id` from the hook payload (both tools provide
  it).
- On first sight of a session the lowest free positive integer not held by any
  live session is assigned as its alias. Aliases are released on `SessionEnd`
  and when a session has not been seen for 12 hours (stale sweep on every
  invocation).
- `SessionStart` with matcher `clear`/`resume`/`compact` keeps the alias (same
  `session_id`); a brand-new `session_id` gets a new alias.
- Display name is the basename of `cwd`. Two sessions in the same directory
  differ by alias and by their message tail.

### Which events notify

| Source | Event | Notifies as |
|---|---|---|
| Claude | `Stop` | done (subject to short-turn suppression) |
| Claude | `Notification` / `permission_prompt` | blocked: permission |
| Claude | `Notification` / `idle_prompt` | blocked: waiting for input |
| Claude | `Notification` / `agent_needs_input`, `elicitation_dialog` | blocked: question |
| Codex | `Stop` | done (subject to short-turn suppression) |
| Codex | `PermissionRequest` | blocked: permission (`tool_name` + truncated command) |
| both | `UserPromptSubmit` | no message; records `last_prompt_at` |
| both | `SessionStart`, `SessionEnd` | no message; alias bookkeeping |

Everything else, including subagent events, is not registered.

### Short-turn suppression ("am I at the keyboard?")

A `done` event is dropped when the turn took less than `min_turn_seconds`
(default 45), measured from the session's `last_prompt_at` to the `Stop`.
Reasoning: if you asked something and it answered in 20 seconds you are still
looking at it; if it ran for 5 minutes you probably walked away. Blocked
events are never suppressed. `always_notify = true` in config turns the
heuristic off. If no `last_prompt_at` is known (hook installed mid-session),
the event is sent.

### Mute

`cdw mute 2h` writes `mute_until`; `cdw unmute` removes it. While muted,
`done` events are dropped and blocked events still send. Builder's call
whether mute also covers blockers via a flag; default is as stated.

### Message format

One Discord message per flush. Each session contributes a block:

```
**[2] claude · claude-text-updates** ✅ done · 4m12s
…wrote the migration and ran the tests, 14 passed. Ready for review.
```

```
**[1] codex · compas** ⏸ needs permission
Bash: rm -rf build/ && make all
```

```
**[3] claude · claude-text-updates** ❓ waiting for your answer
Which auth provider should the login page use?
```

Rules:

- Header: bold, `[alias] tool · dirname`, then a status glyph and label.
  Duration shown for `done` when `last_prompt_at` is known.
- Body for `done`: the agent's `last_assistant_message` with whitespace
  collapsed. If it is at most `tail_chars` (default 100) characters, the whole
  thing; otherwise the last `tail_chars` characters prefixed with `…`, cut at a
  word boundary when one exists within the last 20 characters.
- Body for blocked: the `message` field (Claude) or `tool_name` plus a
  one-line summary of `tool_input` truncated to `tail_chars` (Codex).
- Discord markdown in the agent's text is left alone except that code fences
  are stripped to backticks so a half-fence cannot swallow the rest of the
  message.
- Blocks are separated by a blank line. If the combined content would exceed
  2000 characters, split into several posts in order.
- Webhook `username` is `Claude` or `Codex` by source when all blocks share a
  source, otherwise `Agents`.

### Batching

`batch_window_seconds` (default 3). The flusher sleeps this long after
acquiring the lock, then drains everything in the spool. A hook that arrives
while a flusher is sleeping just drops its file in the spool and exits; the
flusher picks it up. If the flusher's post fails after retries, spool files
stay on disk and the next invocation retries them.

### Delivery and failure handling

- POST with `urllib` (stdlib), 10-second timeout.
- On 429, sleep `retry_after` and retry once. On 5xx or network error, retry
  twice with 2-second backoff, then leave the spool files in place and exit 0.
- Hooks always exit 0 and print nothing to stdout; failures go to a log file
  `~/.local/state/claude-discord-webhook/log` (rotated at 1 MB, one backup).
  Never block or alter the agent's flow.

### Configuration

`~/.config/claude-discord-webhook/config.toml`, created by `cdw install` with
mode 0600:

```toml
webhook_url = "https://discord.com/api/webhooks/..."
tail_chars = 100
min_turn_seconds = 45
batch_window_seconds = 3
always_notify = false
```

`CDW_WEBHOOK_URL` environment variable overrides the file when set (useful for
tests).

### CLI (`cdw`)

| Command | Does |
|---|---|
| `cdw install [--webhook-url URL]` | Writes config (prompts for URL if absent), merges hook entries into `~/.claude/settings.json` and `~/.codex/hooks.json`, creating files if missing. Idempotent: re-running updates in place. Backs up each file to `<file>.bak-<timestamp>` before writing. |
| `cdw uninstall` | Removes only the entries it added (identified by the command containing `cdw hook`). Leaves config and state. |
| `cdw hook` | The hook entrypoint; reads stdin. Internal, but documented so the settings file is self-explanatory. |
| `cdw test` | Posts a sample two-block message to the webhook. |
| `cdw status` | Shows config path, hook install state per tool, live sessions with aliases, mute state, spool depth. Redacts the webhook URL. |
| `cdw mute <duration>` / `cdw unmute` | As above. Durations like `30m`, `2h`. |

Hook entries written for Claude (`~/.claude/settings.json`), each with
`"type": "command"`, `"command": "cdw hook"`, `"async": true`, `"timeout": 30`:
`Stop`; `Notification` with matcher
`permission_prompt|idle_prompt|agent_needs_input|elicitation_dialog`;
`UserPromptSubmit`; `SessionStart`; `SessionEnd`.

Hook entries written for Codex (`~/.codex/hooks.json`, same shape):
`Stop`, `PermissionRequest`, `UserPromptSubmit`, `SessionStart`, `SessionEnd`.

`cdw` must be on PATH for the hook command to resolve from inside the agents;
install writes the absolute path of the `cdw` executable instead of the bare
name to avoid PATH differences.

### Packaging

- Repo: this directory, `claude-discord-webhook`. Python package
  `claude_discord_webhook/` with modules roughly: `hook.py` (event handling),
  `state.py` (sessions, aliases, spool, lock), `format.py` (pure message
  building), `discord.py` (posting), `install.py`, `cli.py`.
- `pyproject.toml` with a `cdw` console script, no runtime dependencies,
  `requires-python >= 3.12`. Installed with `uv tool install .` (or `pipx`).
- Tests with `pytest` (dev dependency only): pure-function tests for tail
  truncation, alias allocation and release, suppression logic, batching
  drain, config merge/unmerge on realistic settings files; one integration
  test that runs `cdw hook` against a local HTTP server standing in for
  Discord.

## Left to the builder

- Exact file-locking primitive (`fcntl.flock` is fine) and spool file naming.
- Log format.
- Whether `SessionStart` assigns the alias eagerly or the first notifying
  event does. Either is acceptable as long as aliases are stable per session.
- How `cdw install` prompts for the webhook URL when stdin is not a TTY
  (probably: error with instructions).
- README content, beyond: what it does, install, config keys, how to get a
  webhook URL from Discord channel settings, how to enable push for that
  channel in the mobile app.
