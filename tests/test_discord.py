import logging

from agent_discord_webhook.discord import Outcome, post, redact
from tests.fakediscord import FakeDiscord


def test_success_payload_shape():
    with FakeDiscord() as fd:
        assert post(fd.url, "hi @everyone", "Claude", logging.getLogger("t"), sleep=lambda s: None) == Outcome.OK
        assert fd.requests == [{"content": "hi @everyone", "username": "Claude", "allowed_mentions": {"parse": []}}]


def test_429_then_success_sleeps_retry_after():
    slept = []
    with FakeDiscord(script=[429], retry_after=1.5) as fd:
        assert post(fd.url, "x", "Claude", logging.getLogger("t"), sleep=slept.append)
        assert slept == [1.5] and len(fd.requests) == 2


def test_5xx_retries_twice_then_fails():
    slept = []
    with FakeDiscord(script=[500, 502, 503]) as fd:
        assert post(fd.url, "x", "Claude", logging.getLogger("t"), sleep=slept.append) == Outcome.RETRYABLE
        assert slept == [2.0, 2.0] and len(fd.requests) == 3


def test_404_fails_without_retry_and_log_redacts_url(caplog):
    log = logging.getLogger("t")
    with FakeDiscord(script=[404]) as fd, caplog.at_level(logging.DEBUG, logger="t"):
        assert post(fd.url, "x", "Claude", log, sleep=lambda s: None) == Outcome.PERMANENT
        assert len(fd.requests) == 1
    assert "secret-token" not in caplog.text and "404" in caplog.text


def test_connection_refused_logs_without_url(caplog):
    url = "http://127.0.0.1:9/api/webhooks/123/secret"
    with caplog.at_level(logging.DEBUG, logger="t"):
        assert post(url, "x", "Claude", logging.getLogger("t"), sleep=lambda s: None) == Outcome.RETRYABLE
    assert "secret" not in caplog.text and caplog.text


def test_redact():
    assert redact("https://discord.com/api/webhooks/123/abc") == "https://discord.com/…"


def test_outcome_truthiness():
    assert Outcome.OK and not Outcome.RETRYABLE and not Outcome.PERMANENT
