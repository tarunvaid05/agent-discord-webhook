"""Read a Claude Code transcript to find background tasks that have not reported back.

Claude Code runs subagents (Agent tool), `run_in_background` shells and workflows
asynchronously. The launch returns at once and the main agent usually ends its turn
with "waiting"; when the task finishes, Claude injects a `<task-notification>` user
turn naming the launching `<tool-use-id>` and `<task-id>`. While any launch has no
notification yet, the session is still busy even though Stop fired.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ASYNC_TOOLS = ("Agent", "Task", "Workflow")
_TASK_ID_IN_RESULT = re.compile(r"(?:agentId|with ID|run id|Run ID|task id)[:\s]+`?([A-Za-z0-9_-]+)")
_TOOL_USE_ID_TAG = re.compile(r"<tool-use-id>\s*([^<\s]+)\s*</tool-use-id>")
_TASK_ID_TAG = re.compile(r"<task-id>\s*([^<\s]+)\s*</task-id>")
_SCAN_HINTS = ("tool_use", "tool_result", "task-notification")


def _texts(content: object) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [x.get("text", "") for x in content if isinstance(x, dict) and x.get("type") == "text"]
    return []


def _is_async_launch(name: str, tool_input: object) -> bool:
    if name in ASYNC_TOOLS:
        return True
    return name == "Bash" and isinstance(tool_input, dict) and bool(tool_input.get("run_in_background"))


def pending_background_tasks(transcript_path: str | Path) -> int | None:
    """Number of async launches without a completion. None if the transcript is unreadable."""
    try:
        text = Path(transcript_path).read_text(errors="replace")
    except OSError:
        return None

    launched: dict[str, str | None] = {}  # tool_use_id -> task id when known
    done_tool_use: set[str] = set()
    done_task: set[str] = set()

    for line in text.splitlines():
        if not any(h in line for h in _SCAN_HINTS):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        content = d.get("message", {}).get("content") if isinstance(d.get("message"), dict) else None

        for t in _texts(content):
            if "<task-notification>" in t:
                done_tool_use.update(_TOOL_USE_ID_TAG.findall(t))
                done_task.update(_TASK_ID_TAG.findall(t))

        if not isinstance(content, list):
            continue
        for x in content:
            if not isinstance(x, dict):
                continue
            if x.get("type") == "tool_use":
                name = str(x.get("name", ""))
                tid = x.get("id")
                if name == "TaskStop":
                    inp = x.get("input")
                    if isinstance(inp, dict) and isinstance(inp.get("task_id"), str):
                        done_task.add(inp["task_id"])
                elif isinstance(tid, str) and _is_async_launch(name, x.get("input")):
                    launched[tid] = None
            elif x.get("type") == "tool_result":
                tid = x.get("tool_use_id")
                if not isinstance(tid, str) or tid not in launched:
                    continue
                body = " ".join(_texts(x.get("content")))
                m = _TASK_ID_IN_RESULT.search(body)
                if m:
                    launched[tid] = m.group(1)
                elif "background" not in body.lower() and "async" not in body.lower():
                    # The tool ran to completion synchronously; nothing to wait for.
                    del launched[tid]

    return sum(
        1 for tid, task in launched.items() if tid not in done_tool_use and (task is None or task not in done_task)
    )
