"""Ticket lifecycle: topic format, permissions, archive / re-open / delete, claiming,
the 48-hour cleanup loop and SLA alerts. Closing and re-opening never rename a channel
or edit its topic (Discord allows only 2 of those per channel every 10 minutes)."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import helpers as h
from caudal_bot.cogs import tickets as tickets_module
from caudal_bot.common import (
    ARCHIVE_RETENTION_SECONDS, STATUS_AWAITING, STATUS_IN_PROGRESS, archived_status, get_ticket_status,
    restored_status, swap_name_prefix, ticket_channel_name, ticket_delete_at, ticket_owner_id, ticket_topic,
)
from caudal_bot.main import FAQBot
from caudal_bot.views.ticket_lifecycle import (
    CloseReasonModal, ClosedTicketView, ConfirmCloseView, TicketControlView,
)

CLAIMED = f"{STATUS_IN_PROGRESS} (Claimed by <@{h.STAFF_ID}>)"


# ---- topic serialisation --------------------------------------------------------

def chan(topic):
    c = MagicMock(spec=discord.TextChannel)
    c.topic = topic
    return c


@pytest.mark.parametrize("owner, delete_at", [(1, None), (852112652611878922, None), (5, 1790000000), (5, 0)])
def test_topic_round_trip(owner, delete_at):
    topic = ticket_topic(owner, delete_at)
    assert topic == (f"ticket-owner:{owner}" if delete_at is None else f"ticket-owner:{owner} | delete_at:{delete_at}")
    assert ticket_owner_id(chan(topic)) == owner
    assert ticket_delete_at(chan(topic)) == delete_at


@pytest.mark.parametrize("topic", [None, "", "ticket-owner:", "ticket-owner:abc", "support ticket",
                                   "Ticket-Owner:5", " ticket-owner:5"])
def test_malformed_topics_are_not_tickets(topic):
    assert ticket_owner_id(chan(topic)) is None and ticket_delete_at(chan(topic)) is None


def test_delete_at_requires_a_ticket_topic():
    assert ticket_delete_at(chan("rules | delete_at:5")) is None


def test_only_text_channels_are_tickets():
    assert ticket_owner_id(NS(topic="ticket-owner:5")) is None and ticket_owner_id(None) is None


@pytest.mark.parametrize("name, old, new, expected", [
    ("ticket-bob", "ticket-", "closed-", "closed-bob"),
    ("closed-bob", "closed-", "ticket-", "ticket-bob"),
    ("support-bob", "ticket-", "closed-", "support-bob"),   # renamed by hand: left alone
    ("ticket-" + "x" * 99, "ticket-", "closed-", ("closed-" + "x" * 99)[:100]),
])
def test_swap_name_prefix(name, old, new, expected):
    assert swap_name_prefix(name, old, new) == expected


@pytest.mark.parametrize("username, expected", [
    ("John.Doe Smith", "ticket-john-doe-smith"),
    ("ÆØÅ!!!", "ticket-42"),          # nothing usable left: falls back to the user ID
    ("a" * 200, "ticket-" + "a" * 93),  # Discord's 100-character channel name cap
])
def test_ticket_channel_name(username, expected):
    assert ticket_channel_name(NS(name=username, id=42)) == expected


def test_status_round_trip_keeps_the_claim():
    assert restored_status(archived_status(CLAIMED)) == CLAIMED
    assert restored_status(archived_status(STATUS_AWAITING)) == STATUS_AWAITING
    assert archived_status(archived_status(CLAIMED)) == archived_status(CLAIMED)  # no nesting


# ---- world fixture ----------------------------------------------------------------

@pytest.fixture
def world(make_config, monkeypatch):
    monkeypatch.setattr(FAQBot, "user", property(lambda self: h.ME))
    b = FAQBot(make_config(archive_category_id=h.ARCHIVE_CAT))
    b.is_staff = lambda u: getattr(u, "id", None) == h.STAFF_ID
    users = {u.id: u for u in (h.OWNER, h.STAFF, h.OUTSIDER)}
    b.get_user = lambda uid: users.get(uid)
    tickets_cat, archive_cat = h.category(h.TICKETS_CAT, size=3), h.category(h.ARCHIVE_CAT)
    guild = h.Guild({h.TICKETS_CAT: tickets_cat, h.ARCHIVE_CAT: archive_cat})
    b.get_channel = guild.get_channel
    b.get_guild = lambda _gid: guild
    b.managed_guilds = lambda: [guild]
    b.transcript_channel = lambda _guild: None
    b.transcripts.dm_owner = AsyncMock()  # DM content is covered in test_transcripts
    monkeypatch.setattr(tickets_module, "CLOSE_COUNTDOWN_SECONDS", 0)
    monkeypatch.setattr(tickets_module, "CHANNEL_EDIT_TIMEOUT_SECONDS", 0.05)
    return NS(bot=b, t=b.tickets, guild=guild, tickets=tickets_cat, archive=archive_cat)


def ticket(world, status=STATUS_AWAITING, **kw):
    return h.text_channel(guild=world.guild, messages=[h.welcome(status)], **kw)


def archived(world, **kw):
    defaults = dict(topic=f"ticket-owner:{h.OWNER_ID} | delete_at:9999999999", name="closed-bob",
                    category_id=h.ARCHIVE_CAT)
    return ticket(world, **(defaults | kw))


def owner_overwrite(edit_call):
    return next(v for k, v in edit_call.kwargs["overwrites"].items() if k.id == h.OWNER_ID)


def closed(world, delete_at=9999999999, **kw):
    """A ticket archived by this version: plain name and topic, deadline in the database."""
    ch = ticket(world, category_id=h.ARCHIVE_CAT, **kw)
    world.t._deadlines[ch.id] = delete_at
    return ch


def assert_no_rename_or_retopic(ch):
    for call in ch.edit.call_args_list:
        assert "name" not in call.kwargs and "topic" not in call.kwargs, call.kwargs


# ---- permissions ----------------------------------------------------------------

def test_owner_overwrite_changes_only_send_permission(world):
    ch = ticket(world)
    staff_role = discord.Object(h.STAFF_ROLE_ID, type=discord.Role)
    ch.overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, manage_messages=True)
    result = world.t._owner_overwrites(ch, h.OWNER_ID, can_send=False)
    owner = next(v for k, v in result.items() if k.id == h.OWNER_ID)
    assert (owner.view_channel, owner.read_message_history, owner.send_messages) == (True, True, False)
    assert result[staff_role].manage_messages is True and len(result) == 2  # nothing duplicated or dropped


def test_owner_without_overwrite_is_targeted_by_id(world):
    ch = ticket(world)
    ch.overwrites = {}
    result = world.t._owner_overwrites(ch, h.OWNER_ID, can_send=True)
    (key, value), = result.items()
    assert isinstance(key, discord.Object) and key.id == h.OWNER_ID and key.type is discord.Member
    assert value.send_messages is True


# ---- close routing -------------------------------------------------------------------

async def test_staff_get_the_reason_modal(world):
    i = h.interaction(ticket(world), h.STAFF)
    await world.t.close_ticket(i)
    assert isinstance(i.response.send_modal.call_args[0][0], CloseReasonModal)


async def test_staff_close_with_a_reason_skips_the_modal(world):
    ch = ticket(world)
    i = h.interaction(ch, h.STAFF)
    await world.t.close_ticket(i, "  Duplicate of #12  ")
    i.response.send_modal.assert_not_awaited()
    assert world.t.delete_at(ch) is not None
    assert "Duplicate of #12" in ch.msgs[-1].embeds[0].fields[1].value      # on the archive card


async def test_owner_gets_a_confirmation(world):
    i = h.interaction(ticket(world), h.OWNER)
    await world.t.close_ticket(i)
    kw = i.response.send_message.call_args.kwargs
    assert isinstance(kw["view"], ConfirmCloseView) and kw["ephemeral"] is True


async def test_outsiders_cannot_close(world):
    i = h.interaction(ticket(world), h.OUTSIDER)
    await world.t.close_ticket(i)
    assert "Only staff or the ticket creator" in h.last_text(i.response.send_message)


async def test_close_outside_a_ticket_is_refused(world):
    i = h.interaction(ticket(world, topic="general chat"), h.STAFF)
    await world.t.close_ticket(i)
    assert "only be used inside a ticket" in h.last_text(i.response.send_message)


# ---- archive (stage 1) -------------------------------------------------------------

async def test_archive_moves_and_locks_without_renaming(world):
    ch = ticket(world, status=CLAIMED)
    before = time.time()
    await world.t.archive_ticket(h.interaction(ch, h.STAFF), "Fixed")

    # One request: permissions + move. No name or topic, so Discord's 2-per-10-minutes
    # name/topic limit can never lock a ticket.
    ch.edit.assert_awaited_once()
    kw = ch.edit.call_args.kwargs
    assert set(kw) == {"overwrites", "category", "reason"} and kw["category"] is world.archive
    assert owner_overwrite(ch.edit.call_args).send_messages is False
    assert owner_overwrite(ch.edit.call_args).view_channel is True
    delete_at = world.t.delete_at(ch)
    assert before + ARCHIVE_RETENTION_SECONDS - 1 <= delete_at <= time.time() + ARCHIVE_RETENTION_SECONDS + 1
    assert "sync_permissions" not in kw  # the archive category's permissions must not replace the lock


async def test_archive_posts_the_card_and_updates_status(world):
    ch = ticket(world, status=CLAIMED)
    i = h.interaction(ch, h.STAFF)
    await world.t.archive_ticket(i, "Fixed")
    card = next(m for m in ch.msgs if m.embeds and m.embeds[0].title == "🔒 Ticket Closed & Archived")
    assert isinstance(card.kwargs["view"], ClosedTicketView)
    assert [c.custom_id for c in card.kwargs["view"].children] == ["ticket:reopen", "ticket:delete_now"]
    assert get_ticket_status(ch.msgs[0]) == archived_status(CLAIMED)
    # the cached channel reflects the edit immediately, before Discord's gateway event
    assert ch.name == "ticket-bob" and ch.topic == f"ticket-owner:{h.OWNER_ID}" and ch.category_id == h.ARCHIVE_CAT
    assert world.t.delete_at(ch) is not None
    assert "Ticket closed" in h.last_text(i.followup.send)
    world.bot.transcripts.dm_owner.assert_awaited_once()


async def test_archive_in_place_when_archive_category_is_full(world):
    world.archive.channels = [object()] * 50
    ch = ticket(world)
    await world.t.archive_ticket(h.interaction(ch, h.STAFF), "x")
    assert "category" not in ch.edit.call_args.kwargs and ch.category_id == h.TICKETS_CAT


async def test_closing_twice_is_refused(world):
    ch = ticket(world)
    await world.t.archive_ticket(h.interaction(ch, h.STAFF), "x")
    i = h.interaction(ch, h.STAFF)
    await world.t.close_ticket(i)
    assert "already closed" in h.last_text(i.response.send_message)
    ch.edit.assert_awaited_once()


async def test_a_stalled_edit_fails_cleanly(world):
    ch = ticket(world)
    async def stalled(**_kw):
        await asyncio.sleep(10)   # e.g. discord.py waiting out a rate limit
    ch.edit = AsyncMock(side_effect=stalled)
    i = h.interaction(ch, h.STAFF)
    await world.t.archive_ticket(i, "x")
    assert "taking too long" in h.last_text(i.followup.send)
    assert world.t.delete_at(ch) is None and ch.category_id == h.TICKETS_CAT     # nothing half-applied
    assert len(ch.msgs) == 1                                                     # no archive card
    assert not world.t._lifecycle_lock(ch.id).locked()


async def test_missing_permissions_fail_cleanly(world):
    ch = ticket(world)
    ch.edit = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "no"))
    i = h.interaction(ch, h.STAFF)
    await world.t.archive_ticket(i, "x")
    assert "don't have permission" in h.last_text(i.followup.send)


# ---- re-open ---------------------------------------------------------------------------

async def test_reopen_restores_everything(world):
    ch = ticket(world, status=CLAIMED)
    await world.t.archive_ticket(h.interaction(ch, h.STAFF), "x")
    ch.overwrites = ch.edit.call_args.kwargs["overwrites"]
    card = ch.msgs[-1]
    i = h.interaction(ch, h.OWNER, message=card)
    await world.t.reopen_ticket(i)
    assert ch.edit.await_count == 2
    kw = ch.edit.call_args.kwargs
    assert set(kw) == {"overwrites", "category", "reason"}   # no rename, no topic edit
    assert kw["category"] is world.tickets and owner_overwrite(ch.edit.call_args).send_messages is True
    assert world.t.delete_at(ch) is None and ch.category_id == h.TICKETS_CAT
    card.delete.assert_awaited_once()
    assert get_ticket_status(ch.msgs[0]) == CLAIMED
    assert "re-opened by <@1>" in ch.msgs[-1].embeds[0].description


async def test_repeated_close_and_reopen_never_rename(world):
    """The old version renamed on every close and re-open, so the second cycle within
    10 minutes hit Discord's limit. Now any number of cycles is fine."""
    ch = ticket(world)
    for _ in range(3):
        await world.t.archive_ticket(h.interaction(ch, h.STAFF), "x")
        ch.overwrites = ch.edit.call_args.kwargs["overwrites"]
        await world.t.reopen_ticket(h.interaction(ch, h.STAFF, message=ch.msgs[-1]))
        ch.overwrites = ch.edit.call_args.kwargs["overwrites"]
    assert ch.edit.await_count == 6
    assert_no_rename_or_retopic(ch)
    assert ch.name == "ticket-bob" and world.t.delete_at(ch) is None


