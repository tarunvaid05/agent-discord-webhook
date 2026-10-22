import dataclasses
import logging

from agent_discord_webhook.config import Config
from agent_discord_webhook.format import Block
from agent_discord_webhook.hook import NOTIFICATION_TYPE_KEY, handle_event, summarize_tool_input
from agent_discord_webhook.state import SessionStore

CFG = Config(webhook_url="https://x", tail_chars=100, min_turn_seconds=45)
NOMUTE = (False, False)


def mk(tmp_path):
    return SessionStore(tmp_path, logging.getLogger("t")), {}


def ev(name, **k):
    return {"hook_event_name": name, "session_id": "S1", "cwd": "/home/u/proj", **k}


def test_notification_type_key_matches_claude_payload():
    assert NOTIFICATION_TYPE_KEY == "notification_type"


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


def test_idle_without_prior_stop_sends_nothing(tmp_path):
    store, s = mk(tmp_path)
    idle = ev("Notification", message="m", **{NOTIFICATION_TYPE_KEY: "idle_prompt"})
    assert handle_event("claude", idle, s, store, CFG, NOMUTE, 1.0) is None


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


def test_long_blocked_message_capped(tmp_path):
    store, s = mk(tmp_path)
    p = handle_event("claude", ev("Notification", message="z" * 900, **{NOTIFICATION_TYPE_KEY: "permission_prompt"}),
                     s, store, CFG, NOMUTE, 1.0)
    assert len(p.body) == 501 and p.body.endswith("…")


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


def test_missing_session_id_or_cwd_does_not_crash(tmp_path):
    store, s = mk(tmp_path)
    assert handle_event("claude", {"hook_event_name": "Stop"}, s, store, CFG, NOMUTE, 1.0) is None
    assert s == {}
