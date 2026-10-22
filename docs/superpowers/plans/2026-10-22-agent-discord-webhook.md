# agent-discord-webhook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Discord push notifications from local Claude Code and Codex CLI sessions when a turn finishes or the agent is blocked, with session identity, message tail, batching, suppression, and a one-command installer.

**Architecture:** No daemon. Each hook invocation is a short `adw hook` Python process: read event JSON from stdin, reject headless sessions, update `sessions.json` under a file lock, decide whether to notify, drop a spool file, and race for the flusher lock. The flusher sleeps the batch window, drains the spool into one Discord post, and deletes the spool files it delivered. Pure decision and formatting logic lives in `hook.py` and `format.py` with no I/O so it is unit-tested without a filesystem or network.

**Tech Stack:** Python 3.12 stdlib only (`tomllib`, `urllib`, `fcntl`, `json`, `argparse`, `logging`). `pytest` as the only dev dependency. `uv` for env and `uv tool install` for deployment.

**Spec:** `docs/superpowers/specs/2026-10-22-agent-discord-webhook-design.md`

## Global Constraints

- `requires-python >= 3.12`; no runtime dependencies; `pytest` dev-only.
- Hooks always exit 0 and print nothing to stdout. All failures go to the log file.
- Never log, print, or echo the webhook URL. `adw status` redacts it.
- Every Discord POST carries `"allowed_mentions": {"parse": []}`.
- Hook entries written to agents use `"type": "command"`, `"async": true`, `"timeout": 30`, and the absolute path of the `adw` executable.
- Install appends to existing hooks; uninstall removes only entries whose command contains `adw hook`.
- Config file `~/.config/agent-discord-webhook/config.toml` mode 0600. Defaults: `tail_chars = 100`, `min_turn_seconds = 45`, `batch_window_seconds = 3`, `always_notify = false`.
- State dir `~/.local/state/agent-discord-webhook/` with `sessions.json`, `spool/`, `lock`, `mute_until`, `log` (rotated at 1 MB, one backup).
- Discord: 2000-char content limit, 204 success, 429 with `retry_after`.
- Blockers always send unless `mute --all`. `done` never sends while muted.

**Decisions this plan makes where the spec left them open or is stale:**

- Env override for the webhook URL is `ADW_WEBHOOK_URL` (spec says `CDW_WEBHOOK_URL`, a leftover from the pre-rename name). Two more env overrides exist for tests: `ADW_CONFIG_PATH`, `ADW_STATE_DIR`.
- The hook command written to agents is `<abs>/adw hook --tool claude` or `--tool codex`, so the hook knows its source without guessing. Still contains `adw hook` for uninstall detection.
- Alias assigned on first sight of a non-headless session, whichever event that is (`SessionStart` in practice).
- Locks: `fcntl.flock`. `state.lock` held briefly around every sessions.json read-modify-write; `lock` is the flusher lock (`LOCK_EX | LOCK_NB`).
- Spool file name: `<time_ns>-<pid>.json`; lexicographic order is chronological.
- Log format: `%(asctime)s %(levelname)s %(message)s`.
- Non-TTY `adw install` without `--webhook-url`: exit 2 with message `webhook URL required: pass --webhook-url or run interactively`.
- Non-429 4xx from Discord (bad URL, deleted webhook): drop the spool files and log an error, instead of retrying forever.
- Blocked-event bodies are hard-capped at 500 chars so a long Claude `message` cannot produce an oversized block.

## Review Focus

1. `Stop` payload with empty or missing `last_assistant_message` (user interrupted, or tool-only turn): must produce a `done` block with body `(no message)`, not crash. Test in Task 7.
2. `sessions.json` corrupt or truncated (two hooks raced before the lock existed, disk full): `SessionStore.load` returns an empty map and logs a warning; the hook still notifies. Test in Task 4.
3. A corrupt spool file: `spool_drain` skips it and deletes it, so one bad file never jams every later flush. Test in Task 5.
4. A failed POST must not leak the webhook URL into the log, including `URLError` text that embeds the URL. Test in Task 8.
5. A single block whose rendered text exceeds 2000 chars (long permission message) must be hard-truncated to fit one post, not raise or loop in `render_posts`. Test in Task 3.

---

### Task 1: Verify hook signals on this machine (no code)

**Files:**
- Modify: `docs/superpowers/specs/2026-10-22-agent-discord-webhook-design.md:161-200` (headless section)
- Create (temporary, deleted at end of task): `/tmp/adw-dump.sh`

**Interfaces:**
- Produces: four facts recorded in the spec that Task 6 and Task 7 read: (a) the value of `CLAUDE_CODE_SESSION_ATTENDED` in an interactive Claude session, (b) the Claude `Notification` payload field that carries the matcher value (expected `notification_type`), (c) the Codex `hooks.json` top-level shape that actually fires, (d) which signal distinguishes `codex exec` from interactive `codex`.

- [ ] **Step 1: Write the dump hook**

```bash
cat > /tmp/adw-dump.sh <<'EOF'
#!/bin/sh
{ date -Is; echo "ARGS: $*"; echo "ENV:"; env | grep -E '^(CLAUDE|CODEX)' ; echo "TTY:"; (exec 3</dev/tty && echo yes) 2>&1 || echo no; echo "STDIN:"; cat; echo; echo ====; } >> /tmp/adw-dump.log
exit 0
EOF
chmod +x /tmp/adw-dump.sh
```

- [ ] **Step 2: Register it in Claude for one interactive turn**

Back up `~/.claude/settings.json` to `~/.claude/settings.json.bak-task1`. Add to `hooks`: a `Notification` group with matcher `permission_prompt|idle_prompt|agent_needs_input|elicitation_dialog` and a `SessionStart` group, both with `{"type":"command","command":"/tmp/adw-dump.sh","async":true,"timeout":30}`. Keep the existing `Stop` and `UserPromptSubmit` groups.

- [ ] **Step 3: Run one interactive turn, one permission prompt, and wait for idle**

In a new terminal: `claude`, ask it to run `ls /` with a tool that prompts (or any command outside allowlist), approve, then wait 90 seconds without typing.
Expected: `/tmp/adw-dump.log` contains a `SessionStart` entry, a `Notification` entry with the permission payload, and a `Notification` entry for idle. Record `CLAUDE_CODE_SESSION_ATTENDED` value (expect `1` or absent) and the name of the payload key holding `permission_prompt` / `idle_prompt`.

- [ ] **Step 4: Register it in Codex and run `codex exec` plus one interactive turn**

Write `~/.codex/hooks.json`:

```json
{"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "/tmp/adw-dump.sh", "async": true, "timeout": 30}]}],
           "Stop": [{"hooks": [{"type": "command", "command": "/tmp/adw-dump.sh", "async": true, "timeout": 30}]}]}}
```

Run: `codex exec "say hi"` then an interactive `codex` turn.
Expected: both fire. If nothing fires, try the same content without the outer `hooks` key and record which shape works. Record for `codex exec`: `source`/`originator` in the `SessionStart` payload, any `CODEX_*` env var, and the `TTY:` line. Record the same three for interactive.

- [ ] **Step 5: Record findings in the spec**

Replace the "must confirm" and "no environment signal verified yet" sentences in the headless section with the observed values, and add one line to the Constraints section giving the Codex `hooks.json` shape and the Claude `Notification` matcher field name. If `codex exec` shows no distinguishing signal at all, write that, and Task 6 falls back to the `/dev/tty` check for Codex only.