async def test_ticket_closed_by_an_older_version_is_restored_once(world):
    ch = archived(world)   # "closed-bob", deadline in the topic
    await world.t.reopen_ticket(h.interaction(ch, h.STAFF, message=ch.msgs[-1]))
    kw = ch.edit.call_args.kwargs
    assert kw["name"] == "ticket-bob" and kw["topic"] == f"ticket-owner:{h.OWNER_ID}"   # one-time clean-up
    assert world.t.delete_at(ch) is None
    ch.overwrites = kw["overwrites"]
    ch.edit.reset_mock()
    await world.t.archive_ticket(h.interaction(ch, h.STAFF), "again")
    assert_no_rename_or_retopic(ch)


async def test_deadlines_are_stored_in_the_database(world, db):
    world.bot.db = db
    try:
        ch = ticket(world)
        await world.t.archive_ticket(h.interaction(ch, h.STAFF), "x")
        assert await db.archive_deadlines() == {ch.id: world.t.delete_at(ch)}
        # A restart reloads them before the loops start.
        world.t._deadlines.clear()
        world.t._cleanup_expired.start = world.t._sla_check.start = lambda: None
        await world.t.cog_load()
        assert world.t.delete_at(ch) is not None
        ch.overwrites = ch.edit.call_args.kwargs["overwrites"]
        await world.t.reopen_ticket(h.interaction(ch, h.STAFF, message=ch.msgs[-1]))
        assert await db.archive_deadlines() == {}
    finally:
        world.bot.db = None


