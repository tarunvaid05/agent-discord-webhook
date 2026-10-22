# agent-discord-webhook — design

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
  beyond a truncated command summary. Post with `allowed_mentions: {parse: []}`
  so agent text containing `@everyone` pings nobody.
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
in `~/.local/state/agent-discord-webhook/`:

- `sessions.json` — map of `session_id` → `{alias, tool, cwd, last_prompt_at,
  last_seen_at, last_stop_at, last_assistant_message, notified_for_stop}`.
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
| Claude | `Notification` / `idle_prompt` | late done (see suppression); never a blocker |
| Claude | `Notification` / `agent_needs_input`, `elicitation_dialog` | blocked: question |
| Codex | `Stop` | done (subject to short-turn suppression) |
| Codex | `PermissionRequest` | blocked: permission (`tool_name` + truncated command) |
| both | `UserPromptSubmit` | no message; records `last_prompt_at` |
| both | `SessionStart`, `SessionEnd` | no message; alias bookkeeping |

Everything else, including subagent events, is not registered.

### Short-turn suppression and idle follow-up ("am I at the keyboard?")

Per session, every `Stop` stores `last_assistant_message`, `last_stop_at`, and
`notified_for_stop = false`.

- If the turn took at least `min_turn_seconds` (default 45), measured from
  `last_prompt_at` to the `Stop`, send `done` now and set `notified_for_stop`.
  If `last_prompt_at` is unknown (hook installed mid-session), send.
- Otherwise send nothing yet. You probably asked something quick and are still
  looking at the answer.
- Claude's `idle_prompt` fires about 60 seconds after a turn ends with no new
  prompt. On `idle_prompt`: if `notified_for_stop` is false, send `done` using
  the stored tail (label it `done`, duration from the stored timestamps) and
  set the flag; if already true, drop it. So a short turn you walked away from
  is reported a minute later, and a long turn is never reported twice.
- Codex has no idle event, so Codex short turns that you walk away from are
  not reported. Accepted; `always_notify = true` covers Codex users who want
  every turn.
- A prose question from the agent ("Which auth provider should I use?") is a
  plain `Stop`, not a Notification. It is reported as `done`; the tail shows
  the question. No special detection.
- Blocked events (`permission_prompt`, `agent_needs_input`,
  `elicitation_dialog`, Codex `PermissionRequest`) are never suppressed and
  do not touch the flag.

Both the immediate and the idle path go through the same spool and batching.

### Headless and nested sessions are ignored

Decided: sessions nobody is watching must not notify or consume an alias.
Cases: `claude -p`, `codex exec`, cron/scripts, and one agent spawning another
(Claude running Codex as a reviewer). The hook treats a session as headless if
any of these hold, checked in this order, and then exits without touching
state:

1. **Claude environment (verified on this machine, Claude Code 2.1.307):**
   under `claude -p` the hook process sees `CLAUDE_CODE_ENTRYPOINT=sdk-cli`,
   `CLAUDE_CODE_SESSION_ATTENDED=0`, and `CLAUDE_CODE_CHILD_SESSION=1`.
   Headless if `CLAUDE_CODE_SESSION_ATTENDED` is `0`, or
   `CLAUDE_CODE_CHILD_SESSION` is `1`, or `CLAUDE_CODE_ENTRYPOINT` is
   `sdk-cli`. The builder must confirm an interactive session reports
   `CLAUDE_CODE_SESSION_ATTENDED=1` (or unset) by installing a stdin/env dump
   hook and running one interactive turn; this is the first task of the plan.
2. **Process ancestry (both tools):** walk `/proc/<pid>/stat` parent chain
   from the hook process. The first `claude` or `codex` process found is the
   session's own host. If a second `claude` or `codex` process appears further
   up the chain, the session is nested inside another agent: headless. Match
   on the process `comm` name; the `cmdline` may be `node .../claude`, so also
   match a cmdline whose first path component ends in `/claude` or `/codex`.
   Verified: a process spawned by Claude's Bash tool has `claude` as an
   ancestor and no controlling terminal.
