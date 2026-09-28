"""Fakes for Discord objects. Plain objects and spec'd mocks only: nothing here talks to Discord."""

from __future__ import annotations

import datetime as dt
import itertools
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import discord

from caudal_bot.common import STATUS_AWAITING, build_ticket_welcome_embed

UTC = dt.timezone.utc
GUILD_ID, STAFF_ROLE_ID, TICKETS_CAT, ARCHIVE_CAT = 1, 9, 5, 6
OWNER_ID, STAFF_ID, OUTSIDER_ID = 1, 2, 3

_ids = itertools.count(10_000)


def now() -> dt.datetime:
    return dt.datetime.now(UTC)


def ago(**kw) -> dt.datetime:
    return now() - dt.timedelta(**kw)


def user(uid: int, name: str, *, bot: bool = False) -> NS:
    return NS(id=uid, name=name, display_name=name.title(), mention=f"<@{uid}>", bot=bot,
              display_avatar=NS(url=f"https://cdn.discordapp.com/avatars/{uid}.png"))


ME = user(999, "supportbot", bot=True)
OWNER = user(OWNER_ID, "bob")
STAFF = user(STAFF_ID, "mod")
OUTSIDER = user(OUTSIDER_ID, "eve")


def member(uid: int, *, admin: bool = False, name: str = "admin") -> MagicMock:
    m = MagicMock(spec=discord.Member)
    m.id, m.name, m.mention, m.display_name = uid, name, f"<@{uid}>", name.title()
    m.guild_permissions = discord.Permissions(administrator=admin)
    return m


def msg(author, at: dt.datetime, content: str = "", *, embeds=(), attachments=(), mid: int | None = None) -> NS:
    m = NS(id=mid or next(_ids), author=author, content=content, clean_content=content, embeds=list(embeds),
           attachments=list(attachments), stickers=[], created_at=at)
    async def edit(**kw):
        if "embed" in kw:
            m.embeds = [kw["embed"]]
    m.edit = AsyncMock(side_effect=edit)
    m.delete = AsyncMock()
    return m


def welcome(status: str = STATUS_AWAITING, at: dt.datetime | None = None, *, question: str | None = None) -> NS:
    embed = build_ticket_welcome_embed(NS(mention=f"<@{OWNER_ID}>", id=OWNER_ID), NS(mention=f"<@&{STAFF_ROLE_ID}>"), question)
    embed.set_field_at(2, name=embed.fields[2].name, value=status, inline=True)
    return msg(ME, at or ago(hours=1), embeds=[embed])


def category(cid: int, *, size: int = 0, channels=()) -> MagicMock:
    c = MagicMock(spec=discord.CategoryChannel)
    c.id = cid
    c.guild = NS(id=GUILD_ID)
    c.text_channels = list(channels)
    c.channels = [object()] * size
    return c


class Guild(NS):
    """Minimal guild: channel lookup by id, no members cached (the bot runs without that intent)."""

    def __init__(self, categories: dict[int, MagicMock] | None = None, **kw):
        super().__init__(id=GUILD_ID, name="Caudal", me=ME, roles=[], channels=[], **kw)
        self.categories = categories or {}

    def get_channel(self, cid):
        return self.categories.get(cid)

    def get_member(self, _uid):
        return None

    def get_role(self, rid):
        return NS(id=rid, mention=f"<@&{rid}>") if rid == STAFF_ROLE_ID else None


def text_channel(cid: int | None = None, *, topic: str | None = f"ticket-owner:{OWNER_ID}", name: str = "ticket-bob",
                 category_id: int = TICKETS_CAT, created_at: dt.datetime | None = None, guild=None,
                 messages=None) -> MagicMock:
    """A ticket channel whose history, sends and edits are all local."""
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = cid or next(_ids)
    ch.name, ch.topic, ch.category_id = name, topic, category_id
    ch.mention = f"<#{ch.id}>"
    ch.created_at = created_at or ago(hours=2)
    ch.guild = guild or Guild()
    ch.overwrites = {discord.Object(OWNER_ID, type=discord.Member):
                     discord.PermissionOverwrite(view_channel=True, send_messages=True)}
    ch.msgs = list(messages or [])
    ch.edit = AsyncMock()
    ch.delete = AsyncMock()

    async def send(content=None, **kw):
        sent = msg(ME, now(), content or "", embeds=[kw["embed"]] if kw.get("embed") else [])
        sent.kwargs = kw
        ch.msgs.append(sent)
        return sent
    ch.send = AsyncMock(side_effect=send)

    async def history(**kw):
        for m in list(ch.msgs)[: kw.get("limit") or None]:
            yield m
    ch.history = history

    async def fetch_message(mid):
        for m in ch.msgs:
            if m.id == mid:
                return m
        raise discord.NotFound(MagicMock(status=404), "unknown message")
    ch.fetch_message = AsyncMock(side_effect=fetch_message)
    return ch