- [ ] **Step 6: Restore and clean up**

Restore `~/.claude/settings.json` from the backup, delete `~/.codex/hooks.json`, delete `/tmp/adw-dump.sh` and `/tmp/adw-dump.log`.

- [ ] **Step 7: Commit**

```bash
git add docs/superpowers/specs/2026-10-22-agent-discord-webhook-design.md
git commit -m "docs: record verified hook signals for Claude and Codex"
```

---

### Task 2: Package scaffold and config

**Files:**
- Create: `pyproject.toml`, `agent_discord_webhook/__init__.py`, `agent_discord_webhook/config.py`, `tests/__init__.py`, `tests/test_config.py`, `.gitignore`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) Config(webhook_url: str | None, tail_chars: int = 100, min_turn_seconds: int = 45, batch_window_seconds: float = 3.0, always_notify: bool = False, skip_cwd_patterns: tuple[str, ...] = ())`
  - `config_path(env: Mapping[str, str] = os.environ) -> Path` returns `env["ADW_CONFIG_PATH"]` or `~/.config/agent-discord-webhook/config.toml`.
  - `state_dir(env=os.environ) -> Path` returns `env["ADW_STATE_DIR"]` or `~/.local/state/agent-discord-webhook`, created with `spool/` inside (`mkdir(parents=True, exist_ok=True)`).
  - `load_config(env=os.environ) -> Config` reads the TOML at `config_path(env)` if it exists, then `ADW_WEBHOOK_URL` overrides `webhook_url`. An empty-string URL from either source becomes `None`.
  - `get_logger(env=os.environ) -> logging.Logger` named `adw`, one `RotatingFileHandler(state_dir(env) / "log", maxBytes=1_000_000, backupCount=1)`, level DEBUG, format `%(asctime)s %(levelname)s %(message)s`; idempotent (second call adds no handler).

- [ ] **Step 1: Write `pyproject.toml`**

Project name `agent-discord-webhook`, version `0.1.0`, `requires-python = ">=3.12"`, no dependencies, `[project.scripts] adw = "agent_discord_webhook.cli:main"`, `[dependency-groups] dev = ["pytest>=8"]`, build backend `hatchling`. `.gitignore`: `.venv/`, `__pycache__/`, `*.egg-info/`, `dist/`.

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_config.py
def test_defaults_when_no_file(tmp_path):
    env = {"ADW_CONFIG_PATH": str(tmp_path / "config.toml"), "ADW_STATE_DIR": str(tmp_path / "state")}
    cfg = load_config(env)
    assert cfg == Config(webhook_url=None)
    assert cfg.tail_chars == 100 and cfg.min_turn_seconds == 45
    assert cfg.batch_window_seconds == 3.0 and cfg.always_notify is False

def test_file_values_and_env_override(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('webhook_url = "https://discord.com/api/webhooks/1/a"\ntail_chars = 50\nskip_cwd_patterns = ["/tmp/*"]\n')
    cfg = load_config({"ADW_CONFIG_PATH": str(p)})
    assert cfg.webhook_url == "https://discord.com/api/webhooks/1/a"
    assert cfg.tail_chars == 50 and cfg.skip_cwd_patterns == ("/tmp/*",)
    cfg2 = load_config({"ADW_CONFIG_PATH": str(p), "ADW_WEBHOOK_URL": "https://x/y"})
    assert cfg2.webhook_url == "https://x/y"

def test_state_dir_created_with_spool(tmp_path):
    d = state_dir({"ADW_STATE_DIR": str(tmp_path / "s")})
    assert (d / "spool").is_dir()

def test_logger_idempotent_and_writes(tmp_path):
    env = {"ADW_STATE_DIR": str(tmp_path)}
    log = get_logger(env); get_logger(env)
    assert len(log.handlers) == 1
    log.info("hello"); log.handlers[0].flush()
    assert "INFO hello" in (tmp_path / "log").read_text()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv sync && uv run pytest tests/test_config.py -v`
Expected: FAIL with `ImportError` / `ModuleNotFoundError`.

- [ ] **Step 4: Implement `agent_discord_webhook/config.py`**

Use `tomllib`. Unknown TOML keys are ignored. `skip_cwd_patterns` list becomes a tuple.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml .gitignore agent_discord_webhook tests
git commit -m "feat: package scaffold and config loading"
```

---

### Task 3: Message formatting (pure)

**Files:**
- Create: `agent_discord_webhook/format.py`, `tests/test_format.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) Block(alias: int, tool: str, dirname: str, kind: str, body: str, duration_seconds: float | None = None)` where `tool in {"claude", "codex"}` and `kind in {"done", "permission", "question"}`. Has `to_dict()` and `@classmethod from_dict(d)`.
  - `@dataclass(frozen=True) Post(content: str, block_indices: tuple[int, ...])`
  - `collapse_whitespace(s: str) -> str`
  - `strip_code_fences(s: str) -> str` replaces every run of three or more backticks with a single backtick.
  - `tail(s: str, n: int) -> str` per spec: whole string if `len(s) <= n`, else take the last n chars; if a space occurs within the first 20 chars of that slice, drop everything up to and including the first such space; prefix `"…"`.
  - `head(s: str, n: int) -> str` whole string if `len(s) <= n`, else first n chars + `"…"`.
  - `format_duration(seconds: float) -> str` → `"37s"`, `"4m12s"`, `"1h02m"`.
  - `render_block(b: Block) -> str`
  - `render_posts(blocks: Sequence[Block], limit: int = 2000) -> list[Post]`
  - `username_for(blocks: Sequence[Block]) -> str` → `"Claude"`, `"Codex"`, or `"Agents"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_format.py
def test_tail_short_unchanged():
    assert tail("hello world", 100) == "hello world"

def test_tail_cuts_at_word_boundary():
    s = "a" * 50 + " " + "b" * 95
    out = tail(s, 100)
    assert out == "…" + "b" * 95         # boundary at the space, 5 chars after cut
    assert tail("x" * 300, 100) == "…" + "x" * 100   # no boundary: hard cut

def test_head_truncates_with_ellipsis():
    assert head("rm -rf build/ && make all", 10) == "rm -rf bui…"

def test_collapse_and_fences():
    assert collapse_whitespace("a\n\n  b\t c") == "a b c"
    assert strip_code_fences("x ```py\ncode``` y") == "x `py\ncode` y"

def test_format_duration():
    assert format_duration(37) == "37s"
    assert format_duration(252) == "4m12s"
    assert format_duration(3720) == "1h02m"

def test_render_done_block_matches_spec():
    b = Block(2, "claude", "claude-text-updates", "done",
              "…wrote the migration and ran the tests, 14 passed. Ready for review.", 252)
    assert render_block(b) == ("**[2] claude · claude-text-updates** ✅ done · 4m12s\n"
                               "…wrote the migration and ran the tests, 14 passed. Ready for review.")

def test_render_permission_and_question():
    p = Block(1, "codex", "compas", "permission", "Bash: rm -rf build/ && make all")
    assert render_block(p) == "**[1] codex · compas** ⏸ needs permission\nBash: rm -rf build/ && make all"
    q = Block(3, "claude", "x", "question", "Claude is waiting for your response to a question.")
    assert render_block(q).startswith("**[3] claude · x** ❓ needs your answer\n")

