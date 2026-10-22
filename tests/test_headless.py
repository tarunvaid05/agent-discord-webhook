from agent_discord_webhook.headless import ancestor_agents, headless_reason, is_agent_process


def test_claude_env_signals():
    for env in ({"CLAUDE_CODE_SESSION_ATTENDED": "0"}, {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}):
        assert headless_reason("claude", {}, env, "/p", (), ["claude"], True) == "env"
    assert headless_reason("claude", {}, {"CLAUDE_CODE_SESSION_ATTENDED": "1"}, "/p", (), ["claude"], False) is None
    assert headless_reason("claude", {}, {}, "/p", (), ["claude"], False) is None


def test_child_session_flag_alone_is_not_headless():
    env = {"CLAUDE_CODE_SESSION_ATTENDED": "1", "CLAUDE_CODE_CHILD_SESSION": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"}
    assert headless_reason("claude", {}, env, "/p", (), ["claude"], False) is None


def test_nested_agent_is_headless():
    assert headless_reason("codex", {}, {}, "/p", (), ["codex", "claude"], False) == "nested"
    assert headless_reason("claude", {}, {}, "/p", (), ["claude"], False) is None
    assert headless_reason("claude", {}, {}, "/p", (), [], False) is None


def test_skip_cwd_patterns():
    assert headless_reason("claude", {}, {}, "/tmp/scratch/x", ("/tmp/scratch/*",), ["claude"], True) == "skip_cwd"


def test_is_agent_process():
    assert is_agent_process("claude", "/usr/bin/claude")
    assert is_agent_process("node", "node /home/u/.local/bin/claude --flag")
    assert is_agent_process("node", "/x/codex.js")
    assert not is_agent_process("bash", "/bin/bash")
    assert not is_agent_process("adw", "/x/adw hook --tool claude")


def test_ancestor_agents_walks_chain():
    procs = {50: (40, "adw", "adw hook"), 40: (30, "node", "node /x/claude"), 30: (20, "bash", "bash"),
             20: (1, "codex", "/y/codex"), 1: (0, "systemd", "/sbin/init")}
    assert ancestor_agents(50, lambda p: procs.get(p)) == ["claude", "codex"]


def test_codex_exec_signal():
    assert headless_reason("codex", {"source": "exec"}, {}, "/p", (), ["codex"], False) == "codex_exec"
    assert headless_reason("codex", {"originator": "codex_exec"}, {}, "/p", (), ["codex"], False) == "codex_exec"
    assert headless_reason("codex", {"source": "cli"}, {}, "/p", (), ["codex"], False) is None
    assert headless_reason("codex", {}, {}, "/p", (), ["codex"], False) is None