async def test_archive_without_a_database_still_works_in_memory(world):
    ch = ticket(world)
    await world.t.archive_ticket(h.interaction(ch, h.STAFF), "x")
    assert world.t.delete_at(ch) is not None


async def test_reopen_refused_when_tickets_category_is_full(world):
    world.tickets.channels = [object()] * 50
    ch = archived(world)
    i = h.interaction(ch, h.STAFF, message=MagicMock())
    await world.t.reopen_ticket(i)
    assert "full" in h.last_text(i.followup.send)
    ch.edit.assert_not_awaited()


@pytest.mark.parametrize("who, channel_kind, expected", [
    ("outsider", "archived", "Only staff or the ticket creator"),
    ("owner", "open", "already open"),
])
async def test_reopen_refusals(world, who, channel_kind, expected):
    ch = archived(world) if channel_kind == "archived" else ticket(world)
    i = h.interaction(ch, h.OUTSIDER if who == "outsider" else h.OWNER, message=MagicMock())
    await world.t.reopen_ticket(i)
    assert expected in h.reply_text(i)


# ---- claiming --------------------------------------------------------------------------------

async def test_claim_updates_card_disables_button_and_pings_owner(world):
    ch = ticket(world)
    card = ch.msgs[0]
    i = h.interaction(ch, h.STAFF, message=card)
    await world.t.claim_ticket(i)
    assert get_ticket_status(card) == CLAIMED
    view = card.edit.call_args.kwargs["view"]
    assert view.claim.disabled and view.claim.label == "Claimed by Mod"
    notice = ch.msgs[-1]
    assert notice.content.startswith("<@1> 👋 <@2> has claimed this ticket")
    assert [u.id for u in notice.kwargs["allowed_mentions"].users] == [h.OWNER_ID]  # owner pinged, not staff
    assert "claimed this ticket" in h.last_text(i.followup.send)


