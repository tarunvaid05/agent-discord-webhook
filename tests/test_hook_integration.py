import io
from agent_discord_webhook import discord
import json
import logging
import os
import subprocess
import time
from pathlib import Path

from agent_discord_webhook.config import load_config, state_dir
from agent_discord_webhook.hook import flush, run_hook
from agent_discord_webhook.state import spool_depth, try_flusher_lock
from tests.fakediscord import FakeDiscord

REPO_ROOT = Path(__file__).resolve().parent.parent
NOOP = dict(sleep=lambda s: None, read_proc=lambda p: None, tty=lambda: True)


def env_for(tmp_path, url):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'webhook_url = "{url}"\nmin_turn_seconds = 0\nbatch_window_seconds = 0\n')
    return {"ADW_CONFIG_PATH": str(cfg), "ADW_STATE_DIR": str(tmp_path / "state"),
            "CLAUDE_CODE_SESSION_ATTENDED": "1"}


def stop_payload(sid, msg, cwd="/home/u/proj"):
    return json.dumps({"hook_event_name": "Stop", "session_id": sid, "cwd": cwd, "last_assistant_message": msg})


def test_in_process_stop_posts_one_message(tmp_path):
    with FakeDiscord() as fd:
        env = env_for(tmp_path, fd.url)
        rc = run_hook("claude", io.StringIO(stop_payload("S1", "done thing")), env, **NOOP)
        assert rc == 0
        assert fd.requests[0]["content"] == "**[1] claude · proj** ✅ done\ndone thing"
        assert fd.requests[0]["username"] == "Claude"
    assert spool_depth(Path(env["ADW_STATE_DIR"])) == 0


def test_two_sessions_batch_into_one_post(tmp_path):
    with FakeDiscord() as fd:
        env = env_for(tmp_path, fd.url)
        sd = state_dir(env)
        held = try_flusher_lock(sd)
        for sid, msg in (("S1", "one"), ("S2", "two")):
            run_hook("claude", io.StringIO(stop_payload(sid, msg)), env, **NOOP)
        assert fd.requests == [] and spool_depth(sd) == 2
        held.close()
        flush(sd, load_config(env), logging.getLogger("t"), sleep=lambda s: None)
        assert len(fd.requests) == 1
        c = fd.requests[0]["content"]
        assert "[1] claude · proj" in c and "[2] claude · proj" in c and "\n\n" in c


def test_failed_post_leaves_spool_for_next_run(tmp_path):
    with FakeDiscord(script=[500, 500, 500]) as fd:
        env = env_for(tmp_path, fd.url)
        run_hook("claude", io.StringIO(stop_payload("S1", "x")), env, **NOOP)
        sd = Path(env["ADW_STATE_DIR"])
        assert spool_depth(sd) == 1
        run_hook("claude", io.StringIO(stop_payload("S1", "y")), env, **NOOP)
        assert spool_depth(sd) == 0 and len(fd.requests) == 4


def test_404_drops_spool(tmp_path):
    with FakeDiscord(script=[404]) as fd:
        env = env_for(tmp_path, fd.url)
        run_hook("claude", io.StringIO(stop_payload("S1", "x")), env, **NOOP)
        assert spool_depth(Path(env["ADW_STATE_DIR"])) == 0 and len(fd.requests) == 1


def test_headless_session_touches_nothing(tmp_path):
    with FakeDiscord() as fd:
        env = env_for(tmp_path, fd.url) | {"CLAUDE_CODE_SESSION_ATTENDED": "0"}
        run_hook("claude", io.StringIO(stop_payload("S1", "x")), env, **NOOP)
        sd = Path(env["ADW_STATE_DIR"])
        assert fd.requests == [] and not (sd / "sessions.json").exists()
        assert "headless" in (sd / "log").read_text()


