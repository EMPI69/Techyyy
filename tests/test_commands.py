"""Slash commands: /staff-stats and /staff-leaderboard (volume and response speed only),
/set-staff-role and the dynamic staff checks, /admin-help, and the /ticket-purge admin tool."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import helpers as h
from caudal_bot.cogs.commands import ADMIN_HELP
from caudal_bot.main import FAQBot
from caudal_bot.views.ticket_lifecycle import PurgeConfirmView


# ---- /staff-stats and /staff-leaderboard ----------------------------------------------------

async def handled(db, channel_id, staff_id, response_min, guild_id=h.GUILD_ID):
    opened = h.ago(hours=3)
    await db.upsert_metrics(
        channel_id=channel_id, opener_id=h.OWNER_ID, staff_id=staff_id, created_at=opened,
        first_response_at=None if response_min is None else opened + dt.timedelta(minutes=response_min),
        closed_at=h.ago(hours=1), guild_id=guild_id)


def sent_embed(i) -> discord.Embed:
    return i.response.send_message.call_args.kwargs["embed"]


def mentions_a_rating(embed: discord.Embed) -> bool:
    text = str(embed.to_dict())
    return any(word in text for word in ("⭐", "Satisfaction", "CSAT", "/ 5"))


async def test_staff_stats_shows_tickets_and_response_speed_only(db_bot):
    await handled(db_bot.db, 1, h.STAFF_ID, 4)
    await handled(db_bot.db, 2, h.STAFF_ID, 8)
    await handled(db_bot.db, 3, 50, 1)                   # someone else's ticket
    i = h.interaction(who=h.member(h.STAFF_ID, name="mod"))
    await db_bot.ticket_commands.show_staff_stats(i, None)
    embed = sent_embed(i)
    fields = {f.name: f.value for f in embed.fields}
    assert list(fields) == ["🎫 Tickets Handled", "⚡ Avg First Response"]
    assert fields["🎫 Tickets Handled"].startswith("**2** all time") and fields["⚡ Avg First Response"] == "6m 0s"
    assert not mentions_a_rating(embed) and i.response.send_message.call_args.kwargs["ephemeral"]


async def test_staff_stats_for_someone_with_no_tickets(db_bot):
    i = h.interaction(who=h.member(h.STAFF_ID, name="mod"))
    await db_bot.ticket_commands.show_staff_stats(i, h.member(77, name="new"))
    fields = {f.name: f.value for f in sent_embed(i).fields}
    assert fields["🎫 Tickets Handled"].startswith("**0** all time") and fields["⚡ Avg First Response"] == "—"


async def test_leaderboard_ranks_by_volume_then_response_speed(db_bot):
    for cid, (staff, response) in enumerate([(h.STAFF_ID, 10), (h.STAFF_ID, 10), (50, 3), (50, 3), (51, 1)]):
        await handled(db_bot.db, cid, staff, response)
    i = h.interaction(who=h.member(h.STAFF_ID, name="mod"))
    await db_bot.ticket_commands.show_leaderboard(i, "All Time")
    embed = sent_embed(i)
    lines = embed.description.splitlines()
    # 50 and STAFF tie on volume; 50 answers faster. 51 is fastest but handled fewer.
    assert [line.split("<@")[1].split(">")[0] for line in lines[::2]] == ["50", str(h.STAFF_ID), "51"]
    assert lines[1] == "-# ⚡ 3m 0s avg first response"
    assert "response speed" in embed.footer.text and not mentions_a_rating(embed)
    assert i.response.send_message.call_args.kwargs["allowed_mentions"].users is False


async def test_analytics_only_count_this_servers_tickets(db_bot):
    await handled(db_bot.db, 1, h.STAFF_ID, 4)
    await handled(db_bot.db, 2, h.STAFF_ID, 8, guild_id=500)       # same person, another server
    await handled(db_bot.db, 3, 60, 2, guild_id=500)
    here = NS(id=h.GUILD_ID)
    i = h.interaction(who=h.member(h.STAFF_ID, name="mod"), guild=here)
    await db_bot.ticket_commands.show_staff_stats(i, None)
    assert {f.name: f.value for f in sent_embed(i).fields}["🎫 Tickets Handled"].startswith("**1** all time")
    i = h.interaction(who=h.member(h.STAFF_ID, name="mod"), guild=here)
    await db_bot.ticket_commands.show_leaderboard(i, "All Time")
    assert "<@60>" not in sent_embed(i).description                  # the other server's staff stay private


async def test_analytics_are_staff_only(db_bot):
    i = h.interaction(who=h.member(h.OUTSIDER_ID, name="eve"))
    await db_bot.ticket_commands.show_leaderboard(i, "All Time")
    assert "Only staff" in h.reply_text(i)


# ---- /set-staff-role -------------------------------------------------------------------------

NEW_ROLE = 77
role = h.role
HOME = NS(id=h.GUILD_ID, name="Caudal", get_role=lambda rid: NS(id=rid, name="Support"))


def member_with(uid, *role_ids, admin=False, guild=HOME):
    return h.member_in(guild, uid, *role_ids, admin=admin)


@pytest.fixture
async def real_bot(make_config, db):
    """A bot with the real is_staff (the `bot` fixture fakes it) and a database."""
    b = FAQBot(make_config())
    b.db = db
    yield b
    b.db = None


async def test_set_staff_role_saves_and_applies_everywhere(real_bot):
    admin = member_with(7, admin=True)
    old_staff, new_staff = member_with(20, h.STAFF_ROLE_ID), member_with(21, NEW_ROLE)
    assert real_bot.is_staff(old_staff) and not real_bot.is_staff(new_staff)   # .env role until changed
    i = h.interaction(who=admin, guild=HOME)
    await real_bot.ticket_commands.set_staff_role(i, role(NEW_ROLE))
    assert h.last_text(i.response.send_message) == f"✅ Staff role has been updated to <@&{NEW_ROLE}>."
    assert i.response.send_message.call_args.kwargs["ephemeral"] is True
    assert real_bot.staff_role_id(h.GUILD_ID) == NEW_ROLE
    assert (await real_bot.db.all_guild_settings())[h.GUILD_ID]["staff_role_id"] == NEW_ROLE
    assert real_bot.is_staff(new_staff) and not real_bot.is_staff(old_staff)
    assert real_bot.is_staff(member_with(8, admin=True))                        # admins always count
    assert real_bot.staff_role(HOME).id == NEW_ROLE                             # welcome cards + SLA pings


async def test_staff_role_is_per_server(real_bot):
    other = NS(id=500, name="Other", get_role=lambda rid: NS(id=rid))
    i = h.interaction(who=member_with(7, admin=True, guild=other), guild=other)
    await real_bot.ticket_commands.set_staff_role(i, role(NEW_ROLE))
    assert real_bot.staff_role_id(500) == NEW_ROLE
    assert real_bot.staff_role_id(h.GUILD_ID) == h.STAFF_ROLE_ID                 # untouched
    assert real_bot.is_staff(member_with(21, NEW_ROLE, guild=other))
    assert not real_bot.is_staff(member_with(21, NEW_ROLE))                     # same role ID, other server


async def test_saved_staff_role_is_loaded_on_start(make_config, db):
    await db.save_guild_settings(h.GUILD_ID, {"staff_role_id": NEW_ROLE})
    b = FAQBot(make_config())
    assert b.staff_role_id(h.GUILD_ID) == h.STAFF_ROLE_ID    # nothing loaded yet: .env value
    b.db = db
    await b.load_settings()
    assert b.staff_role_id(h.GUILD_ID) == NEW_ROLE
    b.db = None


async def test_without_a_saved_role_the_env_value_applies(real_bot):
    await real_bot.load_settings()
    assert real_bot.staff_role_id(h.GUILD_ID) == h.STAFF_ROLE_ID
    assert real_bot.staff_role_id(500) is None                   # .env only ever describes GUILD_ID


@pytest.mark.parametrize("who, target, expected", [
    ("staff", role(NEW_ROLE), "Only administrators"),
    ("admin", role(h.GUILD_ID, default=True), "@everyone can't be the staff role"),
    ("admin", role(NEW_ROLE, managed=True), "managed by an integration"),
])
async def test_set_staff_role_refusals(real_bot, who, target, expected):
    user = member_with(20, h.STAFF_ROLE_ID) if who == "staff" else member_with(7, admin=True)
    i = h.interaction(who=user, guild=HOME)
    await real_bot.ticket_commands.set_staff_role(i, target)
    assert expected in h.reply_text(i)
    assert real_bot.staff_role_id(h.GUILD_ID) == h.STAFF_ROLE_ID and await real_bot.db.all_guild_settings() == {}


async def test_set_staff_role_without_a_database_lasts_until_restart(make_config):
    b = FAQBot(make_config())
    i = h.interaction(who=member_with(7, admin=True), guild=HOME)
    await b.ticket_commands.set_staff_role(i, role(NEW_ROLE))
    assert "only until the bot restarts" in h.reply_text(i) and b.staff_role_id(h.GUILD_ID) == NEW_ROLE


# ---- /admin-help ------------------------------------------------------------------------------

@pytest.mark.parametrize("user", [member_with(20, h.STAFF_ROLE_ID), member_with(7, admin=True)],
                         ids=["staff role", "administrator"])
async def test_admin_help_lists_every_staff_command(real_bot, user):
    i = h.interaction(who=user, guild=HOME)
    await real_bot.ticket_commands.show_admin_help(i)
    kw = i.response.send_message.call_args.kwargs
    embed = kw["embed"]
    assert kw["ephemeral"] is True and embed.color == discord.Color.blurple()
    assert [f.name for f in embed.fields] == [
        "/setup [staff_role]", "/close [reason]", "/ticket-purge [category_type]", "/staff-stats [member]",
        "/staff-leaderboard [timeframe]", "/set-staff-role [role]", "/set-faq-channels [channels]"]
    assert [f.value for f in embed.fields] == [v for _, v in ADMIN_HELP]
    assert embed.footer.text == "Active staff role: @Support"


async def test_admin_help_documents_real_commands_only(real_bot):
    commands = {c.name: c for c in real_bot.ticket_commands.get_app_commands()}
    for name, _ in ADMIN_HELP:
        cmd, *params = name.lstrip("/").replace("[", "").replace("]", "").split()
        assert [p.name for p in commands[cmd].parameters] == params, name


async def test_admin_help_is_refused_to_everyone_else(real_bot):
    i = h.interaction(who=member_with(3), guild=HOME)
    await real_bot.ticket_commands.show_admin_help(i)
    assert "Only staff" in h.reply_text(i)


# ---- /ticket-purge ---------------------------------------------------------------------------


@pytest.fixture
def purge_world(bot):
    tickets_cat, archive_cat = h.category(h.TICKETS_CAT), h.category(h.ARCHIVE_CAT)
    guild = h.Guild({h.TICKETS_CAT: tickets_cat, h.ARCHIVE_CAT: archive_cat})
    mk = lambda name, topic, cat: h.text_channel(name=name, topic=topic, category_id=cat.id, guild=guild)
    chans = NS(
        open=mk("ticket-bob", "ticket-owner:1", tickets_cat),
        archived_in_place=mk("closed-amy", "ticket-owner:3 | delete_at:99", tickets_cat),
        archived=mk("closed-cat", "ticket-owner:4 | delete_at:99", archive_cat),
        lookalike=mk("ticket-rules", "Read the rules!", tickets_cat),    # right name, not a ticket
        renamed=mk("support-dan", "ticket-owner:5", tickets_cat),         # a ticket, wrong name pattern
    )
    tickets_cat.text_channels = [chans.open, chans.archived_in_place, chans.lookalike, chans.renamed]
    archive_cat.text_channels = [chans.archived]
    bot.get_channel = guild.get_channel
    bot.config = type(bot.config)(**{**bot.config.__dict__, "archive_category_id": h.ARCHIVE_CAT})
    return NS(bot=bot, guild=guild, c=chans, admin=h.member(7, admin=True), mod=h.member(8, admin=False))


@pytest.mark.parametrize("kind, expected", [
    ("Active", ["open"]), ("Archived", ["archived_in_place", "archived"]),
    ("Both", ["open", "archived_in_place", "archived"])])
def test_purge_targets_match_state_and_naming(purge_world, kind, expected):
    w = purge_world
    got = {ch.id for ch in w.bot.tickets._purge_targets(w.guild, kind)}
    assert got == {getattr(w.c, name).id for name in expected}


async def test_purge_is_admin_only(purge_world):
    i = h.interaction(who=purge_world.mod, guild=purge_world.guild)
    await purge_world.bot.ticket_commands.start_purge(i, "Both")
    assert "Only administrators" in h.last_text(i.response.send_message)


async def test_purge_preview_needs_confirmation(purge_world):
    i = h.interaction(who=purge_world.admin, guild=purge_world.guild)
    await purge_world.bot.ticket_commands.start_purge(i, "Both")
    kw = i.response.send_message.call_args.kwargs
    assert kw["ephemeral"] and isinstance(kw["view"], PurgeConfirmView)
    assert "**3**" in kw["embed"].description and "will be lost" in kw["embed"].description  # no log channel
    assert kw["view"].children[0].label == "⚠️ Confirm Purge"
    purge_world.c.open.delete.assert_not_awaited()


async def test_confirmed_purge_deletes_and_reports(purge_world):
    w = purge_world
    lg = h.log_channel()
    w.bot.transcript_channel = lambda _guild: lg
    w.c.archived_in_place.delete = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=403), "no"))
    lock = w.bot.tickets._lifecycle_lock(w.c.archived.id)
    await lock.acquire()
    try:
        i = h.interaction(who=w.admin, guild=w.guild)
        await w.bot.tickets.run_purge(i, "Both")
    finally:
        lock.release()
    w.c.open.delete.assert_awaited_once()
    assert "Emergency ticket purge by" in w.c.open.delete.call_args.kwargs["reason"]
    for untouched in (w.c.archived, w.c.lookalike, w.c.renamed):
        untouched.delete.assert_not_awaited()
    report = i.followup.send.call_args
    assert report.kwargs["ephemeral"] and "deleted **1** ticket channel(s)" in report[0][0]
    assert "1 could not be deleted" in report[0][0] and "1 skipped" in report[0][0]
    assert len(lg.posted) == 2            # transcripts logged before each delete attempt
    assert not w.bot.tickets._purge_running and not w.bot.tickets._closing_channels


async def test_purge_rechecks_admin_on_confirm_and_blocks_concurrent_runs(purge_world):
    w = purge_world
    i = h.interaction(who=w.mod, guild=w.guild)
    await w.bot.tickets.run_purge(i, "Both")
    assert "Only administrators" in i.response.edit_message.call_args.kwargs["content"]
    w.bot.tickets._purge_running = True
    i = h.interaction(who=w.admin, guild=w.guild)
    await w.bot.ticket_commands.start_purge(i, "Both")
    assert "already running" in h.last_text(i.response.send_message)
    w.c.open.delete.assert_not_awaited()