async def test_claim_refusals(world):
    ch = ticket(world, status=CLAIMED)
    i = h.interaction(ch, h.OUTSIDER, message=ch.msgs[0])
    await world.t.claim_ticket(i)
    assert "Only staff" in h.last_text(i.response.send_message)
    i = h.interaction(ch, h.STAFF, message=ch.msgs[0])
    await world.t.claim_ticket(i)
    assert "already" in h.last_text(i.followup.send)
    arch = archived(world)
    i = h.interaction(arch, h.STAFF, message=arch.msgs[0])
    await world.t.claim_ticket(i)
    assert "Re-open it before claiming" in h.last_text(i.followup.send)


async def test_first_staff_reply_moves_status_to_in_progress(world):
    ch = ticket(world)
    await world.t._on_ticket_message(NS(channel=ch, author=h.OWNER))   # owner: no change
    assert get_ticket_status(ch.msgs[0]) == STATUS_AWAITING
    await world.t._on_ticket_message(NS(channel=ch, author=h.STAFF))
    assert get_ticket_status(ch.msgs[0]) == STATUS_IN_PROGRESS
    assert ch.id in world.t._status_settled  # later messages skip the lookup


# ---- delete now --------------------------------------------------------------------------------

async def test_delete_now_is_staff_only_and_requires_archive(world):
    i = h.interaction(archived(world), h.OWNER)
    await world.t.delete_ticket_now(i)
    assert "Only staff" in h.last_text(i.response.send_message)
    i = h.interaction(ticket(world), h.STAFF)
    await world.t.delete_ticket_now(i)
    assert "re-opened" in h.last_text(i.response.send_message)