def test_garbage_stdin_and_missing_url_exit_zero(tmp_path):
    env = env_for(tmp_path, "")
    assert run_hook("claude", io.StringIO("not json"), env, **NOOP) == 0
    assert run_hook("claude", io.StringIO(stop_payload("S1", "x")), env, **NOOP) == 0
    assert spool_depth(Path(env["ADW_STATE_DIR"])) == 0


def test_subprocess_adw_hook_prints_nothing_and_detached_flush_posts(tmp_path):
    with FakeDiscord() as fd:
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT")}
        env |= env_for(tmp_path, fd.url)
        t0 = time.monotonic()
        r = subprocess.run(["uv", "run", "adw", "hook", "--tool", "codex"], input=stop_payload("S9", "hello"),
                           capture_output=True, text=True, env=env, cwd=REPO_ROOT, timeout=60)
        assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
        deadline = time.monotonic() + 15
        while not fd.requests and time.monotonic() < deadline:
            time.sleep(0.1)
        assert fd.requests and fd.requests[0]["username"] == "Codex"
        assert spool_depth(Path(env["ADW_STATE_DIR"])) == 0


def test_flusher_drains_entries_spooled_while_it_was_posting(tmp_path):
    from agent_discord_webhook.format import Block
    from agent_discord_webhook.hook import run_flusher
    from agent_discord_webhook.state import spool_append

    with FakeDiscord() as fd:
        env = env_for(tmp_path, fd.url)
        sd = state_dir(env)
        spool_append(sd, Block(1, "claude", "proj", "done", "first"))
        late = iter([Block(2, "claude", "proj", "done", "late")])

        def post(url, content, username, log, **kw):
            for b in late:  # a hook fires while this POST is in flight; its flusher sees the lock and exits
                spool_append(sd, b)
            return discord.post(url, content, username, log, **kw)

        run_flusher(sd, load_config(env), logging.getLogger("t"), sleep=lambda s: None, post=post)
        assert spool_depth(sd) == 0
        assert [r["content"].split("\n")[-1] for r in fd.requests] == ["first", "late"]


def test_flusher_stops_looping_when_post_keeps_failing(tmp_path):
    from agent_discord_webhook.format import Block
    from agent_discord_webhook.hook import run_flusher
    from agent_discord_webhook.state import spool_append

    with FakeDiscord(script=[500] * 30) as fd:
        env = env_for(tmp_path, fd.url)
        sd = state_dir(env)
        spool_append(sd, Block(1, "claude", "proj", "done", "x"))
        run_flusher(sd, load_config(env), logging.getLogger("t"), sleep=lambda s: None)
        assert spool_depth(sd) == 1 and len(fd.requests) == 3


def test_detached_flusher_ignores_stdlib_shadowing_files_in_cwd(tmp_path):
    poison = tmp_path / "cwd"
    poison.mkdir()
    (poison / "json.py").write_text('raise SystemExit("shadowed stdlib json")\n')
    with FakeDiscord() as fd:
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT")}
        env |= env_for(tmp_path, fd.url)
        r = subprocess.run(["uv", "run", "--project", str(REPO_ROOT), "adw", "hook", "--tool", "codex"],
                           input=stop_payload("S9", "hello"), capture_output=True, text=True, env=env, cwd=poison,
                           timeout=60)
        assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
        deadline = time.monotonic() + 15
        while not fd.requests and time.monotonic() < deadline:
            time.sleep(0.1)
        assert fd.requests, "detached flusher never posted"


def test_hook_failure_log_names_exception_type_without_its_message(tmp_path):
    env = env_for(tmp_path, "https://discord.com/api/webhooks/1/secret-token")

    def exploding_post(url, *a, **k):
        raise RuntimeError(f"boom {url}")

    assert run_hook("claude", io.StringIO(stop_payload("S1", "x")), env, **NOOP | {"post": exploding_post}) == 0
    text = (Path(env["ADW_STATE_DIR"]) / "log").read_text()
    assert "RuntimeError" in text and "secret-token" not in text