def interaction(channel=None, who=None, *, message=None, done: bool = False, guild=None) -> MagicMock:
    """Tracks whether it has been responded to, like the real thing."""
    i = MagicMock()
    i.channel, i.user, i.message = channel, who, message
    i.guild = guild or (channel.guild if channel is not None else None)
    state = {"done": done}
    i.response.is_done = lambda: state["done"]

    async def respond(*_a, **_kw):
        state["done"] = True
    for name in ("defer", "send_message", "send_modal", "edit_message"):
        setattr(i.response, name, AsyncMock(side_effect=respond))
    i.followup.send = AsyncMock()
    return i


def last_text(mock) -> str:
    """First positional argument of the most recent call (the message text)."""
    return mock.call_args[0][0]


def reply_text(i) -> str:
    """Text of the reply an interaction got, whether it came as a response or a followup."""
    sent = i.followup.send if i.followup.send.await_count else i.response.send_message
    return last_text(sent)


def log_channel(*, attach: bool = True, fail: bool = False) -> MagicMock:
    lg = MagicMock(spec=discord.TextChannel)
    lg.name, lg.id = "ticket-logs", 77
    lg.guild = NS(id=GUILD_ID, me=ME)
    lg.permissions_for = lambda _m: NS(view_channel=True, send_messages=True, embed_links=True, attach_files=attach)
    lg.posted = []

    async def send(**kw):
        if fail:
            raise discord.HTTPException(MagicMock(status=500), "boom")
        m = msg(ME, now(), embeds=[kw["embed"]])
        m.kwargs = kw
        lg.posted.append(m)
        return m
    lg.send = AsyncMock(side_effect=send)
    return lg


class SetupGuild:
    """A guild whose channels and categories can be created and edited locally, for /setup
    and for opening tickets. Everything it creates is findable through the same lookups."""

    def __init__(self, gid: int = GUILD_ID, *, name: str = "Caudal", can_manage: bool = True, roles=()):
        self.id, self.name = gid, name
        # Mocks, not namespaces: both are used as (hashable) permission-overwrite keys.
        self.default_role = MagicMock(spec=discord.Role, id=gid)
        self.default_role.name = "@everyone"
        self.me = MagicMock(spec=discord.Member, id=ME.id, guild_permissions=discord.Permissions(
            manage_channels=can_manage, manage_roles=can_manage))
        self._channels: dict[int, MagicMock] = {}
        self._roles = {r.id: r for r in roles}
        self.create_category = AsyncMock(side_effect=self._create_category)
        self.create_text_channel = AsyncMock(side_effect=self._create_text_channel)

    @property
    def channels(self):
        return list(self._channels.values())

    @property
    def categories(self):
        return [c for c in self.channels if isinstance(c, discord.CategoryChannel)]

    @property
    def text_channels(self):
        return [c for c in self.channels if isinstance(c, discord.TextChannel)]

    def get_channel(self, cid):
        return self._channels.get(cid)

    def get_role(self, rid):
        return self._roles.get(rid)

    def get_member(self, _uid):
        return None

    def add(self, spec, name: str, *, overwrites=None, category=None, topic=None) -> MagicMock:
        c = MagicMock(spec=spec)
        c.id, c.name, c.mention, c.topic, c.guild = next(_ids), name, f"<#{next(_ids)}>", topic, self
        c.mention = f"<#{c.id}>"
        c.overwrites = dict(overwrites or {})
        c.category_id = category.id if category is not None else None
        c.channels, c.text_channels = [], []
        if category is not None:
            category.channels.append(c)
            category.text_channels.append(c)

        async def edit(**kw):
            c.overwrites = kw.get("overwrites", c.overwrites)
        c.edit = AsyncMock(side_effect=edit)
        c.send = AsyncMock()
        self._channels[c.id] = c
        return c

    def remove(self, channel) -> None:
        self._channels.pop(channel.id, None)

    async def _create_category(self, name, *, overwrites=None, reason=None, **_kw):
        return self.add(discord.CategoryChannel, name, overwrites=overwrites)

    async def _create_text_channel(self, name, *, category=None, overwrites=None, topic=None, reason=None, **_kw):
        return self.add(discord.TextChannel, name, overwrites=overwrites, category=category, topic=topic)


def role(rid: int, *, default: bool = False, managed: bool = False) -> MagicMock:
    r = MagicMock(spec=discord.Role)
    r.id, r.name, r.mention, r.managed = rid, f"role{rid}", f"<@&{rid}>", managed
    r.is_default = lambda: default
    return r


def member_in(guild, uid: int, *role_ids: int, admin: bool = False, name: str = "user") -> MagicMock:
    """A member of ``guild`` holding ``role_ids`` (what the real is_staff checks)."""
    m = member(uid, admin=admin, name=name)
    m.guild = guild
    m.get_role = lambda rid: object() if rid in role_ids else None
    return m
