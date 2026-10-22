import logging

import pytest

from agent_discord_webhook.state import (
    SessionStore,
    allocate_alias,
    clear_mute,
    flock,
    mute_state,
    set_mute,
)


def test_allocate_alias_lowest_free():
    assert allocate_alias([]) == 1
    assert allocate_alias([1, 2, 4]) == 3


def test_touch_assigns_stable_alias_and_releases_on_end(tmp_path):
    store = SessionStore(tmp_path, logging.getLogger("t"))
    s = {}
    a = store.touch(s, "A", "claude", "/p/one", 100.0)
    b = store.touch(s, "B", "codex", "/p/one", 101.0)
    assert (a.alias, b.alias) == (1, 2)
    assert store.touch(s, "A", "claude", "/p/one", 102.0).alias == 1
    assert s["A"].last_seen_at == 102.0
    store.end(s, "A")
    assert store.touch(s, "C", "claude", "/p/two", 103.0).alias == 1


def test_sweep_drops_stale(tmp_path):
    store = SessionStore(tmp_path, logging.getLogger("t"))
    s = {}
    store.touch(s, "A", "claude", "/p", 0.0)
    store.touch(s, "B", "claude", "/p", 10.0)
    store.sweep(s, 10.0 + 12 * 3600)
    assert list(s) == ["B"]


def test_locked_roundtrip_and_corrupt_file(tmp_path, caplog):
    store = SessionStore(tmp_path, logging.getLogger("t"))
    with store.locked() as s:
        store.touch(s, "A", "claude", "/p", 1.0)
    assert store.load()["A"].alias == 1
    (tmp_path / "sessions.json").write_text("{not json")
    with caplog.at_level(logging.WARNING, logger="t"):
        assert store.load() == {}
    assert "sessions.json" in caplog.text


def test_flock_nonblocking_raises_when_held(tmp_path):
    with flock(tmp_path / "lock"):
        with pytest.raises(BlockingIOError):
            with flock(tmp_path / "lock", blocking=False):
                pass


def test_mute_roundtrip(tmp_path):
    assert mute_state(tmp_path, 0.0) == (False, False)
    set_mute(tmp_path, 100.0, all_=False)
    assert mute_state(tmp_path, 50.0) == (True, False)
    assert mute_state(tmp_path, 150.0) == (False, False)
    set_mute(tmp_path, 100.0, all_=True)
    assert mute_state(tmp_path, 50.0) == (True, True)
    clear_mute(tmp_path)
    assert mute_state(tmp_path, 50.0) == (False, False)


def test_spool_append_drain_order_and_corrupt_skipped(tmp_path):
    from agent_discord_webhook.format import Block
    from agent_discord_webhook.state import spool_append, spool_depth, spool_drain

    (tmp_path / "spool").mkdir()
    log = logging.getLogger("t")
    p1 = spool_append(tmp_path, Block(1, "claude", "d", "done", "first"))
    p2 = spool_append(tmp_path, Block(2, "codex", "d", "done", "second"))
    bad = tmp_path / "spool" / "0000000000-1.json"
    bad.write_text("{")
    assert spool_depth(tmp_path) == 3
    drained = spool_drain(tmp_path, log)
    assert [b.body for _, b in drained] == ["first", "second"]
    assert [p for p, _ in drained] == [p1, p2]
    assert not bad.exists()


def test_try_flusher_lock_exclusive(tmp_path):
    from agent_discord_webhook.state import try_flusher_lock

    held = try_flusher_lock(tmp_path)
    assert held is not None
    assert try_flusher_lock(tmp_path) is None
    held.close()
    assert try_flusher_lock(tmp_path) is not None