async def test_delete_now_refuses_while_the_ticket_is_busy(world):
    ch = archived(world)
    lock = world.t._lifecycle_lock(ch.id)
    await lock.acquire()
    try:
        i = h.interaction(ch, h.STAFF)
        await world.t.delete_ticket_now(i)
        assert "being updated" in h.last_text(i.response.send_message)
        ch.delete.assert_not_awaited()
    finally:
        lock.release()


async def test_delete_now_counts_down_then_deletes(world):
    ch = archived(world)
    i = h.interaction(ch, h.STAFF)
    await world.t.delete_ticket_now(i)
    assert i.response.send_message.call_args.kwargs["embed"].title == "🗑️ Deleting Ticket"
    ch.delete.assert_awaited_once()
    assert "Ticket deleted by" in ch.delete.call_args.kwargs["reason"]
    assert ch.id not in world.t._closing_channels


# ---- 48-hour cleanup loop --------------------------------------------------------------

async def test_cleanup_deletes_expired_tickets_in_both_categories(world):
    past, future = int(time.time()) - 5, int(time.time()) + 3600
    expired_archive = archived(world, topic=f"ticket-owner:1 | delete_at:{past}")
    expired_in_place = ticket(world, topic=f"ticket-owner:1 | delete_at:{past}", name="closed-x")
    not_yet = archived(world, topic=f"ticket-owner:1 | delete_at:{future}")
    still_open = ticket(world)
    busy = archived(world, topic=f"ticket-owner:1 | delete_at:{past}")
    broken = archived(world, topic=f"ticket-owner:1 | delete_at:{past}")
    broken.delete = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "x"))
    expired_new = closed(world, delete_at=past)            # deadline in the database, plain topic
    not_yet_new = closed(world, delete_at=future)
    world.t._deadlines[424242] = past                      # channel deleted while the bot was offline
    world.archive.text_channels = [expired_archive, not_yet, busy, broken, expired_new, not_yet_new]
    world.tickets.text_channels = [expired_in_place, still_open]
    lock = world.t._lifecycle_lock(busy.id)
    await lock.acquire()
    try:
        await world.t._cleanup_expired()   # one iteration of the loop body
    finally:
        lock.release()
    for ch in (expired_archive, expired_in_place, broken, expired_new):
        ch.delete.assert_awaited_once()
    assert expired_archive.delete.call_args.kwargs["reason"] == "48-hour post-closure retention expired"
    for ch in (not_yet, still_open, busy, not_yet_new):
        ch.delete.assert_not_awaited()
    assert not world.t._closing_channels   # a failed delete doesn't leave the ticket stuck
    assert expired_new.id not in world.t._deadlines and 424242 not in world.t._deadlines
    assert world.t.delete_at(broken) is not None and world.t.delete_at(not_yet_new) == future


