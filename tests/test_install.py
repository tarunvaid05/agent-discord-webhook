import json
from datetime import datetime

from agent_discord_webhook.config import Config, load_config
from agent_discord_webhook.install import (
    is_installed,
    merge_hooks,
    remove_hooks,
    write_config,
    write_json_with_backup,
)

EXISTING = {"hooks": {
    "Stop": [{"hooks": [{"type": "command", "command": "/x/log-progress.sh", "timeout": 30, "async": True}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 /x/context-check.py", "timeout": 10}]}]},
    "permissions": {"allow": ["Bash(ls:*)"]}}


def test_merge_appends_and_keeps_existing():
    out = merge_hooks(EXISTING, "/opt/adw", "claude")
    assert out["permissions"] == EXISTING["permissions"]
    assert out["hooks"]["Stop"][0] == EXISTING["hooks"]["Stop"][0]
    assert out["hooks"]["Stop"][1]["hooks"][0] == {"type": "command", "command": "/opt/adw hook --tool claude", "async": True, "timeout": 30}
    assert out["hooks"]["Notification"][0]["matcher"] == "permission_prompt|idle_prompt|agent_needs_input|elicitation_dialog"
    assert set(out["hooks"]) == {"Stop", "UserPromptSubmit", "Notification", "SessionStart", "SessionEnd"}
    assert len(EXISTING["hooks"]["Stop"]) == 1


def test_merge_is_idempotent_and_updates_path():
    once = merge_hooks(EXISTING, "/opt/adw", "claude")
    twice = merge_hooks(once, "/new/adw", "claude")
    assert len(twice["hooks"]["Stop"]) == 2
    assert twice["hooks"]["Stop"][1]["hooks"][0]["command"] == "/new/adw hook --tool claude"


def test_remove_restores_original():
    assert remove_hooks(merge_hooks(EXISTING, "/opt/adw", "claude")) == EXISTING
    assert remove_hooks(merge_hooks({}, "/opt/adw", "codex")) == {}
    assert is_installed(merge_hooks({}, "/opt/adw", "codex")) and not is_installed(EXISTING)


def test_remove_keeps_foreign_hook_in_mixed_group():
    mixed = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/opt/adw hook --tool claude"},
                                           {"type": "command", "command": "/x/other.sh"}]}]}}
    assert remove_hooks(mixed) == {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/x/other.sh"}]}]}}


def test_codex_events_are_sync():
    out = merge_hooks({}, "/opt/adw", "codex")
    assert set(out["hooks"]) == {"Stop", "PermissionRequest", "UserPromptSubmit", "SessionStart", "SessionEnd"}
    entry = out["hooks"]["Stop"][0]["hooks"][0]
    assert entry["command"] == "/opt/adw hook --tool codex" and "async" not in entry and entry["timeout"] == 30


def test_write_json_with_backup(tmp_path):
    p = tmp_path / "a" / "settings.json"
    assert write_json_with_backup(p, {"a": 1}, datetime(2026, 10, 22, 14, 5, 9)) is None
    bak = write_json_with_backup(p, {"a": 2}, datetime(2026, 10, 22, 14, 5, 9))
    assert bak == tmp_path / "a" / "settings.json.bak-20261022-140509"
    assert json.loads(bak.read_text()) == {"a": 1} and json.loads(p.read_text()) == {"a": 2}


def test_write_config_mode_and_content(tmp_path):
    p = tmp_path / "c" / "config.toml"
    write_config(p, "https://discord.com/api/webhooks/1/x")
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    assert load_config({"ADW_CONFIG_PATH": str(p)}) == Config(webhook_url="https://discord.com/api/webhooks/1/x")
