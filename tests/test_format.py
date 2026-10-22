from agent_discord_webhook.format import (
    Block,
    collapse_whitespace,
    format_duration,
    head,
    render_block,
    render_posts,
    strip_code_fences,
    tail,
    username_for,
)


def test_tail_short_unchanged():
    assert tail("hello world", 100) == "hello world"


def test_tail_cuts_at_word_boundary():
    s = "a" * 50 + " " + "b" * 95
    assert tail(s, 100) == "…" + "b" * 95
    assert tail("x" * 300, 100) == "…" + "x" * 100


def test_head_truncates_with_ellipsis():
    assert head("rm -rf build/ && make all", 10) == "rm -rf bui…"
    assert head("short", 10) == "short"


def test_collapse_and_fences():
    assert collapse_whitespace("a\n\n  b\t c") == "a b c"
    assert strip_code_fences("x ```py\ncode``` y") == "x `py\ncode` y"


def test_format_duration():
    assert format_duration(37) == "37s"
    assert format_duration(252) == "4m12s"
    assert format_duration(3720) == "1h02m"


def test_render_done_block_matches_spec():
    b = Block(2, "claude", "claude-text-updates", "done",
              "…wrote the migration and ran the tests, 14 passed. Ready for review.", 252)
    assert render_block(b) == ("**[2] claude · claude-text-updates** ✅ done · 4m12s\n"
                               "…wrote the migration and ran the tests, 14 passed. Ready for review.")


def test_render_permission_and_question():
    p = Block(1, "codex", "compas", "permission", "Bash: rm -rf build/ && make all")
    assert render_block(p) == "**[1] codex · compas** ⏸ needs permission\nBash: rm -rf build/ && make all"
    q = Block(3, "claude", "x", "question", "Claude is waiting for your response to a question.")
    assert render_block(q).startswith("**[3] claude · x** ❓ needs your answer\n")


def test_done_without_duration_has_no_dot():
    assert render_block(Block(1, "claude", "d", "done", "ok")) == "**[1] claude · d** ✅ done\nok"


def test_render_posts_joins_with_blank_line_and_splits_at_limit():
    blocks = [Block(i, "claude", "d", "done", "x" * 150) for i in range(1, 20)]
    posts = render_posts(blocks, limit=2000)
    assert len(posts) > 1
    assert all(len(p.content) <= 2000 for p in posts)
    assert [i for p in posts for i in p.block_indices] == list(range(19))
    assert "\n\n" in posts[0].content


def test_render_posts_hard_truncates_oversized_block():
    posts = render_posts([Block(1, "claude", "d", "permission", "y" * 3000)], limit=2000)
    assert len(posts) == 1 and len(posts[0].content) <= 2000


def test_username_for():
    c = Block(1, "claude", "d", "done", "a")
    x = Block(2, "codex", "d", "done", "a")
    assert username_for([c]) == "Claude" and username_for([x]) == "Codex" and username_for([c, x]) == "Agents"


def test_block_roundtrip():
    b = Block(1, "codex", "d", "permission", "Bash: ls", None)
    assert Block.from_dict(b.to_dict()) == b