def test_done_without_duration_has_no_dot():
    assert render_block(Block(1, "claude", "d", "done", "ok")) == "**[1] claude · d** ✅ done\nok"

def test_render_posts_joins_with_blank_line_and_splits_at_limit():
    blocks = [Block(i, "claude", "d", "done", "x" * 150) for i in range(1, 20)]
    posts = render_posts(blocks, limit=2000)
    assert len(posts) > 1
    assert all(len(p.content) <= 2000 for p in posts)
    assert [i for p in posts for i in p.block_indices] == list(range(19))
    assert "\n\n" in posts[0].content

def test_render_posts_hard_truncates_oversized_block():
    posts = render_posts([Block(1, "claude", "d", "permission", "y" * 3000)], limit=2000)
    assert len(posts) == 1 and len(posts[0].content) <= 2000

def test_username_for():
    c = Block(1, "claude", "d", "done", "a"); x = Block(2, "codex", "d", "done", "a")
    assert username_for([c]) == "Claude" and username_for([x]) == "Codex" and username_for([c, x]) == "Agents"

def test_block_roundtrip():
    b = Block(1, "codex", "d", "permission", "Bash: ls", None)
    assert Block.from_dict(b.to_dict()) == b
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_format.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `agent_discord_webhook/format.py`**

Labels: `done` → `✅ done` plus ` · <duration>` when `duration_seconds` is not None; `permission` → `⏸ needs permission`; `question` → `❓ needs your answer`. `render_posts` greedily packs rendered blocks separated by `\n\n`; a block longer than `limit` alone is cut to `limit - 1` chars plus `…`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_format.py -v`
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add agent_discord_webhook/format.py tests/test_format.py
git commit -m "feat: pure message formatting"
```

---

### Task 4: Session store, aliases, mute

**Files:**
- Create: `agent_discord_webhook/state.py`, `tests/test_state.py`

**Interfaces:**
- Consumes: `state_dir()` from Task 2.
- Produces:
  - `@dataclass Session(alias: int, tool: str, cwd: str, last_seen_at: float, last_prompt_at: float | None = None, last_stop_at: float | None = None, last_assistant_message: str = "", notified_for_stop: bool = False)` with `to_dict()` / `from_dict()`.
  - `STALE_SECONDS = 12 * 3600`
  - `allocate_alias(taken: Iterable[int]) -> int` lowest positive int not in `taken`.
  - `@contextmanager flock(path: Path, blocking: bool = True)` yields the open file; raises `BlockingIOError` when non-blocking and busy.
  - `class SessionStore(state_dir: Path, log: logging.Logger)`:
    - `load() -> dict[str, Session]` (corrupt or missing file → `{}` plus a warning log)
    - `save(sessions: dict[str, Session]) -> None` atomic write via temp file + `os.replace`
    - `@contextmanager locked() -> Iterator[dict[str, Session]]` takes `state.lock`, loads, yields the dict, saves on exit.
    - `touch(sessions, session_id: str, tool: str, cwd: str, now: float) -> Session` assigns alias on first sight, updates `last_seen_at`.
    - `end(sessions, session_id) -> None`
    - `sweep(sessions, now: float) -> None` drops sessions with `now - last_seen_at > STALE_SECONDS`.
  - `set_mute(state_dir: Path, until: float, all_: bool) -> None` writes `mute_until` as `"<until>"` or `"<until> all"`.
  - `clear_mute(state_dir: Path) -> None`
  - `mute_state(state_dir: Path, now: float) -> tuple[bool, bool]` → `(muted, all_)`; expired or missing file → `(False, False)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_state.py
def test_allocate_alias_lowest_free():
    assert allocate_alias([]) == 1
    assert allocate_alias([1, 2, 4]) == 3

def test_touch_assigns_stable_alias_and_releases_on_end(tmp_path):
    store = SessionStore(tmp_path, logging.getLogger("t"))
    s = {}
    a = store.touch(s, "A", "claude", "/p/one", 100.0)
    b = store.touch(s, "B", "codex", "/p/one", 101.0)
    assert (a.alias, b.alias) == (1, 2)
    assert store.touch(s, "A", "claude", "/p/one", 102.0).alias == 1
    store.end(s, "A")
    assert store.touch(s, "C", "claude", "/p/two", 103.0).alias == 1

def test_sweep_drops_stale(tmp_path):
    store = SessionStore(tmp_path, logging.getLogger("t"))
    s = {}
    store.touch(s, "A", "claude", "/p", 0.0)
    store.touch(s, "B", "claude", "/p", 10.0)
    store.sweep(s, 10.0 + 12 * 3600)
    assert list(s) == ["B"]

def test_locked_roundtrip_and_corrupt_file(tmp_path, caplog):
    store = SessionStore(tmp_path, logging.getLogger("t"))
    with store.locked() as s:
        store.touch(s, "A", "claude", "/p", 1.0)
    assert store.load()["A"].alias == 1
    (tmp_path / "sessions.json").write_text("{not json")
    with caplog.at_level(logging.WARNING, logger="t"):
        assert store.load() == {}
    assert "sessions.json" in caplog.text

def test_flock_nonblocking_raises_when_held(tmp_path):
    with flock(tmp_path / "lock"):
        with pytest.raises(BlockingIOError):
            with flock(tmp_path / "lock", blocking=False):
                pass

def test_mute_roundtrip(tmp_path):
    assert mute_state(tmp_path, 0.0) == (False, False)
    set_mute(tmp_path, 100.0, all_=False)
    assert mute_state(tmp_path, 50.0) == (True, False)
    assert mute_state(tmp_path, 150.0) == (False, False)
    set_mute(tmp_path, 100.0, all_=True)
    assert mute_state(tmp_path, 50.0) == (True, True)
    clear_mute(tmp_path)
    assert mute_state(tmp_path, 50.0) == (False, False)
```

Note: `flock` on the same file in one process. Use two separate `open()` calls; `fcntl.flock` locks are per open file description, so the nested call must get `EWOULDBLOCK`. If it does not on this kernel, run the inner acquire in a `subprocess` instead.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_state.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `agent_discord_webhook/state.py`**

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_state.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add agent_discord_webhook/state.py tests/test_state.py
git commit -m "feat: session store, aliases, mute state"
```

---

### Task 5: Spool and flusher lock

**Files:**
- Modify: `agent_discord_webhook/state.py`
- Modify: `tests/test_state.py`

**Interfaces:**
- Consumes: `Block` from Task 3, `flock` from Task 4.
- Produces (in `state.py`):
  - `spool_append(state_dir: Path, block: Block) -> Path` writes `spool/<time_ns>-<pid>.json` containing `block.to_dict()`.
  - `spool_drain(state_dir: Path, log: logging.Logger) -> list[tuple[Path, Block]]` sorted by filename; a file that fails to parse is deleted and logged at WARNING.
  - `spool_depth(state_dir: Path) -> int`
  - `try_flusher_lock(state_dir: Path) -> IO | None` returns the held file object for `lock` or `None` if another process holds it.

- [ ] **Step 1: Write the failing tests**

```python
def test_spool_append_drain_order_and_corrupt_skipped(tmp_path):
    (tmp_path / "spool").mkdir()
    log = logging.getLogger("t")
    p1 = spool_append(tmp_path, Block(1, "claude", "d", "done", "first"))
    p2 = spool_append(tmp_path, Block(2, "codex", "d", "done", "second"))
    bad = tmp_path / "spool" / "0000000000-1.json"; bad.write_text("{")
    assert spool_depth(tmp_path) == 3
    drained = spool_drain(tmp_path, log)
    assert [b.body for _, b in drained] == ["first", "second"]
    assert [p for p, _ in drained] == [p1, p2]
    assert not bad.exists()

