"""Event handling: decide whether a hook event becomes a notification."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping

from .config import Config
from .format import Block, collapse_whitespace, head, strip_code_fences, tail
from .state import Session, SessionStore

NOTIFICATION_TYPE_KEY = "notification_type"
BLOCKED_BODY_CAP = 500
NO_MESSAGE = "(no message)"

QUESTION_TYPES = ("agent_needs_input", "elicitation_dialog")


def summarize_tool_input(tool_input: object) -> str:
    if isinstance(tool_input, Mapping) and "command" in tool_input:
        cmd = tool_input["command"]
        if isinstance(cmd, (list, tuple)):
            text = " ".join(str(c) for c in cmd)
        else:
            text = str(cmd)
    elif isinstance(tool_input, str):
        text = tool_input
    else:
        try:
            text = json.dumps(tool_input, separators=(", ", ": "))
        except (TypeError, ValueError):
            text = str(tool_input)
    return collapse_whitespace(text)


def _dirname(cwd: str) -> str:
    return os.path.basename(cwd.rstrip("/")) or cwd


def _done_body(message: str, tail_chars: int) -> str:
    text = collapse_whitespace(strip_code_fences(message))
    return tail(text, tail_chars) if text else NO_MESSAGE


def _blocked_body(text: str) -> str:
    return head(collapse_whitespace(text), BLOCKED_BODY_CAP)


def _done_block(s: Session, tool: str, body: str) -> Block:
    duration = None
    if s.last_prompt_at is not None and s.last_stop_at is not None:
        duration = max(0.0, s.last_stop_at - s.last_prompt_at)
    return Block(s.alias, tool, _dirname(s.cwd), "done", body, duration)


def handle_event(
    tool: str,
    payload: Mapping[str, object],
    sessions: dict[str, Session],
    store: SessionStore,
    config: Config,
    muted: tuple[bool, bool],
    now: float,
) -> Block | None:
    event = str(payload.get("hook_event_name", ""))
    session_id = payload.get("session_id")
    cwd = payload.get("cwd")
    if not isinstance(session_id, str) or not session_id or not isinstance(cwd, str):
        return None
    muted_done, muted_all = muted

    if event == "SessionEnd":
        store.end(sessions, session_id)
        return None

    s = store.touch(sessions, session_id, tool, cwd, now)
    if s.headless:
        return None

    if event == "UserPromptSubmit":
        s.last_prompt_at = now
        return None

    if event == "Stop":
        message = payload.get("last_assistant_message")
        s.last_assistant_message = message if isinstance(message, str) else ""
        s.last_stop_at = now
        s.notified_for_stop = False
        long_enough = s.last_prompt_at is None or (now - s.last_prompt_at) >= config.min_turn_seconds
        if not (config.always_notify or long_enough) or muted_done:
            return None
        s.notified_for_stop = True
        return _done_block(s, tool, _done_body(s.last_assistant_message, config.tail_chars))

    if event == "Notification":
        ntype = payload.get(NOTIFICATION_TYPE_KEY)
        message = payload.get("message")
        text = message if isinstance(message, str) else ""
        if ntype == "idle_prompt":
            if s.notified_for_stop or s.last_stop_at is None or muted_done:
                return None
            s.notified_for_stop = True
            return _done_block(s, tool, _done_body(s.last_assistant_message, config.tail_chars))
        if ntype == "permission_prompt":
            kind = "permission"
        elif ntype in QUESTION_TYPES:
            kind = "question"
        else:
            return None
        if muted_all:
            return None
        return Block(s.alias, tool, _dirname(cwd), kind, _blocked_body(text) or NO_MESSAGE)

    if event == "PermissionRequest":
        if muted_all:
            return None
        tool_name = str(payload.get("tool_name") or "tool")
        summary = head(summarize_tool_input(payload.get("tool_input")), config.tail_chars)
        return Block(s.alias, tool, _dirname(cwd), "permission", _blocked_body(f"{tool_name}: {summary}"))

    return None


# ---------------------------------------------------------------------------
# Runner: stdin -> state -> spool -> flush
# ---------------------------------------------------------------------------

import contextlib  # noqa: E402
import logging  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from collections.abc import Callable  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import IO  # noqa: E402

from . import discord, headless  # noqa: E402
from .config import get_logger, load_config, state_dir  # noqa: E402
from .format import render_posts, username_for  # noqa: E402
from .state import mute_state, spool_append, spool_depth, spool_drain, try_flusher_lock  # noqa: E402

PostFn = Callable[..., discord.Outcome]


def flush(
    sd: Path,
    config: Config,
    log: logging.Logger,
    *,
    sleep: Callable[[float], None] = time.sleep,
    post: PostFn = discord.post,
) -> bool:
    """Drain the spool once. Returns False when a retryable failure left blocks behind."""
    entries = spool_drain(sd, log)
    if not entries:
        return True
    if not config.webhook_url:
        log.error("no webhook URL configured; %d spool files left", len(entries))
        return False
    blocks = [b for _, b in entries]
    username = username_for(blocks)
    for p in render_posts(blocks):
        outcome = post(config.webhook_url, p.content, username, log, sleep=sleep)
        if outcome is discord.Outcome.RETRYABLE:
            log.warning("post failed; %d block(s) left in spool", len(entries))
            return False
        for i in p.block_indices:
            with contextlib.suppress(OSError):
                entries[i][0].unlink()
        if outcome is discord.Outcome.PERMANENT:
            log.error("dropped %d block(s) after permanent failure", len(p.block_indices))
    return True


def run_flusher(
    sd: Path,
    config: Config,
    log: logging.Logger,
    *,
    sleep: Callable[[float], None] = time.sleep,
    post: PostFn = discord.post,
) -> None:
    held = try_flusher_lock(sd)
    if held is None:
        log.debug("flusher already running")
        return
    try:
        sleep(config.batch_window_seconds)
        ok = flush(sd, config, log, sleep=sleep, post=post)
    finally:
        held.close()
    # A hook that fired while we held the lock spooled its block and exited, trusting us
    # to post it. Keep going until the spool is empty or a post fails.
    while ok and spool_depth(sd) > 0:
        held = try_flusher_lock(sd)
        if held is None:
            return
        try:
            ok = flush(sd, config, log, sleep=sleep, post=post)
        finally:
            held.close()


def spawn_detached_flusher(env: Mapping[str, str]) -> None:
    # -P and cwd="/" keep the agent's working directory off sys.path: a project json.py or
    # logging.py must not shadow the stdlib in the flusher, and the repo dir must not run its
    # own working tree instead of the installed package.
    subprocess.Popen(
        [sys.executable, "-P", "-m", "agent_discord_webhook.cli", "flush"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, env=dict(env), close_fds=True, cwd="/",
    )


def run_hook(
    tool: str,
    stdin: IO[str],
    env: Mapping[str, str],
    *,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    post: PostFn = discord.post,
    read_proc: headless.ProcReader = headless.default_read_proc,
    tty: Callable[[], bool] = headless.has_controlling_tty,
    detach: bool = False,
) -> int:
    log = get_logger(env)
    try:
        try:
            payload = json.loads(stdin.read())
        except ValueError as e:
            log.warning("unreadable hook payload (%s)", type(e).__name__)
            return 0
        if not isinstance(payload, dict):
            log.warning("hook payload is not an object")
            return 0
        config = load_config(env)
        sd = state_dir(env)
        cwd = str(payload.get("cwd", ""))
        ancestors = headless.ancestor_agents(os.getpid(), read_proc)
        codex_exec = tool == "codex" and headless.has_codex_exec_ancestor(os.getpid(), read_proc)
        reason = headless.headless_reason(tool, payload, env, cwd, config.skip_cwd_patterns, ancestors, tty(),
                                          codex_exec=codex_exec)
        session_id = payload.get("session_id")
        if reason:
            log.debug("headless %s session=%s tool=%s event=%s", reason, session_id, tool,
                      payload.get("hook_event_name"))
            if reason == "codex_exec" and isinstance(session_id, str) and session_id:
                # Belt and braces: remember the session so its later events stay silent even
                # if the process tree is unreadable for one of them.
                store = SessionStore(sd, log)
                with store.locked() as sessions:
                    store.mark_headless(sessions, session_id, tool, cwd, now())
            return 0
        if not config.webhook_url:
            log.error("no webhook URL configured; run `adw install`")
            return 0
        t = now()
        store = SessionStore(sd, log)
        with store.locked() as sessions:
            store.sweep(sessions, t)
            block = handle_event(tool, payload, sessions, store, config, mute_state(sd, t), t)
        if block is None:
            return 0
        spool_append(sd, block)
        log.debug("spooled %s for [%d] %s", block.kind, block.alias, block.dirname)
        if detach:
            spawn_detached_flusher(env)
        else:
            run_flusher(sd, config, log, sleep=sleep, post=post)
    except Exception as e:
        # Type name only: an exception message may carry the webhook URL.
        log.error("hook failed (%s)", type(e).__name__)
    return 0