# ---- SLA alerts ----------------------------------------------------------------------------

@pytest.fixture
async def sla(world, db):
    world.bot.db = db
    yield world
    world.bot.db = None


def open_ticket_aged(world, minutes, *, status=STATUS_AWAITING, extra=()):
    created = h.ago(minutes=minutes)
    return h.text_channel(guild=world.guild, created_at=created,
                          messages=[h.welcome(status, created), *extra])


async def test_sla_alerts_once_per_ticket(sla):
    waiting = open_ticket_aged(sla, 20, extra=[h.msg(h.OWNER, h.ago(minutes=19), "hello??")])
    sla.tickets.text_channels = [waiting]
    await sla.t._sla_check()
    waiting.send.assert_awaited_once()
    assert h.last_text(waiting.send) == ("⚠️ **SLA Warning:** This ticket has been waiting for staff response "
                                        f"for over 15 minutes! <@&{h.STAFF_ROLE_ID}>")
    assert await sla.bot.db.sla_alerted(waiting.id)
    await sla.t._sla_check()
    sla.t._sla_done.clear()               # a restart forgets the in-memory cache...
    await sla.t._sla_check()
    waiting.send.assert_awaited_once()    # ...but the DB still prevents a repeat


@pytest.mark.parametrize("case", ["fresh", "claimed", "archived"])
async def test_sla_skips_tickets_that_dont_qualify(sla, case):
    ch = {"fresh": lambda: open_ticket_aged(sla, 5),
          "claimed": lambda: open_ticket_aged(sla, 30, status=CLAIMED),
          "archived": lambda: archived(sla)}[case]()
    sla.tickets.text_channels = [ch]
    await sla.t._sla_check()
    ch.send.assert_not_awaited()