def test_try_flusher_lock_exclusive(tmp_path):
    held = try_flusher_lock(tmp_path)
    assert held is not None
    assert try_flusher_lock(tmp_path) is None
    held.close()
    assert try_flusher_lock(tmp_path) is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_state.py -v -k "spool or flusher"`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement the four functions in `state.py`**

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_state.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add agent_discord_webhook/state.py tests/test_state.py
git commit -m "feat: spool files and flusher lock"
```

---

### Task 6: Headless detection

**Files:**
- Create: `agent_discord_webhook/headless.py`, `tests/test_headless.py`

**Interfaces:**
- Consumes: facts recorded by Task 1 in the spec's headless section.
- Produces:
  - `AGENT_NAMES = ("claude", "codex")`
  - `is_agent_process(comm: str, cmdline: str) -> bool` true if `comm in AGENT_NAMES` or the first whitespace-separated cmdline token's basename is in `AGENT_NAMES`, or the first token is `node` and the second token's basename is in `AGENT_NAMES`.
  - `ancestor_agents(pid: int, read_proc: Callable[[int], tuple[int, str, str] | None]) -> list[str]` walks parents via `read_proc(pid) -> (ppid, comm, cmdline)` until pid 0/1 or `None`; returns agent names found in order, nearest first.
  - `default_read_proc(pid: int) -> tuple[int, str, str] | None` reads `/proc/<pid>/stat` (ppid is field 4, comm is between the parens) and `/proc/<pid>/cmdline`.
  - `has_controlling_tty() -> bool` tries `open("/dev/tty")`.
  - `headless_reason(tool: str, payload: dict, env: Mapping[str, str], cwd: str, skip_cwd_patterns: Sequence[str], ancestors: list[str], tty: bool) -> str | None` returns a short reason string or `None`. Pure; `run_hook` passes real values.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_headless.py
def test_claude_env_signals():
    for env in ({"CLAUDE_CODE_SESSION_ATTENDED": "0"}, {"CLAUDE_CODE_CHILD_SESSION": "1"},
                {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}):
        assert headless_reason("claude", {}, env, "/p", (), ["claude"], True) is not None
    assert headless_reason("claude", {}, {"CLAUDE_CODE_SESSION_ATTENDED": "1"}, "/p", (), ["claude"], False) is None
    assert headless_reason("claude", {}, {}, "/p", (), ["claude"], False) is None

def test_nested_agent_is_headless():
    assert headless_reason("codex", {}, {}, "/p", (), ["codex", "claude"], False) == "nested"
    assert headless_reason("claude", {}, {}, "/p", (), ["claude"], False) is None

def test_skip_cwd_patterns():
    assert headless_reason("claude", {}, {}, "/tmp/scratch/x", ("/tmp/scratch/*",), ["claude"], True) == "skip_cwd"

def test_is_agent_process():
    assert is_agent_process("claude", "/usr/bin/claude")
    assert is_agent_process("node", "node /home/u/.local/bin/claude --flag")
    assert not is_agent_process("bash", "/bin/bash")

def test_ancestor_agents_walks_chain():
    procs = {50: (40, "adw", "adw hook"), 40: (30, "node", "node /x/claude"), 30: (20, "bash", "bash"),
             20: (1, "codex", "/y/codex"), 1: (0, "systemd", "/sbin/init")}
    assert ancestor_agents(50, lambda p: procs.get(p)) == ["claude", "codex"]

def test_codex_exec_signal():
    # Fill in from the Task 1 record. Exactly one of these three is the decided check; keep that one.
    assert headless_reason("codex", {"source": "exec"}, {}, "/p", (), ["codex"], False) == "codex_exec"
    assert headless_reason("codex", {"source": "cli"}, {}, "/p", (), ["codex"], False) is None
```

Adjust `test_codex_exec_signal` to whatever Task 1 recorded (payload key, env var, or TTY-only). If TTY is the decided signal for Codex, the second assertion becomes `tty=True → None`, `tty=False → "codex_exec"`, and `tty` is ignored for Claude.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_headless.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `agent_discord_webhook/headless.py`**

Order in `headless_reason`: Claude env (`"env"`), ancestry nested (`"nested"`), Codex exec signal (`"codex_exec"`), `skip_cwd_patterns` via `fnmatch` (`"skip_cwd"`). Ancestry walk stops at the first agent process; "nested" means a second agent name appears after it.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_headless.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add agent_discord_webhook/headless.py tests/test_headless.py
git commit -m "feat: headless and nested session detection"
```

---

### Task 7: Event decision logic (pure)

**Files:**
- Create: `agent_discord_webhook/hook.py`, `tests/test_hook.py`

**Interfaces:**
- Consumes: `Config` (Task 2), `Block`, `collapse_whitespace`, `strip_code_fences`, `tail`, `head` (Task 3), `Session`, `SessionStore` (Task 4).
- Produces:
  - `NOTIFICATION_TYPE_KEY = "notification_type"` (or whatever Task 1 recorded).
  - `summarize_tool_input(tool_input: object) -> str`: if dict with `command` that is a list → `" ".join`; if `command` is a str → it; else compact `json.dumps`; then `collapse_whitespace`.
  - `handle_event(tool: str, payload: dict, sessions: dict[str, Session], store: SessionStore, config: Config, muted: tuple[bool, bool], now: float) -> Block | None`. Mutates `sessions`. Never raises on missing payload keys.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_hook.py
CFG = Config(webhook_url="https://x", tail_chars=100, min_turn_seconds=45)
NOMUTE = (False, False)

def mk(tmp_path):
    return SessionStore(tmp_path, logging.getLogger("t")), {}

def ev(name, **k):
    return {"hook_event_name": name, "session_id": "S1", "cwd": "/home/u/proj", **k}

def test_prompt_then_long_stop_notifies_with_duration(tmp_path):
    store, s = mk(tmp_path)
    assert handle_event("claude", ev("UserPromptSubmit"), s, store, CFG, NOMUTE, 1000.0) is None
    b = handle_event("claude", ev("Stop", last_assistant_message="All done.\n\nTests pass."), s, store, CFG, NOMUTE, 1060.0)
    assert b == Block(1, "claude", "proj", "done", "All done. Tests pass.", 60.0)
    assert s["S1"].notified_for_stop is True

def test_short_stop_suppressed_then_idle_sends_once(tmp_path):
    store, s = mk(tmp_path)
    handle_event("claude", ev("UserPromptSubmit"), s, store, CFG, NOMUTE, 1000.0)
    assert handle_event("claude", ev("Stop", last_assistant_message="quick"), s, store, CFG, NOMUTE, 1010.0) is None
    idle = ev("Notification", message="Claude is waiting for your input", **{NOTIFICATION_TYPE_KEY: "idle_prompt"})
    b = handle_event("claude", idle, s, store, CFG, NOMUTE, 1070.0)
    assert b == Block(1, "claude", "proj", "done", "quick", 10.0)
    assert handle_event("claude", idle, s, store, CFG, NOMUTE, 1130.0) is None

