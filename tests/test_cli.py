import io
import json
import logging
from pathlib import Path

import pytest

from agent_discord_webhook.cli import main, parse_duration
from agent_discord_webhook.config import state_dir
from agent_discord_webhook.install import is_installed
from agent_discord_webhook.state import SessionStore, mute_state
from tests.fakediscord import FakeDiscord


def base_env(tmp_path):
    return {"ADW_CONFIG_PATH": str(tmp_path / "config.toml"), "ADW_STATE_DIR": str(tmp_path / "state"),
            "ADW_CLAUDE_SETTINGS": str(tmp_path / "claude.json"), "ADW_CODEX_HOOKS": str(tmp_path / "codex.json")}


def test_parse_duration():
    assert parse_duration("30m") == 1800 and parse_duration("2h") == 7200
    assert parse_duration("90s") == 90 and parse_duration("1d") == 86400
    with pytest.raises(ValueError):
        parse_duration("soon")


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
    assert "hooks" not in json.loads((tmp_path / "codex.json").read_text())


def test_install_noninteractive_without_url_fails(tmp_path, capsys):
    env = base_env(tmp_path)
    assert main(["install"], stdin=io.StringIO(""), stdout=io.StringIO(), env=env) == 2
    assert "webhook URL required" in capsys.readouterr().err


def test_mute_unmute_status(tmp_path):
    env = base_env(tmp_path)
    assert main(["mute", "2h", "--all"], stdout=io.StringIO(), env=env, now=lambda: 1000.0) == 0
    assert mute_state(Path(env["ADW_STATE_DIR"]), 1000.0 + 7199) == (True, True)
    out = io.StringIO()
    main(["status"], stdout=out, env=env, now=lambda: 1000.0)
    assert "mute: until" in out.getvalue() and "all" in out.getvalue()
    assert main(["unmute"], stdout=io.StringIO(), env=env) == 0
    assert mute_state(Path(env["ADW_STATE_DIR"]), 1000.0) == (False, False)


def test_mute_bad_duration_is_usage_error(tmp_path, capsys):
    env = base_env(tmp_path)
    assert main(["mute", "soon"], stdout=io.StringIO(), env=env) == 2
    assert "duration" in capsys.readouterr().err


def test_test_command_posts_two_blocks(tmp_path):
    with FakeDiscord() as fd:
        env = base_env(tmp_path) | {"ADW_WEBHOOK_URL": fd.url}
        out = io.StringIO()
        assert main(["test"], stdout=out, env=env) == 0
        assert out.getvalue().strip() == "ok"
        assert fd.requests[0]["username"] == "Agents" and "needs permission" in fd.requests[0]["content"]


def test_test_command_reports_failure(tmp_path):
    with FakeDiscord(script=[404]) as fd:
        env = base_env(tmp_path) | {"ADW_WEBHOOK_URL": fd.url}
        out = io.StringIO()
        assert main(["test"], stdout=out, env=env) == 1
        assert "failed" in out.getvalue()


def test_status_lists_sessions_and_log(tmp_path):
    env = base_env(tmp_path)
    sd = state_dir(env)
    store = SessionStore(sd, logging.getLogger("t"))
    with store.locked() as s:
        store.touch(s, "S1", "claude", "/home/u/proj", 1000.0)
    (sd / "log").write_text("2026-10-22 DEBUG hello-log-line\n")
    out = io.StringIO()
    main(["status", "--log"], stdout=out, env=env, now=lambda: 1001.0)
    assert "[1] claude /home/u/proj" in out.getvalue() and "hello-log-line" in out.getvalue()
