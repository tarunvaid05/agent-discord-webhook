"""Configuration, state directory, and logging setup."""

from __future__ import annotations

import dataclasses
import logging
import logging.handlers
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path

DEFAULT_CONFIG_PATH = "~/.config/agent-discord-webhook/config.toml"
DEFAULT_STATE_DIR = "~/.local/state/agent-discord-webhook"
LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"


@dataclasses.dataclass(frozen=True)
class Config:
    webhook_url: str | None
    tail_chars: int = 100
    min_turn_seconds: int = 45
    batch_window_seconds: float = 3.0
    always_notify: bool = False
    skip_cwd_patterns: tuple[str, ...] = ()


def config_path(env: Mapping[str, str] = os.environ) -> Path:
    return Path(env.get("ADW_CONFIG_PATH") or DEFAULT_CONFIG_PATH).expanduser()


def state_dir(env: Mapping[str, str] = os.environ) -> Path:
    d = Path(env.get("ADW_STATE_DIR") or DEFAULT_STATE_DIR).expanduser()
    (d / "spool").mkdir(parents=True, exist_ok=True)
    return d


def load_config(env: Mapping[str, str] = os.environ) -> Config:
    values: dict[str, object] = {}
    path = config_path(env)
    if path.is_file():
        with path.open("rb") as f:
            data = tomllib.load(f)
        for field in dataclasses.fields(Config):
            if field.name in data:
                values[field.name] = data[field.name]
    if "ADW_WEBHOOK_URL" in env:
        values["webhook_url"] = env["ADW_WEBHOOK_URL"]
    if not values.get("webhook_url"):
        values["webhook_url"] = None
    if "skip_cwd_patterns" in values:
        values["skip_cwd_patterns"] = tuple(values["skip_cwd_patterns"])  # type: ignore[arg-type]
    if "batch_window_seconds" in values:
        values["batch_window_seconds"] = float(values["batch_window_seconds"])  # type: ignore[arg-type]
    return Config(**values)  # type: ignore[arg-type]


def get_logger(env: Mapping[str, str] = os.environ) -> logging.Logger:
    log = logging.getLogger("adw")
    log.setLevel(logging.DEBUG)
    log.propagate = False
    target = state_dir(env) / "log"
    for h in log.handlers:
        if isinstance(h, logging.handlers.RotatingFileHandler) and Path(h.baseFilename) == target:
            return log
    for h in list(log.handlers):
        log.removeHandler(h)
        h.close()
    handler = logging.handlers.RotatingFileHandler(target, maxBytes=1_000_000, backupCount=1)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    log.addHandler(handler)
    return log