def test_unknown_prompt_time_sends_without_duration(tmp_path):
    store, s = mk(tmp_path)
    b = handle_event("claude", ev("Stop", last_assistant_message="x"), s, store, CFG, NOMUTE, 5.0)
    assert b == Block(1, "claude", "proj", "done", "x", None)

def test_always_notify_sends_short_turn(tmp_path):
    store, s = mk(tmp_path)
    cfg = dataclasses.replace(CFG, always_notify=True)
    handle_event("codex", ev("UserPromptSubmit"), s, store, cfg, NOMUTE, 0.0)
    assert handle_event("codex", ev("Stop", last_assistant_message="hi"), s, store, cfg, NOMUTE, 1.0) is not None

def test_empty_message_gives_placeholder(tmp_path):
    store, s = mk(tmp_path)
    b = handle_event("claude", ev("Stop"), s, store, CFG, NOMUTE, 5.0)
    assert b.body == "(no message)"

def test_tail_and_fences_applied(tmp_path):
    store, s = mk(tmp_path)
    msg = "```py\n" + "a" * 200 + "\n```"
    b = handle_event("claude", ev("Stop", last_assistant_message=msg), s, store, CFG, NOMUTE, 5.0)
    assert b.body.startswith("…") and "```" not in b.body and len(b.body) <= 101

def test_claude_blockers(tmp_path):
    store, s = mk(tmp_path)
    p = handle_event("claude", ev("Notification", message="Claude needs your permission to use Bash",
                     **{NOTIFICATION_TYPE_KEY: "permission_prompt"}), s, store, CFG, NOMUTE, 1.0)
    assert p.kind == "permission" and p.body == "Claude needs your permission to use Bash"
    for t in ("agent_needs_input", "elicitation_dialog"):
        q = handle_event("claude", ev("Notification", message="m", **{NOTIFICATION_TYPE_KEY: t}), s, store, CFG, NOMUTE, 1.0)
        assert q.kind == "question"
    assert s["S1"].notified_for_stop is False

def test_codex_permission_request_summarizes_command(tmp_path):
    store, s = mk(tmp_path)
    b = handle_event("codex", ev("PermissionRequest", tool_name="Bash",
                     tool_input={"command": ["rm", "-rf", "build/"]}), s, store, CFG, NOMUTE, 1.0)
    assert b == Block(1, "codex", "proj", "permission", "Bash: rm -rf build/")
    assert summarize_tool_input({"command": "x " * 100})[:3] == "x x"
    assert summarize_tool_input({"path": "/a"}) == '{"path": "/a"}'

def test_mute_drops_done_keeps_blockers_unless_all(tmp_path):
    store, s = mk(tmp_path)
    assert handle_event("claude", ev("Stop", last_assistant_message="x"), s, store, CFG, (True, False), 5.0) is None
    assert s["S1"].notified_for_stop is False
    blk = ev("Notification", message="m", **{NOTIFICATION_TYPE_KEY: "permission_prompt"})
    assert handle_event("claude", blk, s, store, CFG, (True, False), 5.0) is not None
    assert handle_event("claude", blk, s, store, CFG, (True, True), 5.0) is None

def test_session_lifecycle_and_unregistered_events(tmp_path):
    store, s = mk(tmp_path)
    assert handle_event("claude", ev("SessionStart"), s, store, CFG, NOMUTE, 1.0) is None
    assert s["S1"].alias == 1
    assert handle_event("claude", ev("SubagentStop"), s, store, CFG, NOMUTE, 2.0) is None
    assert handle_event("claude", ev("SessionEnd"), s, store, CFG, NOMUTE, 3.0) is None
    assert "S1" not in s
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_hook.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `handle_event` and `summarize_tool_input` in `agent_discord_webhook/hook.py`**

`done` body pipeline: `strip_code_fences` → `collapse_whitespace` → `tail(_, config.tail_chars)`; empty → `(no message)`. Blocked bodies: `collapse_whitespace` then `head(_, 500)`; Codex permission body is `f"{tool_name}: {head(summary, config.tail_chars)}"`. `dirname = os.path.basename(cwd.rstrip("/")) or cwd`. A `Stop` while muted stores the message and timestamps but leaves `notified_for_stop` false.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_hook.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add agent_discord_webhook/hook.py tests/test_hook.py
git commit -m "feat: event decision logic with suppression and mute"
```

---

### Task 8: Discord posting

**Files:**
- Create: `agent_discord_webhook/discord.py`, `tests/test_discord.py`, `tests/fakediscord.py`

**Interfaces:**
- Produces:
  - `post(webhook_url: str, content: str, username: str, log: logging.Logger, *, timeout: float = 10.0, sleep: Callable[[float], None] = time.sleep) -> bool`. Body is JSON `{"content", "username", "allowed_mentions": {"parse": []}}`. 204/200 → True. 429 → sleep `retry_after` from the JSON body (seconds; default 1.0) and retry once. 5xx or `URLError`/`TimeoutError` → retry twice with `sleep(2.0)` between. Other 4xx → log ERROR, return False, and raise nothing. Returns `True` only on success. Log lines never contain `webhook_url`; use `redact(webhook_url)`.
  - `redact(url: str) -> str` → `https://discord.com/api/webhooks/…/…` style: keep scheme and host, replace path with `/…`.
  - `tests/fakediscord.py`: `class FakeDiscord` context manager starting `http.server` on `127.0.0.1:0` in a thread; `.url`, `.requests: list[dict]` (parsed JSON bodies), `.script: list[int]` of status codes to return in order (then 204), `.retry_after: float`.

- [ ] **Step 1: Write `tests/fakediscord.py` and the failing tests**

```python
# tests/test_discord.py
def test_success_payload_shape():
    with FakeDiscord() as fd:
        assert post(fd.url, "hi @everyone", "Claude", logging.getLogger("t"), sleep=lambda s: None)
        assert fd.requests == [{"content": "hi @everyone", "username": "Claude", "allowed_mentions": {"parse": []}}]

def test_429_then_success_sleeps_retry_after():
    slept = []
    with FakeDiscord(script=[429], retry_after=1.5) as fd:
        assert post(fd.url, "x", "Claude", logging.getLogger("t"), sleep=slept.append)
        assert slept == [1.5] and len(fd.requests) == 2

def test_5xx_retries_twice_then_fails():
    slept = []
    with FakeDiscord(script=[500, 502, 503]) as fd:
        assert post(fd.url, "x", "Claude", logging.getLogger("t"), sleep=slept.append) is False
        assert slept == [2.0, 2.0] and len(fd.requests) == 3

def test_404_fails_without_retry_and_log_redacts_url(caplog):
    log = logging.getLogger("t")
    with FakeDiscord(script=[404]) as fd, caplog.at_level(logging.DEBUG, logger="t"):
        assert post(fd.url, "x", "Claude", log, sleep=lambda s: None) is False
        assert len(fd.requests) == 1
    assert fd.url not in caplog.text and "404" in caplog.text

def test_connection_refused_logs_without_url(caplog):
    url = "http://127.0.0.1:9/api/webhooks/123/secret"
    with caplog.at_level(logging.DEBUG, logger="t"):
        assert post(url, "x", "Claude", logging.getLogger("t"), sleep=lambda s: None) is False
    assert "secret" not in caplog.text

def test_redact():
    assert redact("https://discord.com/api/webhooks/123/abc") == "https://discord.com/…"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_discord.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `agent_discord_webhook/discord.py`**

Use `urllib.request.Request` with `Content-Type: application/json` and a `User-Agent: agent-discord-webhook`. Catch `HTTPError` for status handling; catch `URLError`, `TimeoutError`, `OSError` as network errors and log only `type(e).__name__` plus `e.reason` if it is not a string containing the URL.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_discord.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add agent_discord_webhook/discord.py tests/test_discord.py tests/fakediscord.py
git commit -m "feat: discord webhook posting with retries and redaction"
```

