import pytest

from assistant_bridge.config import ConfigError, load_config
from assistant_bridge.store import Store

CONFIG = """
claude_command = ["claude"]
db_path = "data/db.sqlite3"

[[profiles]]
name = "personal"
bot_token_env = "TG_A"
owner_ids = [42]
model = "sonnet"
"""


def write(tmp_path, text):
    p = tmp_path / "config.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_load_config_resolves_paths_and_defaults(tmp_path):
    cfg = load_config(write(tmp_path, CONFIG), {"TG_A": "token-a"})
    (p,) = cfg.profiles
    assert p.bot_token == "token-a"
    assert p.owner_ids == {42}
    assert p.workspace == (tmp_path / "profiles" / "personal").resolve()
    assert cfg.db_path == (tmp_path / "data" / "db.sqlite3").resolve()
    assert "WebSearch" in p.allowed_tools
    assert cfg.max_concurrent_runs == 2


def test_missing_token_env_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="TG_A"):
        load_config(write(tmp_path, CONFIG), {})


def test_bare_is_rejected(tmp_path):
    text = CONFIG.replace('claude_command = ["claude"]', 'claude_command = ["claude", "--bare"]')
    with pytest.raises(ConfigError, match="--bare"):
        load_config(write(tmp_path, text), {"TG_A": "t"})


def test_duplicate_profile_names_rejected(tmp_path):
    text = CONFIG + CONFIG[CONFIG.index("[[profiles]]") :].replace("TG_A", "TG_B")
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(write(tmp_path, text), {"TG_A": "a", "TG_B": "b"})


def test_store_conversation_lifecycle():
    store = Store(":memory:")
    conv = store.get_or_create("p", 1, 0)
    assert not conv.started
    assert store.get_or_create("p", 1, 0).session_id == conv.session_id

    store.mark_started("p", 1, 0, conv.session_id)
    assert store.get_conversation("p", 1, 0).started

    reset = store.reset_session("p", 1, 0)
    assert reset.session_id != conv.session_id
    assert not reset.started

    store.set_model("p", 1, 0, "opus")
    assert store.get_conversation("p", 1, 0).model == "opus"

    other_topic = store.get_or_create("p", 1, 7, title="Research")
    assert other_topic.session_id != reset.session_id
    assert other_topic.title == "Research"


def test_store_runs_and_offsets():
    store = Store(":memory:")
    conv = store.get_or_create("p", 1, 0)
    store.record_run(conv, started_at="t", ok=True, cost_usd=0.5, duration_ms=1, error=None, prompt="hi")
    store.record_run(conv, started_at="t", ok=False, cost_usd=None, duration_ms=None, error="x", prompt="hi")
    stats = store.run_stats("p", 1, 0)
    assert (stats.runs, stats.failed, stats.cost_usd) == (2, 1, 0.5)

    assert store.get_offset("p") is None
    store.set_offset("p", 10)
    store.set_offset("p", 11)
    assert store.get_offset("p") == 11
