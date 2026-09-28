"""Environment loading and validation, and where runtime data lives."""

from __future__ import annotations

from pathlib import Path

import pytest

from caudal_bot import config as config_module
from caudal_bot.config import DEFAULT_DATA_DIR, Config

ALL_VARS = ("BOT_TOKEN", "GUILD_ID", "STAFF_ROLE_ID", "TICKETS_CATEGORY_ID", "FAQ_CHANNEL_IDS",
            "FAQ_COOLDOWN_SECONDS", "TRANSCRIPT_LOG_CHANNEL_ID", "ARCHIVE_CATEGORY_ID", "DATABASE_PATH",
            "SLA_ALERT_CHANNEL_ID", "DASHBOARD_AUTH_TOKEN", "DASHBOARD_HOST", "DASHBOARD_PORT",
            "HEALTH_HOST", "HEALTH_PORT")
# Only BOT_TOKEN is required; the IDs are the optional .env fallback for one server.
REQUIRED = {"BOT_TOKEN": "tok", "GUILD_ID": "1", "STAFF_ROLE_ID": "2", "TICKETS_CATEGORY_ID": "3"}


@pytest.fixture
def env(monkeypatch):
    """A clean environment holding only the required variables; returns a setter."""
    for name in ALL_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)
    return monkeypatch.setenv


def test_minimal_env_uses_documented_defaults(env):
    cfg = Config.from_env()
    assert (cfg.bot_token, cfg.guild_id, cfg.staff_role_id, cfg.tickets_category_id) == ("tok", 1, 2, 3)
    assert cfg.faq_channel_ids == frozenset() and cfg.faq_cooldown == 30.0
    assert cfg.transcript_log_channel_id is None and cfg.archive_category_id is None
    assert cfg.database_path == "tickets.db" and cfg.sla_alert_channel_id is None
    assert (cfg.dashboard_token, cfg.dashboard_host, cfg.dashboard_port) == ("", "127.0.0.1", 8080)
    assert (cfg.health_host, cfg.health_port) == ("127.0.0.1", 8080)   # health endpoint on by default
    assert not cfg.health_on_dashboard                                  # dashboard is off


def test_optional_values_are_parsed(env):
    env("FAQ_CHANNEL_IDS", "10, 11,12,")
    env("FAQ_COOLDOWN_SECONDS", "2.5")
    env("ARCHIVE_CATEGORY_ID", "6")
    env("DASHBOARD_PORT", "9000")
    env("DASHBOARD_AUTH_TOKEN", "  s3cret  ")
    cfg = Config.from_env()
    assert cfg.faq_channel_ids == {10, 11, 12} and cfg.faq_cooldown == 2.5
    assert cfg.archive_category_id == 6 and cfg.dashboard_port == 9000 and cfg.dashboard_token == "s3cret"


def test_bot_token_is_the_only_required_variable(env, monkeypatch):
    for name in ("GUILD_ID", "STAFF_ROLE_ID", "TICKETS_CATEGORY_ID"):
        monkeypatch.delenv(name)
    cfg = Config.from_env()      # every server is then configured with /setup
    assert (cfg.guild_id, cfg.staff_role_id, cfg.tickets_category_id) == (None, None, None)
    monkeypatch.delenv("BOT_TOKEN")
    with pytest.raises(RuntimeError, match="BOT_TOKEN"):
        Config.from_env()


@pytest.mark.parametrize("name", ["STAFF_ROLE_ID", "TICKETS_CATEGORY_ID", "ARCHIVE_CATEGORY_ID",
                                  "TRANSCRIPT_LOG_CHANNEL_ID", "SLA_ALERT_CHANNEL_ID", "FAQ_CHANNEL_IDS"])
def test_server_ids_without_guild_id_are_rejected(env, monkeypatch, name):
    """They'd be ambiguous with several servers: which one do they belong to?"""
    for n in ("GUILD_ID", "STAFF_ROLE_ID", "TICKETS_CATEGORY_ID"):
        monkeypatch.delenv(n)
    env(name, "5")
    with pytest.raises(RuntimeError, match=f"{name} only apply to GUILD_ID"):
        Config.from_env()


def test_blank_token_counts_as_missing(env):
    env("BOT_TOKEN", "   ")
    with pytest.raises(RuntimeError, match="BOT_TOKEN"):
        Config.from_env()