---

### Task 9: Hook runner and flush (integration)

**Files:**
- Modify: `agent_discord_webhook/hook.py`
- Create: `agent_discord_webhook/cli.py` (minimal: only `hook` subcommand; Task 11 extends it), `tests/test_hook_integration.py`

**Interfaces:**
- Consumes: everything above.
- Produces (in `hook.py`):
  - `flush(state_dir: Path, config: Config, log: logging.Logger, *, sleep=time.sleep, post=discord.post) -> None`: drain; if empty return; `render_posts`; `username_for`; post each in order; delete the spool files of each successful post; stop at the first failure and leave the rest.
  - `run_hook(tool: str, stdin: IO[str], env: Mapping[str, str], *, now: Callable[[], float] = time.time, sleep=time.sleep, post=discord.post, read_proc=headless.default_read_proc, tty: Callable[[], bool] = headless.has_controlling_tty) -> int` returns 0 always. Order: parse JSON (invalid → log, return 0); load config; compute `headless_reason`; if set, log DEBUG `headless <reason> session=<id>` and return 0; `with store.locked()`: sweep, `handle_event`; if a block: `spool_append`; `try_flusher_lock`; if won: `sleep(config.batch_window_seconds)`, `flush`, release. Any exception → `log.exception`, return 0. If `config.webhook_url` is None, log ERROR once and return 0 before spooling.
- Produces (in `cli.py`): `main(argv: list[str] | None = None) -> int` with `argparse` subcommand `hook --tool {claude,codex}` calling `run_hook(tool, sys.stdin, os.environ)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_hook_integration.py
def env_for(tmp_path, url):
    cfg = tmp_path / "config.toml"; cfg.write_text(f'webhook_url = "{url}"\nmin_turn_seconds = 0\nbatch_window_seconds = 0\n')
    return {"ADW_CONFIG_PATH": str(cfg), "ADW_STATE_DIR": str(tmp_path / "state"),
            "CLAUDE_CODE_SESSION_ATTENDED": "1"}

def stop_payload(sid, msg, cwd="/home/u/proj"):
    return json.dumps({"hook_event_name": "Stop", "session_id": sid, "cwd": cwd, "last_assistant_message": msg})

def test_in_process_stop_posts_one_message(tmp_path):
    with FakeDiscord() as fd:
        env = env_for(tmp_path, fd.url)
        rc = run_hook("claude", io.StringIO(stop_payload("S1", "done thing")), env,
                      sleep=lambda s: None, read_proc=lambda p: None, tty=lambda: True)
        assert rc == 0
        assert fd.requests[0]["content"] == "**[1] claude · proj** ✅ done\ndone thing"
        assert fd.requests[0]["username"] == "Claude"
    assert spool_depth(Path(env["ADW_STATE_DIR"])) == 0

def test_two_sessions_batch_into_one_post(tmp_path):
    with FakeDiscord() as fd:
        env = env_for(tmp_path, fd.url)
        sd = Path(env["ADW_STATE_DIR"])
        # Simulate a flusher already sleeping: hold the lock, run two hooks, then flush manually.
        state_dir(env)
        held = try_flusher_lock(sd)
        for sid, msg in (("S1", "one"), ("S2", "two")):
            run_hook("claude", io.StringIO(stop_payload(sid, msg)), env, sleep=lambda s: None,
                     read_proc=lambda p: None, tty=lambda: True)
        assert fd.requests == [] and spool_depth(sd) == 2
        held.close()
        flush(sd, load_config(env), logging.getLogger("t"), sleep=lambda s: None)
        assert len(fd.requests) == 1
        assert "[1] claude · proj" in fd.requests[0]["content"] and "[2] claude · proj" in fd.requests[0]["content"]
        assert "\n\n" in fd.requests[0]["content"]

def test_failed_post_leaves_spool_for_next_run(tmp_path):
    with FakeDiscord(script=[500, 500, 500]) as fd:
        env = env_for(tmp_path, fd.url)
        run_hook("claude", io.StringIO(stop_payload("S1", "x")), env, sleep=lambda s: None,
                 read_proc=lambda p: None, tty=lambda: True)
        sd = Path(env["ADW_STATE_DIR"])
        assert spool_depth(sd) == 1
        run_hook("claude", io.StringIO(stop_payload("S1", "y")), env, sleep=lambda s: None,
                 read_proc=lambda p: None, tty=lambda: True)
        assert spool_depth(sd) == 0 and len(fd.requests) == 4

def test_headless_session_touches_nothing(tmp_path):
    with FakeDiscord() as fd:
        env = env_for(tmp_path, fd.url) | {"CLAUDE_CODE_SESSION_ATTENDED": "0"}
        run_hook("claude", io.StringIO(stop_payload("S1", "x")), env, sleep=lambda s: None,
                 read_proc=lambda p: None, tty=lambda: True)
        sd = Path(env["ADW_STATE_DIR"])
        assert fd.requests == [] and not (sd / "sessions.json").exists()
        assert "headless" in (sd / "log").read_text()

def test_garbage_stdin_and_missing_url_exit_zero(tmp_path):
    env = env_for(tmp_path, "")
    assert run_hook("claude", io.StringIO("not json"), env, sleep=lambda s: None, read_proc=lambda p: None, tty=lambda: True) == 0
    assert run_hook("claude", io.StringIO(stop_payload("S1", "x")), env, sleep=lambda s: None, read_proc=lambda p: None, tty=lambda: True) == 0

def test_subprocess_adw_hook_prints_nothing(tmp_path):
    with FakeDiscord() as fd:
        env = os.environ | env_for(tmp_path, fd.url)
        r = subprocess.run(["uv", "run", "adw", "hook", "--tool", "codex"], input=stop_payload("S9", "hello"),
                           capture_output=True, text=True, env=env, cwd=REPO_ROOT, timeout=60)
        assert r.returncode == 0 and r.stdout == ""
        assert fd.requests and fd.requests[0]["username"] == "Codex"
```

Note: the subprocess test runs under a real `claude` ancestor when executed from inside Claude Code, which makes ancestry report `["claude"]` for a `codex` hook. That is not "nested" (only one agent in the chain), so it passes. The test still sets `CLAUDE_CODE_SESSION_ATTENDED=1` via `env_for`; also strip `CLAUDE_CODE_CHILD_SESSION` and `CLAUDE_CODE_ENTRYPOINT` from `env` in that test.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_hook_integration.py -v`
Expected: FAIL with `ImportError` on `run_hook`/`flush`.

- [ ] **Step 3: Implement `flush` and `run_hook` in `hook.py`, and `cli.py` with the `hook` subcommand**

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -v`
Expected: all passed, 6 new.

