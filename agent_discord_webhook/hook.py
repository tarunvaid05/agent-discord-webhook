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
