"""Merge and remove adw hook entries in agent settings files; write config."""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

MARKER = "adw hook"
HOOK_TIMEOUT = 30

CLAUDE_EVENTS: dict[str, str | None] = {
    "Stop": None,
    "Notification": "permission_prompt|idle_prompt|agent_needs_input|elicitation_dialog",
    "UserPromptSubmit": None,
    "SessionStart": None,
    "SessionEnd": None,
}
# Codex 0.145 rejects async hooks ("async hooks are not supported yet"), so no async flag.
CODEX_EVENTS: dict[str, str | None] = {
    "Stop": None,
    "PermissionRequest": None,
    "UserPromptSubmit": None,
    "SessionStart": None,
    "SessionEnd": None,
}

CONFIG_TEMPLATE = """\
webhook_url = {url}
tail_chars = 100
min_turn_seconds = 45
batch_window_seconds = 3
always_notify = false
# skip_cwd_patterns = ["/tmp/*"]
"""


def hook_entry(adw_path: str, tool: str, matcher: str | None = None) -> dict:
    hook = {"type": "command", "command": f"{adw_path} hook --tool {tool}", "timeout": HOOK_TIMEOUT}
    if tool == "claude":
        hook["async"] = True
    entry: dict = {"hooks": [hook]}
    if matcher is not None:
        entry = {"matcher": matcher, **entry}
    return entry


def _is_adw(hook: dict) -> bool:
    return isinstance(hook, dict) and MARKER in str(hook.get("command", ""))


def remove_hooks(settings: dict) -> dict:
    out = copy.deepcopy(settings)
    hooks = out.get("hooks")
    if not isinstance(hooks, dict):
        return out
    for event in list(hooks):
        groups = []
        for group in hooks[event] or []:
            kept = [h for h in group.get("hooks", []) if not _is_adw(h)]
            if kept:
                groups.append({**group, "hooks": kept})
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]
    if not hooks:
        del out["hooks"]
    return out


def merge_hooks(settings: dict, adw_path: str, tool: str) -> dict:
    out = remove_hooks(settings)
    events = CLAUDE_EVENTS if tool == "claude" else CODEX_EVENTS
    hooks = out.setdefault("hooks", {})
    for event, matcher in events.items():
        hooks.setdefault(event, []).append(hook_entry(adw_path, tool, matcher))
    return out


def is_installed(settings: dict) -> bool:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    return any(_is_adw(h) for groups in hooks.values() for g in groups or [] for h in g.get("hooks", []))


def write_json_with_backup(path: Path, data: dict, now: datetime) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{now:%Y%m%d-%H%M%S}")
        shutil.copy2(path, backup)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return backup


_URL_LINE = re.compile(r"^\s*webhook_url\s*=.*$", re.MULTILINE)


def render_config(existing: str | None, webhook_url: str) -> str:
    """Fresh config from the template, or the existing file with only webhook_url replaced."""
    line = f"webhook_url = {json.dumps(webhook_url)}"
    if existing is None:
        return CONFIG_TEMPLATE.format(url=json.dumps(webhook_url))
    if _URL_LINE.search(existing):
        return _URL_LINE.sub(lambda _: line, existing, count=1)
    return f"{line}\n{existing}"


def write_config(path: Path, webhook_url: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    existing = path.read_text() if path.is_file() else None
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(render_config(existing, webhook_url))
    os.chmod(path, 0o600)


def adw_executable() -> str:
    found = shutil.which("adw")
    if found:
        return str(Path(found).resolve())
    return str(Path(sys.argv[0]).resolve())