- [ ] **Step 5: Commit**

```bash
git add agent_discord_webhook/hook.py agent_discord_webhook/cli.py tests/test_hook_integration.py
git commit -m "feat: hook runner, batching flush, adw hook entrypoint"
```

---

### Task 10: Install and uninstall merge logic

**Files:**
- Create: `agent_discord_webhook/install.py`, `tests/test_install.py`

**Interfaces:**
- Produces:
  - `MARKER = "adw hook"`
  - `hook_entry(adw_path: str, tool: str, matcher: str | None = None) -> dict` → `{"matcher": matcher, "hooks": [{"type": "command", "command": f"{adw_path} hook --tool {tool}", "async": True, "timeout": 30}]}` with `matcher` omitted when None.
  - `CLAUDE_EVENTS: dict[str, str | None] = {"Stop": None, "Notification": "permission_prompt|idle_prompt|agent_needs_input|elicitation_dialog", "UserPromptSubmit": None, "SessionStart": None, "SessionEnd": None}`
  - `CODEX_EVENTS = {"Stop": None, "PermissionRequest": None, "UserPromptSubmit": None, "SessionStart": None, "SessionEnd": None}`
  - `merge_hooks(settings: dict, adw_path: str, tool: str) -> dict` returns a deep copy with adw entries removed then appended under `settings["hooks"][event]` for each event in the tool's table.
  - `remove_hooks(settings: dict) -> dict` deep copy with every hook whose `command` contains `MARKER` removed; groups left with no hooks are removed; events left with no groups are removed; an empty `hooks` key is removed.
  - `is_installed(settings: dict) -> bool`
  - `write_json_with_backup(path: Path, data: dict, now: datetime) -> Path | None` copies the existing file to `<path>.bak-<YYYYmmdd-HHMMSS>` first (returns that path, or None if the file did not exist), then writes `json.dumps(data, indent=2) + "\n"`, creating parent dirs.
  - `write_config(path: Path, webhook_url: str) -> None` writes the spec's TOML with defaults, mode 0600, creating the parent dir with mode 0700.
  - `adw_executable() -> str` → `shutil.which("adw")` or `str(Path(sys.argv[0]).resolve())`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_install.py
EXISTING = {"hooks": {
    "Stop": [{"hooks": [{"type": "command", "command": "/x/log-progress.sh", "timeout": 30, "async": True}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 /x/context-check.py", "timeout": 10}]}]},
    "permissions": {"allow": ["Bash(ls:*)"]}}

def test_merge_appends_and_keeps_existing():
    out = merge_hooks(EXISTING, "/opt/adw", "claude")
    assert out["permissions"] == EXISTING["permissions"]
    assert out["hooks"]["Stop"][0] == EXISTING["hooks"]["Stop"][0]
    assert out["hooks"]["Stop"][1]["hooks"][0] == {"type": "command", "command": "/opt/adw hook --tool claude", "async": True, "timeout": 30}
    assert out["hooks"]["Notification"][0]["matcher"] == "permission_prompt|idle_prompt|agent_needs_input|elicitation_dialog"
    assert set(out["hooks"]) == {"Stop", "UserPromptSubmit", "Notification", "SessionStart", "SessionEnd"}
    assert EXISTING["hooks"]["Stop"] and len(EXISTING["hooks"]["Stop"]) == 1   # input untouched

def test_merge_is_idempotent_and_updates_path():
    once = merge_hooks(EXISTING, "/opt/adw", "claude")
    twice = merge_hooks(once, "/new/adw", "claude")
    assert len(twice["hooks"]["Stop"]) == 2
    assert twice["hooks"]["Stop"][1]["hooks"][0]["command"] == "/new/adw hook --tool claude"

def test_remove_restores_original():
    assert remove_hooks(merge_hooks(EXISTING, "/opt/adw", "claude")) == EXISTING
    assert remove_hooks(merge_hooks({}, "/opt/adw", "codex")) == {}
    assert is_installed(merge_hooks({}, "/opt/adw", "codex")) and not is_installed(EXISTING)

def test_codex_events():
    out = merge_hooks({}, "/opt/adw", "codex")
    assert set(out["hooks"]) == {"Stop", "PermissionRequest", "UserPromptSubmit", "SessionStart", "SessionEnd"}
    assert out["hooks"]["Stop"][0]["hooks"][0]["command"] == "/opt/adw hook --tool codex"

def test_write_json_with_backup(tmp_path):
    p = tmp_path / "a" / "settings.json"
    assert write_json_with_backup(p, {"a": 1}, datetime(2026, 10, 22, 14, 5, 9)) is None
    bak = write_json_with_backup(p, {"a": 2}, datetime(2026, 10, 22, 14, 5, 9))
    assert bak == tmp_path / "a" / "settings.json.bak-20261022-140509"
    assert json.loads(bak.read_text()) == {"a": 1} and json.loads(p.read_text()) == {"a": 2}

def test_write_config_mode_and_content(tmp_path):
    p = tmp_path / "c" / "config.toml"
    write_config(p, "https://discord.com/api/webhooks/1/x")
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    assert load_config({"ADW_CONFIG_PATH": str(p)}) == Config(webhook_url="https://discord.com/api/webhooks/1/x")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_install.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `agent_discord_webhook/install.py`**

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_install.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add agent_discord_webhook/install.py tests/test_install.py
git commit -m "feat: hook merge/unmerge and config writing"
```

---

### Task 11: CLI commands

**Files:**
- Modify: `agent_discord_webhook/cli.py`
- Create: `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 10 functions; `SessionStore`, `set_mute`, `clear_mute`, `mute_state`, `spool_depth` (Tasks 4–5); `redact`, `post` (Task 8); `render_posts`, `Block` (Task 3).
- Produces (in `cli.py`):
  - `parse_duration(s: str) -> float` accepts `<int>(s|m|h|d)`; raises `ValueError` otherwise.
  - `CLAUDE_SETTINGS = Path("~/.claude/settings.json")`, `CODEX_HOOKS = Path("~/.codex/hooks.json")` (expanded at call time; tests override via `ADW_CLAUDE_SETTINGS` / `ADW_CODEX_HOOKS` env vars).
  - `main(argv=None, *, stdin=sys.stdin, stdout=sys.stdout, env=os.environ, now=time.time) -> int` with subcommands:
    - `install [--webhook-url URL]`: URL from flag, else prompt `Discord webhook URL: ` if `stdin.isatty()`, else print `webhook URL required: pass --webhook-url or run interactively` to stderr and return 2. Write config, merge into both files (creating `{}` if missing), print each backup path and `installed: <path>`.
    - `uninstall`: remove from both files (skip missing), print `removed: <path>`.
    - `test`: post a two-block sample (one `done`, one `permission`) rendered with `render_posts`; print `ok` or `failed (see log)`; return 0/1.
    - `status [--log]`: lines `config: <path> (webhook: <redacted> | missing)`, `claude hooks: installed|not installed`, `codex hooks: installed|not installed`, `mute: off|until <ISO> [all]`, `spool: <n>`, then one line per live session `[<alias>] <tool> <cwd> last seen <ISO>`. With `--log`, append the last 50 lines of the log file.
    - `mute <duration> [--all]`, `unmute`.
    - `hook --tool {claude,codex}` (from Task 9).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cli.py
def base_env(tmp_path):
    return {"ADW_CONFIG_PATH": str(tmp_path / "config.toml"), "ADW_STATE_DIR": str(tmp_path / "state"),
            "ADW_CLAUDE_SETTINGS": str(tmp_path / "claude.json"), "ADW_CODEX_HOOKS": str(tmp_path / "codex.json")}

def test_parse_duration():
    assert parse_duration("30m") == 1800 and parse_duration("2h") == 7200
    with pytest.raises(ValueError): parse_duration("soon")

def test_install_then_status_then_uninstall(tmp_path):
    env = base_env(tmp_path)
    (tmp_path / "claude.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/x.sh"}]}]}}))
    out = io.StringIO()
    assert main(["install", "--webhook-url", "https://discord.com/api/webhooks/1/s"], stdout=out, env=env) == 0
    assert "installed:" in out.getvalue() and "/1/s" not in out.getvalue()
    claude = json.loads((tmp_path / "claude.json").read_text())
    assert len(claude["hooks"]["Stop"]) == 2 and is_installed(json.loads((tmp_path / "codex.json").read_text()))
    assert list(tmp_path.glob("claude.json.bak-*"))
    out = io.StringIO()
    assert main(["status"], stdout=out, env=env) == 0
    s = out.getvalue()
    assert "claude hooks: installed" in s and "codex hooks: installed" in s and "mute: off" in s and "/1/s" not in s
    assert main(["uninstall"], stdout=io.StringIO(), env=env) == 0
    assert json.loads((tmp_path / "claude.json").read_text())["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": "/x.sh"}]}]

