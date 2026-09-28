"""Self-provisioning across servers: per-server settings (cache, .env fallback, legacy
migration), /setup, /set-faq-channels, and the on-the-fly fallbacks when a server
hasn't been set up (or something was deleted)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import helpers as h
from caudal_bot.guild_settings import GuildSettings, GuildSettingsStore
from caudal_bot.main import FAQBot
from caudal_bot.provisioning import (
    ARCHIVE_CATEGORY_NAME, SLA_ALERTS_CHANNEL_NAME, TICKETS_CATEGORY_NAME, TRANSCRIPTS_CHANNEL_NAME,
)

OTHER = 500          # a server that isn't GUILD_ID, so .env never applies to it
STAFF = 77


@pytest.fixture
async def bot(make_config, db, monkeypatch):
    monkeypatch.setattr(FAQBot, "user", property(lambda self: h.ME))
    b = FAQBot(make_config())
    b.db = db
    yield b
    b.db = None


def admin_in(guild):
    return h.member_in(guild, 7, admin=True, name="admin")


# ---- settings store ----------------------------------------------------------------------

def test_env_ids_apply_to_guild_id_only(bot):
    home = bot.settings.get(h.GUILD_ID)
    assert (home.staff_role_id, home.tickets_category_id) == (h.STAFF_ROLE_ID, h.TICKETS_CAT)
    assert bot.settings.get(OTHER) == GuildSettings()           # nothing configured there
    assert not bot.settings.is_configured(h.GUILD_ID)


async def test_first_change_keeps_the_env_fallback_for_other_fields(bot):
    await bot.settings.update(h.GUILD_ID, faq_channel_ids=frozenset({55}))
    s = bot.settings.get(h.GUILD_ID)
    assert s.faq_channel_ids == {55} and s.staff_role_id == h.STAFF_ROLE_ID and s.tickets_category_id == h.TICKETS_CAT
    assert bot.settings.is_configured(h.GUILD_ID)


async def test_settings_survive_a_restart(make_config, db):
    first = FAQBot(make_config())
    first.db = db
    await first.settings.update(OTHER, staff_role_id=STAFF, faq_channel_ids=frozenset({3, 1, 2}))
    assert (await db.all_guild_settings())[OTHER]["faq_channel_ids"] == "1,2,3"
    second = FAQBot(make_config())
    second.db = db
    await second.load_settings()
    assert second.settings.get(OTHER) == GuildSettings(staff_role_id=STAFF, faq_channel_ids=frozenset({1, 2, 3}))
    first.db = second.db = None


async def test_cleared_faq_channels_mean_every_channel_even_with_env_ids(make_config, db):
    b = FAQBot(make_config(faq_channel_ids=frozenset({55})))
    b.db = db
    assert b.settings.get(h.GUILD_ID).faq_channel_ids == {55}
    await b.settings.update(h.GUILD_ID, faq_channel_ids=frozenset())
    assert b.settings.get(h.GUILD_ID).faq_channel_ids == frozenset()    # the row wins over .env
    assert (await db.all_guild_settings())[h.GUILD_ID]["faq_channel_ids"] is None
    b.db = None


async def test_staff_role_from_the_single_server_version_is_kept(make_config, db):
    await db.set_setting("staff_role_id", str(STAFF))       # what /set-staff-role stored before
    b = FAQBot(make_config())
    b.db = db
    await b.load_settings()
    assert b.staff_role_id(h.GUILD_ID) == STAFF and b.staff_role_id(OTHER) is None
    b.db = None


async def test_earlier_metrics_are_assigned_to_guild_id(make_config, db):
    await db.upsert_metrics(channel_id=1, opener_id=1, staff_id=2, created_at=h.ago(hours=2),
                            first_response_at=None, closed_at=h.ago(hours=1))       # no guild: old row
    b = FAQBot(make_config())
    b.db = db
    await b.load_settings()
    assert (await db.staff_stats(2, guild_id=h.GUILD_ID)).tickets == 1
    assert (await db.staff_stats(2, guild_id=OTHER)).tickets == 0
    b.db = None


async def test_without_a_database_settings_still_apply_until_restart(make_config):
    b = FAQBot(make_config())
    assert await b.settings.update(OTHER, staff_role_id=STAFF) is False
    assert b.staff_role_id(OTHER) == STAFF


# ---- /setup --------------------------------------------------------------------------------

async def setup(bot, guild, role=None, who=None):
    i = h.interaction(who=who or admin_in(guild), guild=guild)
    await bot.ticket_commands.run_setup(i, role or h.role(STAFF))
    return i


def by_name(guild, name):
    return next(c for c in guild.channels if c.name == name)


def overwrite_for(channel, target_id):
    return next(v for k, v in channel.overwrites.items() if k.id == target_id)


async def test_setup_provisions_a_fresh_server(bot):
    guild = h.SetupGuild(OTHER)
    i = await setup(bot, guild)
    i.response.defer.assert_awaited_once()
    tickets, archive = by_name(guild, TICKETS_CATEGORY_NAME), by_name(guild, ARCHIVE_CATEGORY_NAME)
    transcripts, sla = by_name(guild, TRANSCRIPTS_CHANNEL_NAME), by_name(guild, SLA_ALERTS_CHANNEL_NAME)
    assert isinstance(tickets, discord.CategoryChannel) and isinstance(archive, discord.CategoryChannel)
    assert transcripts.category_id == archive.id and sla.category_id == archive.id
    for private in (archive, transcripts, sla):
        assert overwrite_for(private, OTHER).view_channel is False      # @everyone denied
        assert overwrite_for(private, STAFF).view_channel is True       # staff allowed
        assert overwrite_for(private, h.ME.id).view_channel is True     # the bot too
    assert overwrite_for(transcripts, STAFF).send_messages is False    # read-only audit log
    assert bot.settings.get(OTHER) == GuildSettings(
        staff_role_id=STAFF, tickets_category_id=tickets.id, archive_category_id=archive.id,
        transcript_log_channel_id=transcripts.id, sla_alert_channel_id=sla.id)
    assert (await bot.db.all_guild_settings())[OTHER]["archive_category_id"] == archive.id
    kw = i.followup.send.call_args.kwargs
    embed = kw["embed"]
    assert kw["ephemeral"] is True and embed.color == discord.Color.blurple() and embed.title == "✅ Setup complete"
    assert all(f.value.endswith("created") for f in embed.fields) and len(embed.fields) == 4
    assert transcripts.mention in embed.fields[2].value


async def test_setup_twice_reuses_everything(bot):
    guild = h.SetupGuild(OTHER)
    await setup(bot, guild)
    first = bot.settings.get(OTHER)
    i = await setup(bot, guild)
    assert guild.create_category.await_count == 2 and guild.create_text_channel.await_count == 2
    assert bot.settings.get(OTHER) == first
    assert all(f.value.endswith("already existed") for f in i.followup.send.call_args.kwargs["embed"].fields)


async def test_setup_keeps_existing_structure(bot):
    """A renamed tickets category (found by stored ID) and a public category that already has
    the archive name are reused. Only the archive is made private: an existing tickets
    category may hold public channels, so its permissions are left alone."""
    guild = h.SetupGuild(OTHER)
    tickets = guild.add(discord.CategoryChannel, "Help Desk")
    archive = guild.add(discord.CategoryChannel, ARCHIVE_CATEGORY_NAME)
    await bot.settings.update(OTHER, tickets_category_id=tickets.id)
    await setup(bot, guild)
    guild.create_category.assert_not_awaited()
    tickets.edit.assert_not_awaited()
    assert overwrite_for(archive, OTHER).view_channel is False and overwrite_for(archive, STAFF).view_channel
    assert bot.settings.get(OTHER).tickets_category_id == tickets.id


@pytest.mark.parametrize("case, expected", [
    ("not admin", "Only administrators"),
    ("everyone role", "@everyone can't be the staff role"),
    ("no permissions", "Manage Channels"),
])
async def test_setup_refusals_change_nothing(bot, case, expected):
    guild = h.SetupGuild(OTHER, can_manage=case != "no permissions")
    who = h.member_in(guild, 3) if case == "not admin" else None
    role = h.role(OTHER, default=True) if case == "everyone role" else None
    i = await setup(bot, guild, role=role, who=who)
    assert expected in h.reply_text(i)
    assert guild.channels == [] and not bot.settings.is_configured(OTHER)


async def test_setup_that_fails_partway_can_be_rerun(bot):
    guild = h.SetupGuild(OTHER)
    guild.create_text_channel.side_effect = discord.Forbidden(MagicMock(status=403), "Missing Permissions")
    i = await setup(bot, guild)
    assert "stopped partway" in h.reply_text(i) and not bot.settings.is_configured(OTHER)
    guild.create_text_channel.side_effect = guild._create_text_channel
    await setup(bot, guild)
    assert len(guild.categories) == 2                      # the first run's categories were reused
    assert bot.settings.get(OTHER).sla_alert_channel_id is not None


async def test_setup_without_a_database_warns(make_config):
    b = FAQBot(make_config())
    i = await setup(b, h.SetupGuild(OTHER))
    assert "only until the bot restarts" in i.followup.send.call_args.kwargs["embed"].description


# ---- /set-faq-channels --------------------------------------------------------------------

def faq_guild():
    guild = h.SetupGuild(OTHER)
    help_, general = guild.add(discord.TextChannel, "help"), guild.add(discord.TextChannel, "general")
    guild.add(discord.CategoryChannel, "Stuff")
    return guild, help_, general


def faq_message(guild, channel, *, public=True):
    channel.permissions_for = lambda _role: NS(view_channel=public)
    m = MagicMock()
    m.content, m.channel, m.guild = "how do I log in?", channel, guild
    m.author = NS(bot=False, id=h.OUTSIDER_ID, mention="<@3>")
    return m


async def test_faq_channels_can_be_restricted_and_cleared(bot):
    guild, help_, general = faq_guild()
    i = h.interaction(who=admin_in(guild), guild=guild)
    await bot.ticket_commands.set_faq_channels(i, f"{help_.mention}, {general.id}")
    assert h.last_text(i.response.send_message) == f"✅ FAQ answers are now limited to {help_.mention}, {general.mention}."
    assert bot.settings.get(OTHER).faq_channel_ids == {help_.id, general.id}
    elsewhere = guild.add(discord.TextChannel, "random")
    assert bot.faq._should_listen(faq_message(guild, help_, public=False))   # listed: even if private
    assert not bot.faq._should_listen(faq_message(guild, elsewhere))

    i = h.interaction(who=admin_in(guild), guild=guild)
    await bot.ticket_commands.set_faq_channels(i, None)
    assert "every channel" in h.last_text(i.response.send_message)
    assert bot.faq._should_listen(faq_message(guild, elsewhere))
    assert not bot.faq._should_listen(faq_message(guild, elsewhere, public=False))


@pytest.mark.parametrize("text", ["#nope", "12345", "<#999>", "help"])
async def test_faq_channels_reject_what_isnt_a_text_channel_here(bot, text):
    guild, *_ = faq_guild()
    guild.add(discord.CategoryChannel, "cat")
    i = h.interaction(who=admin_in(guild), guild=guild)
    await bot.ticket_commands.set_faq_channels(i, text)
    assert "aren't text channels in this server" in h.reply_text(i)
    assert not bot.settings.is_configured(OTHER)


async def test_faq_channels_are_admin_only(bot):
    guild, help_, _ = faq_guild()
    i = h.interaction(who=h.member_in(guild, 3), guild=guild)
    await bot.ticket_commands.set_faq_channels(i, help_.mention)
    assert "Only administrators" in h.reply_text(i)


# ---- on-the-fly fallbacks ----------------------------------------------------------------------

async def open_ticket(bot, guild, uid=40):
    user = h.member_in(guild, uid, name=f"user{uid}")
    i = h.interaction(who=user, guild=guild)
    await bot.tickets.open_ticket(i)
    return i


async def test_opening_a_ticket_in_an_unconfigured_server_creates_the_category(bot):
    guild = h.SetupGuild(OTHER)
    i = await open_ticket(bot, guild)
    category = by_name(guild, TICKETS_CATEGORY_NAME)
    assert bot.settings.get(OTHER).tickets_category_id == category.id              # saved for next time
    ticket = by_name(guild, "ticket-user40")
    assert ticket.category_id == category.id and "created" in h.last_text(i.followup.send)
    # No staff role yet: nobody but the owner, the bot (and admins, who bypass overwrites) can see it.
    assert {k.id for k in guild.create_text_channel.call_args.kwargs["overwrites"]} == {OTHER, 40, h.ME.id}
    welcome = ticket.send.call_args.kwargs
    assert "our support team" in welcome["embed"].description and welcome["content"] == "<@40>"


async def test_a_deleted_tickets_category_is_recreated(bot):
    guild = h.SetupGuild(OTHER)
    await open_ticket(bot, guild, 40)
    old = by_name(guild, TICKETS_CATEGORY_NAME)
    guild.remove(old)
    await open_ticket(bot, guild, 41)
    new = bot.tickets_category(guild)
    assert new is not None and new.id != old.id and by_name(guild, "ticket-user41").category_id == new.id


async def test_concurrent_first_tickets_create_one_category(bot):
    guild = h.SetupGuild(OTHER)
    create = guild._create_category

    async def slow_create(*a, **kw):
        await asyncio.sleep(0.01)
        return await create(*a, **kw)
    guild.create_category.side_effect = slow_create
    await asyncio.gather(open_ticket(bot, guild, 40), open_ticket(bot, guild, 41))
    assert guild.create_category.await_count == 1 and len(guild.text_channels) == 2


async def test_opening_without_permission_explains_instead_of_crashing(bot):
    guild = h.SetupGuild(OTHER)
    guild.create_category.side_effect = discord.Forbidden(MagicMock(status=403), "Missing Permissions")
    i = await open_ticket(bot, guild)
    assert "Manage Channels" in h.last_text(i.followup.send) and guild.text_channels == []


async def test_staff_role_from_setup_is_used_for_new_tickets(bot):
    guild = h.SetupGuild(OTHER, roles=[h.role(STAFF)])
    await setup(bot, guild)
    await open_ticket(bot, guild)
    assert STAFF in {k.id for k in guild.create_text_channel.call_args.kwargs["overwrites"]}
    assert "<@&77>" in by_name(guild, "ticket-user40").send.call_args.kwargs["content"]


async def test_archive_without_an_archive_category_stays_in_place(bot):
    guild = h.SetupGuild(OTHER)
    await open_ticket(bot, guild)
    ticket = by_name(guild, "ticket-user40")
    ticket.msgs = []
    bot.transcripts.dm_owner = AsyncMock()
    bot.transcripts.gather = AsyncMock(side_effect=RuntimeError("not under test"))
    staff = h.member_in(guild, 7, admin=True)
    i = h.interaction(ticket, staff)
    i.guild = guild
    await bot.tickets.archive_ticket(i, "done")
    assert "category" not in ticket.edit.call_args.kwargs and bot.tickets.delete_at(ticket) is not None
