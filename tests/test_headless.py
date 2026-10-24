from agent_discord_webhook.headless import ancestor_agents, has_codex_exec_ancestor, headless_reason, is_agent_process


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


def test_codex_exec_signal_comes_from_ancestry_not_payload():
    # Verified on codex 0.145: `codex exec` SessionStart carries source="startup", same as interactive.
    assert headless_reason("codex", {"source": "startup"}, {}, "/p", (), ["codex"], True, codex_exec=True) == "codex_exec"
    assert headless_reason("codex", {"source": "startup"}, {}, "/p", (), ["codex"], True, codex_exec=False) is None
    assert headless_reason("codex", {"source": "exec"}, {}, "/p", (), ["codex"], True) is None
    assert headless_reason("claude", {}, {}, "/p", (), ["claude"], True, codex_exec=True) is None


def test_has_codex_exec_ancestor():
    exec_chain = {60: (50, "adw", "/x/adw hook --tool codex"), 50: (40, "sh", "sh"),
                  40: (30, "codex", "/n/@openai/codex-linux-x64/vendor/bin/codex exec -m luna Reply pong"),
                  30: (20, "node", "node /n/bin/codex exec -m luna Reply pong"), 20: (1, "bash", "bash")}
    assert has_codex_exec_ancestor(60, lambda p: exec_chain.get(p))
    tui_chain = {60: (50, "adw", "/x/adw hook --tool codex"), 50: (40, "sh", "sh"),
                 40: (30, "codex", "/n/vendor/bin/codex -a untrusted -m luna"),
                 30: (20, "node", "node /n/bin/codex -a untrusted -m luna"), 20: (1, "bash", "bash")}
    assert not has_codex_exec_ancestor(60, lambda p: tui_chain.get(p))
    prompt_named_exec = {60: (50, "adw", "adw hook"), 50: (1, "codex", "/y/codex -m luna 'run exec please'")}
    assert not has_codex_exec_ancestor(60, lambda p: prompt_named_exec.get(p))
    claude_chain = {60: (50, "adw", "adw hook"), 50: (1, "claude", "claude exec")}
    assert not has_codex_exec_ancestor(60, lambda p: claude_chain.get(p))


def test_npm_codex_wrapper_and_native_child_count_as_one_host():
    procs = {60: (50, "adw", "/x/adw hook --tool codex"), 50: (40, "sh", "sh -c adw"),
             40: (30, "codex", "/n/node_modules/@openai/codex/vendor/codex"),
             30: (20, "node", "node /n/bin/codex"), 20: (1, "bash", "bash"), 1: (0, "systemd", "/sbin/init")}
    assert ancestor_agents(60, lambda p: procs.get(p)) == ["codex"]


def test_agent_run_from_shell_inside_same_agent_is_nested():
    procs = {60: (50, "adw", "/x/adw hook --tool codex"), 50: (40, "sh", "sh"), 40: (30, "codex", "/y/codex"),
             30: (20, "bash", "bash"), 20: (1, "codex", "/y/codex"), 1: (0, "systemd", "/sbin/init")}
    assert ancestor_agents(60, lambda p: procs.get(p)) == ["codex", "codex"]