def test_install_noninteractive_without_url_fails(tmp_path, capsys):
    env = base_env(tmp_path)
    assert main(["install"], stdin=io.StringIO(""), stdout=io.StringIO(), env=env) == 2
    assert "webhook URL required" in capsys.readouterr().err

def test_mute_unmute_status(tmp_path):
    env = base_env(tmp_path)
    assert main(["mute", "2h", "--all"], stdout=io.StringIO(), env=env, now=lambda: 1000.0) == 0
    assert mute_state(Path(env["ADW_STATE_DIR"]), 1000.0 + 7199) == (True, True)
    out = io.StringIO(); main(["status"], stdout=out, env=env, now=lambda: 1000.0)
    assert "mute: until" in out.getvalue() and "all" in out.getvalue()
    assert main(["unmute"], stdout=io.StringIO(), env=env) == 0
    assert mute_state(Path(env["ADW_STATE_DIR"]), 1000.0) == (False, False)

def test_test_command_posts_two_blocks(tmp_path):
    with FakeDiscord() as fd:
        env = base_env(tmp_path) | {"ADW_WEBHOOK_URL": fd.url}
        out = io.StringIO()
        assert main(["test"], stdout=out, env=env) == 0
        assert out.getvalue().strip() == "ok"
        assert fd.requests[0]["username"] == "Agents" and "needs permission" in fd.requests[0]["content"]

def test_status_lists_sessions(tmp_path):
    env = base_env(tmp_path)
    sd = state_dir(env)
    store = SessionStore(sd, logging.getLogger("t"))
    with store.locked() as s:
        store.touch(s, "S1", "claude", "/home/u/proj", 1000.0)
    out = io.StringIO(); main(["status"], stdout=out, env=env, now=lambda: 1001.0)
    assert "[1] claude /home/u/proj" in out.getvalue()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL with `ImportError` on `parse_duration`.

- [ ] **Step 3: Implement the subcommands in `cli.py`**

Keep `cli.py` to argument parsing and printing; file and state work calls Task 10 and Task 4 functions. `status` resolves `is_installed` by loading each settings file (missing → not installed).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -v`
Expected: all passed, 6 new.

- [ ] **Step 5: Commit**

```bash
git add agent_discord_webhook/cli.py tests/test_cli.py
git commit -m "feat: adw install/uninstall/status/mute/test commands"
```

---

### Task 12: README, real install, end-to-end check

**Files:**
- Create: `README.md`
- Modify (on this machine, outside the repo): `~/.claude/settings.json`, `~/.codex/hooks.json`, `~/.config/agent-discord-webhook/config.toml`

**Interfaces:**
- Consumes: the whole CLI.

- [ ] **Step 1: Write `README.md`**

Sections, in order: what it does (two sentences and one sample message block from the spec), install (`uv tool install .` then `adw install`), getting a webhook URL (Discord channel → Edit Channel → Integrations → Webhooks → New Webhook → Copy URL), enabling push for the channel in the Discord mobile app (channel → notification settings → All Messages), config keys with defaults (the five keys plus `skip_cwd_patterns`), commands table (copy from spec CLI table, with `--tool` in the `hook` row), how suppression works (three sentences), mute, uninstall, where state and logs live. Plain prose, no marketing.

- [ ] **Step 2: Install for real**

Run: `uv tool install . && adw install --webhook-url <URL>`
Expected: two backup paths printed, two `installed:` lines, and `adw status` shows both tools installed. Confirm with `python3 -c "import json;print(list(json.load(open('$HOME/.claude/settings.json'))['hooks']))"` that `Stop` and `UserPromptSubmit` still hold the pre-existing hooks.

- [ ] **Step 3: `adw test`**

Run: `adw test`
Expected: prints `ok`; phone receives one push with two blocks.

- [ ] **Step 4: Live Claude check**

In a new terminal: `claude`, ask for a task that takes over 45 seconds, walk away.
Expected: one `✅ done` push with duration and the message tail. Then ask something trivial, do not type for 90 seconds: one `✅ done` push arrives about a minute later. Then trigger a permission prompt: `⏸ needs permission` push arrives immediately, even while muted with `adw mute 1h` (unmute afterwards).

- [ ] **Step 5: Live Codex check**

Run an interactive `codex` turn over 45 seconds and one `codex exec "say hi"`.
Expected: one push for the interactive turn with `Codex` as username; nothing for `codex exec`. `adw status --log` shows a `headless` debug line for the exec session.

- [ ] **Step 6: Nested check**

From inside a Claude session, ask Claude to run `codex exec "say hi"`.
Expected: no push; log shows `headless nested`.

- [ ] **Step 7: Commit**

```bash
git add README.md
git commit -m "docs: README with install, config, and usage"
```

---

## Self-review notes

- Spec coverage: shape/no-daemon (T9), aliases and stale sweep (T4), event table (T7), suppression and idle (T7), headless (T1, T6), mute (T4, T7, T11), message format incl. fences, splitting, username (T3), batching (T5, T9), delivery and retries (T8, T9), config and env override (T2), CLI table (T9, T11), hook entries written (T10), packaging and tests (T2, T9), README (T12). Anti-goals: blockers always send (T7 mute test), no URL in logs (T8), `allowed_mentions` (T8), async hooks (T10 entry shape), existing hooks preserved (T10, T12 step 2).
- Names used across tasks: `Block`, `Post`, `render_posts`, `username_for`, `Session`, `SessionStore.locked/touch/end/sweep`, `spool_append/spool_drain/spool_depth/try_flusher_lock`, `set_mute/clear_mute/mute_state`, `headless_reason`, `handle_event`, `flush`, `run_hook`, `post`, `redact`, `merge_hooks/remove_hooks/is_installed/write_json_with_backup/write_config`, `main`, `parse_duration`. Each is defined once in the task that owns it.
