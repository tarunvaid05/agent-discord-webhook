from agent_discord_webhook.config import Config, get_logger, load_config, state_dir


def test_defaults_when_no_file(tmp_path):
    env = {"ADW_CONFIG_PATH": str(tmp_path / "config.toml"), "ADW_STATE_DIR": str(tmp_path / "state")}
    cfg = load_config(env)
    assert cfg == Config(webhook_url=None)
    assert cfg.tail_chars == 100 and cfg.min_turn_seconds == 45
    assert cfg.batch_window_seconds == 3.0 and cfg.always_notify is False


def test_file_values_and_env_override(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('webhook_url = "https://discord.com/api/webhooks/1/a"\ntail_chars = 50\nskip_cwd_patterns = ["/tmp/*"]\n')
    cfg = load_config({"ADW_CONFIG_PATH": str(p)})
    assert cfg.webhook_url == "https://discord.com/api/webhooks/1/a"
    assert cfg.tail_chars == 50 and cfg.skip_cwd_patterns == ("/tmp/*",)
    cfg2 = load_config({"ADW_CONFIG_PATH": str(p), "ADW_WEBHOOK_URL": "https://x/y"})
    assert cfg2.webhook_url == "https://x/y"


def test_empty_url_becomes_none(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('webhook_url = ""\n')
    assert load_config({"ADW_CONFIG_PATH": str(p)}).webhook_url is None
    assert load_config({"ADW_CONFIG_PATH": str(p), "ADW_WEBHOOK_URL": ""}).webhook_url is None


def test_state_dir_created_with_spool(tmp_path):
    d = state_dir({"ADW_STATE_DIR": str(tmp_path / "s")})
    assert (d / "spool").is_dir()


def test_logger_idempotent_and_writes(tmp_path):
    env = {"ADW_STATE_DIR": str(tmp_path)}
    log = get_logger(env)
    get_logger(env)
    assert len(log.handlers) == 1
    log.info("hello")
    log.handlers[0].flush()
    assert "INFO hello" in (tmp_path / "log").read_text()
