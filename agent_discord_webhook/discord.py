"""Posting to a Discord webhook with retries. Never logs the webhook URL."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

USER_AGENT = "agent-discord-webhook"
NETWORK_RETRIES = 2
NETWORK_BACKOFF = 2.0


def redact(url: str) -> str:
    try:
        parts = urllib.parse.urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}/…"
    except ValueError:
        return "<url>"


def _safe_error(e: BaseException) -> str:
    reason = getattr(e, "reason", None)
    if isinstance(reason, BaseException):
        reason = f"{type(reason).__name__}: {reason}"
    text = f"{type(e).__name__}"
    if isinstance(reason, str) and "/" not in reason:
        text += f" ({reason})"
    return text


def post(
    webhook_url: str,
    content: str,
    username: str,
    log: logging.Logger,
    *,
    timeout: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    body = json.dumps({"content": content, "username": username, "allowed_mentions": {"parse": []}}).encode()
    safe = redact(webhook_url)
    rate_limited_once = False
    network_failures = 0
    while True:
        req = urllib.request.Request(
            webhook_url, data=body, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                resp.read()
            if 200 <= status < 300:
                log.debug("posted %d chars to %s (%d)", len(content), safe, status)
                return True
            log.error("unexpected status %d from %s", status, safe)
            return False
        except urllib.error.HTTPError as e:
            status = e.code
            if status == 429 and not rate_limited_once:
                rate_limited_once = True
                retry_after = 1.0
                try:
                    retry_after = float(json.loads(e.read().decode()).get("retry_after", 1.0))
                except (ValueError, AttributeError, TypeError):
                    pass
                log.warning("rate limited by %s; retrying after %.1fs", safe, retry_after)
                sleep(retry_after)
                continue
            if 500 <= status < 600 and network_failures < NETWORK_RETRIES:
                network_failures += 1
                log.warning("status %d from %s; retry %d", status, safe, network_failures)
                sleep(NETWORK_BACKOFF)
                continue
            log.error("post to %s failed with status %d", safe, status)
            return False
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if network_failures < NETWORK_RETRIES:
                network_failures += 1
                log.warning("network error %s posting to %s; retry %d", _safe_error(e), safe, network_failures)
                sleep(NETWORK_BACKOFF)
                continue
            log.error("post to %s failed: %s", safe, _safe_error(e))
            return False
