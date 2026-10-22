"""On-disk state: sessions and aliases, mute, locks."""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import json
import logging
import os
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import IO

STALE_SECONDS = 12 * 3600
SESSIONS_FILE = "sessions.json"
STATE_LOCK = "state.lock"
MUTE_FILE = "mute_until"


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
