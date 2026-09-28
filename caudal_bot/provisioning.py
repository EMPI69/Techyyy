"""Find-or-create the server structure the bot needs: used by /setup, and by ticket
opening when the tickets category is missing.

Existing objects are always reused, matched first by their stored ID (so a renamed
category is still found) and then by name, so running /setup again is safe and never
duplicates anything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import discord

log = logging.getLogger("faq_bot")

TICKETS_CATEGORY_NAME = "🎫 TICKETS"
ARCHIVE_CATEGORY_NAME = "📦 ARCHIVED TICKETS"
TRANSCRIPTS_CHANNEL_NAME = "ticket-transcripts"
SLA_ALERTS_CHANNEL_NAME = "ticket-sla-alerts"
_REASON = "Support bot setup"


def private_overwrites(
    guild: discord.Guild, staff_role: discord.Role | None, *, staff_can_send: bool = True
) -> dict[discord.Role | discord.Member, discord.PermissionOverwrite]:
    """Hidden from @everyone; visible to the staff role and the bot."""
    overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, embed_links=True, attach_files=True,
            read_message_history=True, manage_channels=True,
        ),
    }
    if staff_role is not None:
        overwrites[staff_role] = discord.PermissionOverwrite(
            view_channel=True, read_message_history=True, send_messages=staff_can_send
        )
    return overwrites


def missing_permissions(guild: discord.Guild) -> list[str]:
    """Guild-level permissions the bot lacks to create channels and set their permissions."""
    perms = guild.me.guild_permissions
    return [label for label, ok in (("Manage Channels", perms.manage_channels), ("Manage Roles", perms.manage_roles))
            if not ok]


async def ensure_overwrites(channel: discord.abc.GuildChannel, wanted: dict) -> bool:
    """Add ``wanted`` to the channel's overwrites, keeping everything else. One request, and
    only if something differs. Returns True if it changed anything."""
    current = dict(channel.overwrites)
    changed = False
    for target, overwrite in wanted.items():
        key = next((k for k in current if k.id == target.id), target)
        merged = current.get(key) or discord.PermissionOverwrite()
        for perm, value in overwrite:
            if value is not None and getattr(merged, perm) != value:
                merged.update(**{perm: value})
                changed = True
        current[key] = merged
    if changed:
        await channel.edit(overwrites=current, reason=_REASON)
    return changed


async def find_or_create_category(
    guild: discord.Guild, name: str, *, known_id: int | None, overwrites: dict, enforce: bool
) -> tuple[discord.CategoryChannel, bool]:
    """(category, created). ``enforce`` also applies ``overwrites`` to a category that exists."""
    category = guild.get_channel(known_id) if known_id else None
    if not isinstance(category, discord.CategoryChannel):
        category = discord.utils.get(guild.categories, name=name)
    if category is None:
        category = await guild.create_category(name, overwrites=overwrites, reason=_REASON)
        log.info("Created category %r in %s", name, guild.name)
        return category, True
    if enforce:
        await ensure_overwrites(category, overwrites)
    return category, False


async def find_or_create_text_channel(
    guild: discord.Guild, name: str, *, known_id: int | None, category: discord.CategoryChannel | None,
    overwrites: dict,
) -> tuple[discord.TextChannel, bool]:
    """(channel, created). An existing channel keeps its place but gets ``overwrites`` applied."""
    channel = guild.get_channel(known_id) if known_id else None
    if not isinstance(channel, discord.TextChannel):
        channel = discord.utils.get(guild.text_channels, name=name)
    if channel is None:
        channel = await guild.create_text_channel(name, category=category, overwrites=overwrites, reason=_REASON)
        log.info("Created #%s in %s", name, guild.name)
        return channel, True
    await ensure_overwrites(channel, overwrites)
    return channel, False


@dataclass(frozen=True)
class SetupResult:
    tickets: tuple[discord.CategoryChannel, bool]
    archive: tuple[discord.CategoryChannel, bool]
    transcripts: tuple[discord.TextChannel, bool]
    sla_alerts: tuple[discord.TextChannel, bool]


async def provision_guild(guild: discord.Guild, staff_role: discord.Role, current) -> SetupResult:
    """Find or create everything /setup manages. ``current`` is the server's GuildSettings.

    The tickets category's permissions are only set when it's created: an existing one
    may hold public channels (e.g. "how to open a ticket") that must stay visible. Ticket
    channels never inherit it anyway; each gets its own overwrites.
    """
    tickets = await find_or_create_category(
        guild, TICKETS_CATEGORY_NAME, known_id=current.tickets_category_id,
        overwrites=private_overwrites(guild, staff_role), enforce=False)
    archive = await find_or_create_category(
        guild, ARCHIVE_CATEGORY_NAME, known_id=current.archive_category_id,
        overwrites=private_overwrites(guild, staff_role), enforce=True)
    transcripts = await find_or_create_text_channel(
        guild, TRANSCRIPTS_CHANNEL_NAME, known_id=current.transcript_log_channel_id, category=archive[0],
        overwrites=private_overwrites(guild, staff_role, staff_can_send=False))   # read-only audit log
    sla_alerts = await find_or_create_text_channel(
        guild, SLA_ALERTS_CHANNEL_NAME, known_id=current.sla_alert_channel_id, category=archive[0],
        overwrites=private_overwrites(guild, staff_role))
    return SetupResult(tickets, archive, transcripts, sla_alerts)
