"""Shared fixtures. The autouse guard makes every test offline by construction."""

from __future__ import annotations

import pytest
import discord

import helpers as h
from caudal_bot import config as config_module
from caudal_bot.config import Config
from caudal_bot.database import TicketDB
from caudal_bot.main import FAQBot


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """No test may read the real .env or contact Discord.

    load_dotenv is disabled (so the project's real BOT_TOKEN can't leak in), and every
    route to Discord (login, gateway connect, start, any REST request) raises.
    """
    monkeypatch.setattr(config_module, "load_dotenv", lambda *a, **k: False)

    async def refuse(*_a, **_k):
        raise AssertionError("a test tried to contact Discord")
    for name in ("login", "connect", "start"):
        monkeypatch.setattr(discord.Client, name, refuse)
    monkeypatch.setattr(discord.http.HTTPClient, "request", refuse)


@pytest.fixture
def make_config(tmp_path):
    """Config factory; all runtime data goes under this test's tmp_path."""
    def factory(**overrides) -> Config:
        # health_port=None: the real default (8080) may be in use by a bot running on this machine.
        base = dict(bot_token="test-token", guild_id=h.GUILD_ID, staff_role_id=h.STAFF_ROLE_ID,
                    tickets_category_id=h.TICKETS_CAT, data_dir=tmp_path, health_port=None)
        return Config(**(base | overrides))
    return factory


@pytest.fixture
def bot(make_config, monkeypatch):
    """A FAQBot that believes it is logged in as helpers.ME, with staff = helpers.STAFF."""
    monkeypatch.setattr(FAQBot, "user", property(lambda self: h.ME))
    b = FAQBot(make_config())
    b.is_staff = lambda u: getattr(u, "id", None) == h.STAFF_ID
    users = {u.id: u for u in (h.OWNER, h.STAFF, h.OUTSIDER)}
    b.get_user = lambda uid: users.get(uid)
    b.transcript_channel = lambda _guild: None
    return b


@pytest.fixture
async def db(tmp_path):
    database = await TicketDB.open(tmp_path / "test.db")
    yield database
    await database.close()


@pytest.fixture
async def db_bot(bot, db):
    """`bot` with a real SQLite database attached."""
    bot.db = db
    yield bot
    bot.db = None  # the db fixture closes the connection itself
