import json

from agent_discord_webhook.transcript import pending_background_tasks


def _line(role, content):
    return json.dumps({"type": role, "message": {"role": role, "content": content}})


def _use(tid, name, **inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def _result(tid, text):
    return {"type": "tool_result", "tool_use_id": tid, "content": [{"type": "text", "text": text}]}


def _note(tool_use_id, task_id):
    return f"<task-notification>\n<task-id>{task_id}</task-id>\n<tool-use-id>{tool_use_id}</tool-use-id>\n<status>completed</status>\n</task-notification>"


AGENT_LAUNCH = "Async agent launched successfully. (internal)\nagentId: a616c6bbe65986a00 (internal ID)"
BASH_LAUNCH = "Command running in background with ID: b0490n0dw. Output is being written to: /tmp/x.output."


def write(tmp_path, lines):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(lines) + "\n")
    return p


def test_missing_transcript_is_unknown(tmp_path):
    assert pending_background_tasks(tmp_path / "nope.jsonl") is None


def test_no_launches_means_nothing_pending(tmp_path):
    p = write(tmp_path, [_line("user", "hi"), _line("assistant", [{"type": "text", "text": "hello"}]), "not json"])
    assert pending_background_tasks(p) == 0


def test_agent_launch_pending_until_notification_by_tool_use_id(tmp_path):
    lines = [
        _line("user", "go"),
        _line("assistant", [_use("toolu_1", "Agent", description="impl", subagent_type="general-purpose")]),
        _line("user", [_result("toolu_1", AGENT_LAUNCH)]),
        _line("assistant", [{"type": "text", "text": "Waiting."}]),
    ]
    assert pending_background_tasks(write(tmp_path, lines)) == 1
    lines.append(_line("user", _note("toolu_1", "a616c6bbe65986a00")))
    assert pending_background_tasks(write(tmp_path, lines)) == 0


def test_two_launches_one_done(tmp_path):
    lines = [
        _line("assistant", [_use("toolu_1", "Agent", description="a"), _use("toolu_2", "Agent", description="b")]),
        _line("user", [_result("toolu_1", AGENT_LAUNCH), _result("toolu_2", AGENT_LAUNCH.replace("a616c6bbe65986a00", "b7"))]),
        _line("user", [{"type": "text", "text": _note("toolu_2", "b7")}]),
    ]
    assert pending_background_tasks(write(tmp_path, lines)) == 1


def test_background_bash_done_by_task_id_or_taskstop(tmp_path):
    lines = [
        _line("assistant", [_use("toolu_b", "Bash", command="make", run_in_background=True)]),
        _line("user", [_result("toolu_b", BASH_LAUNCH)]),
    ]
    assert pending_background_tasks(write(tmp_path, lines)) == 1
    stopped = lines + [_line("assistant", [_use("toolu_s", "TaskStop", task_id="b0490n0dw")])]
    assert pending_background_tasks(write(tmp_path, stopped)) == 0
    notified = lines + [_line("user", _note("toolu_other", "b0490n0dw"))]
    assert pending_background_tasks(write(tmp_path, notified)) == 0


def test_foreground_bash_and_sync_agent_result_not_pending(tmp_path):
    lines = [
        _line("assistant", [_use("toolu_f", "Bash", command="ls"), _use("toolu_a", "Agent", description="sync")]),
        _line("user", [_result("toolu_f", "file.txt"), _result("toolu_a", "Here is the full report from the agent: all good.")]),
    ]
    assert pending_background_tasks(write(tmp_path, lines)) == 0


def test_launch_without_result_yet_is_pending(tmp_path):
    lines = [_line("assistant", [_use("toolu_1", "Agent", description="a")])]
    assert pending_background_tasks(write(tmp_path, lines)) == 1
