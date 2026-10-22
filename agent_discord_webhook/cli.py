"""`adw` command line."""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Mapping
from typing import IO

from .config import get_logger, load_config, state_dir
from .hook import run_flusher, run_hook


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="adw", description="Discord notifications for Claude Code and Codex CLI sessions")
    sub = p.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("hook", help="hook entrypoint; reads the event JSON from stdin")
    h.add_argument("--tool", choices=("claude", "codex"), required=True)
    sub.add_parser("flush", help="internal: wait the batch window, then drain the spool to Discord")
    return p


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
        run_flusher(state_dir(env), load_config(env), get_logger(env))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
