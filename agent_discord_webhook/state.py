"""On-disk state: sessions and aliases, mute, locks."""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import json
import logging
import os
import tempfile
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import IO

from .format import Block

STALE_SECONDS = 12 * 3600
SESSIONS_FILE = "sessions.json"
STATE_LOCK = "state.lock"
MUTE_FILE = "mute_until"
SPOOL_DIR = "spool"
FLUSHER_LOCK = "lock"


@dataclasses.dataclass
class Session:
    alias: int
    tool: str
    cwd: str
    last_seen_at: float
    last_prompt_at: float | None = None
    last_stop_at: float | None = None
    last_assistant_message: str = ""
    notified_for_stop: bool = False
    auto_turn: bool = False  # current turn was started by an injected prompt (task notification), not a person
    headless: bool = False  # classified unattended once (codex exec); later events are dropped

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Session:
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


def allocate_alias(taken: Iterable[int]) -> int:
    used = set(taken)
    n = 1
    while n in used:
        n += 1
    return n


@contextlib.contextmanager
def flock(path: Path, blocking: bool = True) -> Iterator[IO[str]]:
    f = open(path, "a+")
    try:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        fcntl.flock(f.fileno(), flags)
        yield f
    finally:
        f.close()


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class SessionStore:
    def __init__(self, state_dir: Path, log: logging.Logger) -> None:
        self.state_dir = state_dir
        self.log = log
        self.path = state_dir / SESSIONS_FILE

    def load(self) -> dict[str, Session]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
            return {sid: Session.from_dict(d) for sid, d in data.items()}
        except (ValueError, TypeError, KeyError) as e:
            self.log.warning("%s unreadable (%s); starting empty", SESSIONS_FILE, type(e).__name__)
            return {}

    def save(self, sessions: dict[str, Session]) -> None:
        _atomic_write(self.path, json.dumps({sid: s.to_dict() for sid, s in sessions.items()}, indent=1))

    @contextlib.contextmanager
    def locked(self) -> Iterator[dict[str, Session]]:
        with flock(self.state_dir / STATE_LOCK):
            sessions = self.load()
            yield sessions
            self.save(sessions)

    def touch(self, sessions: dict[str, Session], session_id: str, tool: str, cwd: str, now: float) -> Session:
        s = sessions.get(session_id)
        if s is None:
            s = Session(alias=allocate_alias(x.alias for x in sessions.values()), tool=tool, cwd=cwd, last_seen_at=now)
            sessions[session_id] = s
        s.tool, s.cwd, s.last_seen_at = tool, cwd, now
        return s

    def mark_headless(self, sessions: dict[str, Session], session_id: str, tool: str, cwd: str, now: float) -> None:
        """Remember an unattended session without spending an alias on it."""
        s = sessions.get(session_id)
        if s is None:
            s = Session(alias=0, tool=tool, cwd=cwd, last_seen_at=now, headless=True)
            sessions[session_id] = s
        s.tool, s.cwd, s.last_seen_at, s.headless = tool, cwd, now, True

    def end(self, sessions: dict[str, Session], session_id: str) -> None:
        sessions.pop(session_id, None)

    def sweep(self, sessions: dict[str, Session], now: float) -> None:
        for sid in [sid for sid, s in sessions.items() if now - s.last_seen_at > STALE_SECONDS]:
            del sessions[sid]


def set_mute(state_dir: Path, until: float, all_: bool) -> None:
    (state_dir / MUTE_FILE).write_text(f"{until}{' all' if all_ else ''}\n")


def clear_mute(state_dir: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        (state_dir / MUTE_FILE).unlink()


def mute_state(state_dir: Path, now: float) -> tuple[bool, bool]:
    try:
        parts = (state_dir / MUTE_FILE).read_text().split()
        until = float(parts[0])
    except (FileNotFoundError, ValueError, IndexError):
        return (False, False)
    if now >= until:
        return (False, False)
    return (True, len(parts) > 1 and parts[1] == "all")


def spool_append(state_dir: Path, block: Block) -> Path:
    spool = state_dir / SPOOL_DIR
    spool.mkdir(exist_ok=True)
    path = spool / f"{time.time_ns()}-{os.getpid()}.json"
    _atomic_write(path, json.dumps(block.to_dict()))
    return path


def spool_depth(state_dir: Path) -> int:
    spool = state_dir / SPOOL_DIR
    return sum(1 for p in spool.glob("*.json")) if spool.is_dir() else 0


def spool_drain(state_dir: Path, log: logging.Logger) -> list[tuple[Path, Block]]:
    spool = state_dir / SPOOL_DIR
    out: list[tuple[Path, Block]] = []
    if not spool.is_dir():
        return out
    for path in sorted(spool.glob("*.json")):
        try:
            out.append((path, Block.from_dict(json.loads(path.read_text()))))
        except (ValueError, TypeError, KeyError) as e:
            log.warning("spool file %s unreadable (%s); dropped", path.name, type(e).__name__)
            with contextlib.suppress(OSError):
                path.unlink()
    return out


def try_flusher_lock(state_dir: Path) -> IO[str] | None:
    f = open(state_dir / FLUSHER_LOCK, "a+")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    return f