@pytest.mark.parametrize("name", ["GUILD_ID", "STAFF_ROLE_ID", "TICKETS_CATEGORY_ID", "ARCHIVE_CATEGORY_ID",
                                  "TRANSCRIPT_LOG_CHANNEL_ID", "SLA_ALERT_CHANNEL_ID"])
def test_ids_must_be_integers(env, name):
    env(name, "12abc")
    with pytest.raises(RuntimeError, match=f"{name} must be an integer"):
        Config.from_env()


@pytest.mark.parametrize("value, message", [("-1", ">= 0"), ("soon", "must be a number")])
def test_invalid_cooldown_is_rejected(env, value, message):
    env("FAQ_COOLDOWN_SECONDS", value)
    with pytest.raises(RuntimeError, match=message):
        Config.from_env()


def test_zero_cooldown_is_allowed(env):
    env("FAQ_COOLDOWN_SECONDS", "0")
    assert Config.from_env().faq_cooldown == 0


def test_malformed_channel_list_is_rejected(env):
    env("FAQ_CHANNEL_IDS", "10,general")
    with pytest.raises(RuntimeError, match="comma-separated"):
        Config.from_env()


@pytest.mark.parametrize("port", ["0", "-5", "65536", "100000"])
def test_out_of_range_ports_are_rejected(env, port):
    env("DASHBOARD_PORT", port)
    with pytest.raises(RuntimeError, match="between 1 and 65535"):
        Config.from_env()


@pytest.mark.parametrize("port", ["1", "8080", "65535"])
def test_boundary_ports_are_accepted(env, port):
    env("DASHBOARD_PORT", port)
    assert Config.from_env().dashboard_port == int(port)


def test_non_numeric_port_is_rejected(env):
    env("DASHBOARD_PORT", "http")
    with pytest.raises(RuntimeError, match="DASHBOARD_PORT must be an integer"):
        Config.from_env()


def test_dotenv_is_isolated_in_tests(monkeypatch):
    """The project's real .env (with a live token) must never reach a test."""
    for name in ALL_VARS:
        monkeypatch.delenv(name, raising=False)
    assert config_module.load_dotenv() is False  # the autouse guard's stub
    with pytest.raises(RuntimeError, match="BOT_TOKEN"):
        Config.from_env()


def test_default_data_dir_is_the_project_root():
    # Where bot.py lived before the package refactor, so existing data is still found.
    assert DEFAULT_DATA_DIR == Path(config_module.__file__).resolve().parent.parent
    assert (DEFAULT_DATA_DIR / "caudal_bot" / "config.py").is_file()


def test_relative_database_path_resolves_against_data_dir(make_config, tmp_path):
    cfg = make_config(database_path="data/metrics.db")
    assert cfg.database_file == tmp_path / "data" / "metrics.db"
    assert cfg.transcripts_dir == tmp_path / "transcripts" and cfg.backups_dir == tmp_path / "backups"


def test_absolute_database_path_is_kept(make_config, tmp_path):
    absolute = (tmp_path / "elsewhere" / "t.db").resolve()
    assert make_config(database_path=str(absolute)).database_file == absolute


def test_config_is_immutable(make_config):
    with pytest.raises(AttributeError):
        make_config().guild_id = 2


def test_health_endpoint_config(env):
    env("HEALTH_PORT", "8081")
    env("HEALTH_HOST", "0.0.0.0")
    cfg = Config.from_env()
    assert (cfg.health_host, cfg.health_port) == ("0.0.0.0", 8081)
    for off in ("off", "OFF", "0", "false", "disabled"):
        env("HEALTH_PORT", off)
        assert Config.from_env().health_port is None
    env("HEALTH_PORT", "")
    assert Config.from_env().health_port == 8080
    env("HEALTH_PORT", "70000")
    with pytest.raises(RuntimeError, match="HEALTH_PORT must be between 1 and 65535"):
        Config.from_env()


def test_health_shares_the_dashboard_port_only_when_the_dashboard_runs(env):
    env("DASHBOARD_AUTH_TOKEN", "x" * 20)
    assert Config.from_env().health_on_dashboard          # both default to 8080
    env("HEALTH_PORT", "8081")
    assert not Config.from_env().health_on_dashboard      # separate servers
