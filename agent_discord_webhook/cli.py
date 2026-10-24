"""`adw` command line."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import IO

from . import discord
from .config import config_path, get_logger, load_config, state_dir
from .format import Block, render_posts, username_for
from .hook import run_flusher, run_hook
from .install import adw_executable, is_installed, merge_hooks, remove_hooks, write_config, write_json_with_backup
from .state import SessionStore, clear_mute, mute_state, set_mute, spool_depth

DEFAULT_CLAUDE_SETTINGS = "~/.claude/settings.json"
DEFAULT_CODEX_HOOKS = "~/.codex/hooks.json"
URL_REQUIRED = "webhook URL required: pass --webhook-url or run interactively"
URL_NOT_HTTPS = "webhook URL must start with https://"
_DURATION = re.compile(r"^(\d+)([smhd])$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(s: str) -> float:
    m = _DURATION.match(s.strip())
    if not m:
        raise ValueError(f"bad duration {s!r}; use e.g. 30m, 2h, 1d")
    return float(int(m.group(1)) * _UNITS[m.group(2)])


def settings_files(env: Mapping[str, str]) -> dict[str, Path]:
    return {
        "claude": Path(env.get("ADW_CLAUDE_SETTINGS") or DEFAULT_CLAUDE_SETTINGS).expanduser(),
        "codex": Path(env.get("ADW_CODEX_HOOKS") or DEFAULT_CODEX_HOOKS).expanduser(),
    }


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text() or "{}")
    return data if isinstance(data, dict) else {}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="adw", description="Discord notifications for Claude Code and Codex CLI sessions")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("install", help="write config and register hooks in Claude Code and Codex CLI")
    i.add_argument("--webhook-url")
    sub.add_parser("uninstall", help="remove the hooks adw added; keeps config and state")
    sub.add_parser("test", help="post a sample two-block message")
    st = sub.add_parser("status", help="show config, hook state, sessions, mute, spool")
    st.add_argument("--log", action="store_true", help="also show the last 50 log lines")
    m = sub.add_parser("mute", help="silence 'done' notifications for a duration (30m, 2h, 1d)")
    m.add_argument("duration")
    m.add_argument("--all", action="store_true", help="also silence blockers")
    sub.add_parser("unmute")
    h = sub.add_parser("hook", help="hook entrypoint; reads the event JSON from stdin")
    h.add_argument("--tool", choices=("claude", "codex"), required=True)
    sub.add_parser("flush", help="internal: wait the batch window, then drain the spool to Discord")
    return p


def cmd_install(args: argparse.Namespace, stdin: IO[str], stdout: IO[str], env: Mapping[str, str], now_dt: datetime) -> int:
    url = args.webhook_url
    if not url:
        if not stdin.isatty():
            print(URL_REQUIRED, file=sys.stderr)
            return 2
        stdout.write("Discord webhook URL: ")
        stdout.flush()
        url = stdin.readline().strip()
        if not url:
            print(URL_REQUIRED, file=sys.stderr)
            return 2
    if not url.startswith("https://"):
        print(URL_NOT_HTTPS, file=sys.stderr)
        return 2
    write_config(config_path(env), url)
    state_dir(env)
    exe = adw_executable()
    for tool, path in settings_files(env).items():
        merged = merge_hooks(_read_json(path), exe, tool)
        backup = write_json_with_backup(path, merged, now_dt)
        if backup:
            print(f"backup: {backup}", file=stdout)
        print(f"installed: {path}", file=stdout)
    print(f"config: {config_path(env)}", file=stdout)
    return 0


def cmd_uninstall(stdout: IO[str], env: Mapping[str, str], now_dt: datetime) -> int:
    for path in settings_files(env).values():
        if not path.exists():
            continue
        data = _read_json(path)
        if not is_installed(data):
            continue
        write_json_with_backup(path, remove_hooks(data), now_dt)
        print(f"removed: {path}", file=stdout)
    return 0


def cmd_test(stdout: IO[str], env: Mapping[str, str]) -> int:
    config = load_config(env)
    log = get_logger(env)
    if not config.webhook_url:
        print("failed: no webhook URL configured (run `adw install`)", file=stdout)
        return 1
    blocks = [
        Block(1, "claude", "example-project", "done", "…ran the tests, 14 passed. Ready for review.", 252),
        Block(2, "codex", "example-project", "permission", "Bash: rm -rf build/ && make all"),
    ]
    for p in render_posts(blocks):
        if not discord.post(config.webhook_url, p.content, username_for(blocks), log):
            print("failed (see log)", file=stdout)
            return 1
    print("ok", file=stdout)
    return 0


def cmd_status(args: argparse.Namespace, stdout: IO[str], env: Mapping[str, str], now: float) -> int:
    config = load_config(env)
    sd = state_dir(env)
    cp = config_path(env)
    webhook = discord.redact(config.webhook_url) if config.webhook_url else "missing"
    print(f"config: {cp} (webhook: {webhook})", file=stdout)
    for tool, path in settings_files(env).items():
        try:
            state = "installed" if is_installed(_read_json(path)) else "not installed"
        except ValueError:
            state = "unreadable"
        print(f"{tool} hooks: {state} ({path})", file=stdout)
    muted, all_ = mute_state(sd, now)
    if muted:
        until = (sd / "mute_until").read_text().split()[0]
        stamp = datetime.fromtimestamp(float(until)).isoformat(timespec="minutes")
        print(f"mute: until {stamp}{' all' if all_ else ''}", file=stdout)
    else:
        print("mute: off", file=stdout)
    print(f"spool: {spool_depth(sd)}", file=stdout)
    sessions = SessionStore(sd, get_logger(env)).load()
    for s in sorted(sessions.values(), key=lambda x: x.alias):
        seen = datetime.fromtimestamp(s.last_seen_at).isoformat(timespec="seconds")
        print(f"[{s.alias}] {s.tool} {s.cwd} last seen {seen}", file=stdout)
    if args.log:
        log_path = sd / "log"
        if log_path.exists():
            lines = log_path.read_text().splitlines()[-50:]
            print("--- log ---", file=stdout)
            for line in lines:
                print(line, file=stdout)
    return 0


def main(
    argv: list[str] | None = None,
    *,
    stdin: IO[str] = sys.stdin,
    stdout: IO[str] = sys.stdout,
    env: Mapping[str, str] = os.environ,
    now=time.time,
) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "hook":
        return run_hook(args.tool, stdin, env, now=now, detach=True)
    if args.cmd == "flush":
        log = get_logger(env)
        try:
            run_flusher(state_dir(env), load_config(env), log)
        except Exception as e:
            log.error("flush failed (%s)", type(e).__name__)
        return 0
    if args.cmd == "install":
        return cmd_install(args, stdin, stdout, env, datetime.fromtimestamp(now()))
    if args.cmd == "uninstall":
        return cmd_uninstall(stdout, env, datetime.fromtimestamp(now()))
    if args.cmd == "test":
        return cmd_test(stdout, env)
    if args.cmd == "status":
        return cmd_status(args, stdout, env, now())
    if args.cmd == "mute":
        try:
            seconds = parse_duration(args.duration)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        set_mute(state_dir(env), now() + seconds, args.all)
        print(f"muted for {args.duration}{' (including blockers)' if args.all else ''}", file=stdout)
        return 0
    if args.cmd == "unmute":
        clear_mute(state_dir(env))
        print("unmuted", file=stdout)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
