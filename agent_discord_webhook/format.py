"""Pure message formatting: blocks, posts, truncation."""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Sequence

ELLIPSIS = "…"
DISCORD_LIMIT = 2000
BOUNDARY_WINDOW = 20

LABELS = {
    "done": "✅ done",
    "permission": "⏸ needs permission",
    "question": "❓ needs your answer",
}

_WS = re.compile(r"\s+")
_FENCE = re.compile(r"`{3,}")


@dataclasses.dataclass(frozen=True)
class Block:
    alias: int
    tool: str
    dirname: str
    kind: str
    body: str
    duration_seconds: float | None = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Block:
        return cls(
            alias=int(d["alias"]),
            tool=str(d["tool"]),
            dirname=str(d["dirname"]),
            kind=str(d["kind"]),
            body=str(d["body"]),
            duration_seconds=None if d.get("duration_seconds") is None else float(d["duration_seconds"]),
        )


@dataclasses.dataclass(frozen=True)
class Post:
    content: str
    block_indices: tuple[int, ...]


def collapse_whitespace(s: str) -> str:
    return _WS.sub(" ", s).strip()


def strip_code_fences(s: str) -> str:
    return _FENCE.sub("`", s)


def tail(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    piece = s[-n:]
    space = piece.find(" ", 0, BOUNDARY_WINDOW)
    if space != -1:
        piece = piece[space + 1 :]
    return ELLIPSIS + piece


def head(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[:n] + ELLIPSIS


def format_duration(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{sec:02d}s"
    return f"{sec}s"


def render_block(b: Block) -> str:
    label = LABELS[b.kind]
    if b.kind == "done" and b.duration_seconds is not None:
        label += f" · {format_duration(b.duration_seconds)}"
    return f"**[{b.alias}] {b.tool} · {b.dirname}** {label}\n{b.body}"


def render_posts(blocks: Sequence[Block], limit: int = DISCORD_LIMIT) -> list[Post]:
    posts: list[Post] = []
    current: list[str] = []
    indices: list[int] = []
    for i, b in enumerate(blocks):
        text = render_block(b)
        if len(text) > limit:
            text = text[: limit - 1] + ELLIPSIS
        joined_len = sum(len(t) for t in current) + 2 * len(current) + len(text)
        if current and joined_len > limit:
            posts.append(Post("\n\n".join(current), tuple(indices)))
            current, indices = [], []
        current.append(text)
        indices.append(i)
    if current:
        posts.append(Post("\n\n".join(current), tuple(indices)))
    return posts


def username_for(blocks: Sequence[Block]) -> str:
    tools = {b.tool for b in blocks}
    if tools == {"claude"}:
        return "Claude"
    if tools == {"codex"}:
        return "Codex"
    return "Agents"
