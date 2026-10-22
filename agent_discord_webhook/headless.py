"""Decide whether a session is one nobody is watching."""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import PurePath

AGENT_NAMES = ("claude", "codex")

ProcReader = Callable[[int], tuple[int, str, str] | None]


def _agent_name(token: str) -> str | None:
    base = PurePath(token).name
    base = base.removesuffix(".js")
    return base if base in AGENT_NAMES else None


def is_agent_process(comm: str, cmdline: str) -> bool:
    if comm in AGENT_NAMES:
        return True
    tokens = cmdline.split()
    if not tokens:
        return False
    if _agent_name(tokens[0]):
        return True
    return PurePath(tokens[0]).name == "node" and len(tokens) > 1 and _agent_name(tokens[1]) is not None


def _agent_of(comm: str, cmdline: str) -> str | None:
    if comm in AGENT_NAMES:
        return comm
    tokens = cmdline.split()
    for t in tokens[:2]:
        name = _agent_name(t)
        if name:
            return name
    return None


def ancestor_agents(pid: int, read_proc: ProcReader) -> list[str]:
    found: list[str] = []
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        info = read_proc(pid)
        if info is None:
            break
        ppid, comm, cmdline = info
        if is_agent_process(comm, cmdline):
            found.append(_agent_of(comm, cmdline) or comm)
        pid = ppid
    return found


def default_read_proc(pid: int) -> tuple[int, str, str] | None:
    try:
        stat = open(f"/proc/{pid}/stat").read()
        cmdline = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return None
    lparen, rparen = stat.find("("), stat.rfind(")")
    comm = stat[lparen + 1 : rparen]
    ppid = int(stat[rparen + 2 :].split()[1])
    return ppid, comm, cmdline.strip()


def has_controlling_tty() -> bool:
    try:
        fd = os.open("/dev/tty", os.O_RDONLY | os.O_NOCTTY)
    except OSError:
        return False
    os.close(fd)
    return True


def headless_reason(
    tool: str,
    payload: Mapping[str, object],
    env: Mapping[str, str],
    cwd: str,
    skip_cwd_patterns: Sequence[str],
    ancestors: Sequence[str],
    tty: bool,
) -> str | None:
    if tool == "claude":
        if env.get("CLAUDE_CODE_SESSION_ATTENDED") == "0" or env.get("CLAUDE_CODE_ENTRYPOINT") == "sdk-cli":
            return "env"
    if len(ancestors) >= 2:
        return "nested"
    if tool == "codex":
        for key in ("source", "originator"):
            value = payload.get(key)
            if isinstance(value, str) and "exec" in value:
                return "codex_exec"
    for pattern in skip_cwd_patterns:
        if fnmatch.fnmatch(cwd, pattern):
            return "skip_cwd"
    return None