async def test_sla_fixes_a_stale_card_instead_of_alerting(sla):
    # Staff replied while the bot was offline, so the card still says Awaiting.
    ch = open_ticket_aged(sla, 30, extra=[h.msg(h.STAFF, h.ago(minutes=25), "on it")])
    sla.tickets.text_channels = [ch]
    await sla.t._sla_check()
    ch.send.assert_not_awaited()
    assert get_ticket_status(ch.msgs[0]) == STATUS_IN_PROGRESS


async def test_sla_retries_after_a_failed_send(sla):
    ch = open_ticket_aged(sla, 40)
    ch.send = AsyncMock(side_effect=[discord.HTTPException(MagicMock(status=500), "x"), None])
    sla.tickets.text_channels = [ch]
    await sla.t._sla_check()
    assert not await sla.bot.db.sla_alerted(ch.id)
    await sla.t._sla_check()
    assert await sla.bot.db.sla_alerted(ch.id) and ch.send.await_count == 2


async def test_sla_can_post_to_a_dedicated_channel(sla):
    alerts = MagicMock(spec=discord.TextChannel)
    alerts.send = AsyncMock()
    sla.bot.sla_alert_channel = lambda _guild: alerts
    ch = open_ticket_aged(sla, 40)
    sla.tickets.text_channels = [ch]
    await sla.t._sla_check()
    assert h.last_text(alerts.send).startswith(f"{ch.mention} — ⚠️ **SLA Warning:**")
    ch.send.assert_not_awaited()


async def test_sla_pings_the_role_set_with_set_staff_role(sla, monkeypatch):
    monkeypatch.setattr(h.Guild, "get_role", lambda self, rid: NS(id=rid, mention=f"<@&{rid}>"))
    await sla.bot.settings.update(h.GUILD_ID, staff_role_id=77)
    ch = open_ticket_aged(sla, 40)
    sla.tickets.text_channels = [ch]
    await sla.t._sla_check()
    assert h.last_text(ch.send).endswith("<@&77>")
    assert [r.id for r in ch.send.call_args.kwargs["allowed_mentions"].roles] == [77]


async def test_sla_is_paused_without_a_database(world):
    ch = open_ticket_aged(world, 60)
    world.tickets.text_channels = [ch]
    await world.t._sla_check()   # no DB = no once-only guarantee, so nothing is sent
    ch.send.assert_not_awaited()


async def test_sla_falls_back_to_the_ticket_when_the_alert_channel_fails(sla):
    alerts = MagicMock(spec=discord.TextChannel)
    alerts.name = "ticket-sla-alerts"
    alerts.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "Missing Access"))
    sla.bot.sla_alert_channel = lambda _guild: alerts
    ch = open_ticket_aged(sla, 40)
    sla.tickets.text_channels = [ch]
    await sla.t._sla_check()
    ch.send.assert_awaited_once()                       # alerted in the ticket instead
    assert await sla.bot.db.sla_alerted(ch.id)


async def test_loops_cover_every_server(sla):
    """The SLA and cleanup loops walk every server, each with its own settings."""
    other_cat = h.category(600)
    other = h.Guild({600: other_cat})
    other.id = 500
    other_cat.guild = other
    await sla.bot.settings.update(500, tickets_category_id=600)
    sla.bot.managed_guilds = lambda: [sla.guild, other]
    here, there = open_ticket_aged(sla, 40), h.text_channel(guild=other, created_at=h.ago(minutes=40),
                                                             messages=[h.welcome(STATUS_AWAITING, h.ago(minutes=40))])
    sla.tickets.text_channels, other_cat.text_channels = [here], [there]
    await sla.t._sla_check()
    here.send.assert_awaited_once()
    there.send.assert_awaited_once()
    assert h.last_text(there.send).endswith("minutes!")       # server 500 has no staff role: no ping
    expired = h.text_channel(guild=other, category_id=600)
    sla.t._deadlines[expired.id] = int(time.time()) - 5
    other_cat.text_channels = [expired]
    await sla.t._cleanup_expired()
    expired.delete.assert_awaited_once()