3. **Codex exec:** no environment signal verified yet. An isolated
   `CODEX_HOME` test could not run (model metadata missing in the isolated
   home, and the hooks never fired there). The builder must run `codex exec`
   with a dump hook in the real `~/.codex/hooks.json` and record which of
   these distinguish it: a `source`/`originator` value in the `SessionStart`
   payload (the session file shows `originator: codex_exec`, `source: exec`),
   any `CODEX_*` environment variable, or absence of a controlling terminal
   (`open('/dev/tty')` fails with `ENXIO`). Use the payload field if present,
   else the environment, else the terminal check. Note that the terminal
   check alone is unsafe until confirmed, because Claude's own hooks also run
   without `/dev/tty` when spawned by its Bash tool.

A `skip_cwd_patterns` list in config (glob on `cwd`) is an escape hatch for
anything the heuristics miss. Headless detection is logged at debug level so
a wrongly-skipped session can be diagnosed with `adw status --log`.

### Mute

`adw mute 2h` writes `mute_until`; `adw unmute` removes it. While muted,
`done` events (immediate and idle follow-up) are dropped and blocked events
still send. `adw mute 2h --all` also drops blockers.

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
**[3] claude · claude-text-updates** ❓ needs your answer
Claude is waiting for your response to a question.
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
  `~/.local/state/agent-discord-webhook/log` (rotated at 1 MB, one backup).
  Never block or alter the agent's flow.

### Configuration

`~/.config/agent-discord-webhook/config.toml`, created by `adw install` with
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

### CLI (`adw`)

| Command | Does |
|---|---|
| `adw install [--webhook-url URL]` | Writes config (prompts for URL if absent), merges hook entries into `~/.claude/settings.json` and `~/.codex/hooks.json`, creating files if missing. Idempotent: re-running updates in place. Backs up each file to `<file>.bak-<timestamp>` before writing. |
| `adw uninstall` | Removes only the entries it added (identified by the command containing `adw hook`). Leaves config and state. |
| `adw hook` | The hook entrypoint; reads stdin. Internal, but documented so the settings file is self-explanatory. |
| `adw test` | Posts a sample two-block message to the webhook. |
| `adw status` | Shows config path, hook install state per tool, live sessions with aliases, mute state, spool depth. Redacts the webhook URL. |
| `adw mute <duration>` / `adw unmute` | As above. Durations like `30m`, `2h`. |

Hook entries written for Claude (`~/.claude/settings.json`), each with
`"type": "command"`, `"command": "adw hook"`, `"async": true`, `"timeout": 30`:
`Stop`; `Notification` with matcher
`permission_prompt|idle_prompt|agent_needs_input|elicitation_dialog`;
`UserPromptSubmit`; `SessionStart`; `SessionEnd`.

Hook entries written for Codex (`~/.codex/hooks.json`, same shape):
`Stop`, `PermissionRequest`, `UserPromptSubmit`, `SessionStart`, `SessionEnd`.

`adw` must be on PATH for the hook command to resolve from inside the agents;
install writes the absolute path of the `adw` executable instead of the bare
name to avoid PATH differences.

### Packaging

- Repo: this directory, `agent-discord-webhook`. Python package
  `agent_discord_webhook/` with modules roughly: `hook.py` (event handling),
  `state.py` (sessions, aliases, spool, lock), `format.py` (pure message
  building), `discord.py` (posting), `install.py`, `cli.py`.
- `pyproject.toml` with a `adw` console script, no runtime dependencies,
  `requires-python >= 3.12`. Installed with `uv tool install .` (or `pipx`).
- Tests with `pytest` (dev dependency only): pure-function tests for tail
  truncation, alias allocation and release, suppression logic, batching
  drain, config merge/unmerge on realistic settings files; one integration
  test that runs `adw hook` against a local HTTP server standing in for
  Discord.

## Left to the builder

- Exact file-locking primitive (`fcntl.flock` is fine) and spool file naming.
- Log format.
- Whether `SessionStart` assigns the alias eagerly or the first notifying
  event does. Either is acceptable as long as aliases are stable per session.
- How `adw install` prompts for the webhook URL when stdin is not a TTY
  (probably: error with instructions).
- README content, beyond: what it does, install, config keys, how to get a
  webhook URL from Discord channel settings, how to enable push for that
  channel in the mobile app.
