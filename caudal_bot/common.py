"""Shared building blocks with no dependencies on the rest of the package.

Constants, ticket-topic parsing, the welcome/archive/status embed helpers, and small
formatting utilities. Everything else imports from here, never the other way round,
which is what keeps the package free of circular imports.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import datetime as dt
import logging
import re
from dataclasses import dataclass

import discord
from discord.ext import tasks

log = logging.getLogger("faq_bot")


# The ticket owner's ID is stored in the channel topic. This is more reliable
# than matching on the channel name, since usernames can change.
TICKET_TOPIC_PREFIX = "ticket-owner:"
CLOSE_COUNTDOWN_SECONDS = 5
CATEGORY_CHANNEL_LIMIT = 50
GUILD_CHANNEL_LIMIT = 500

# Closed tickets are archived (read-only for the owner) and deleted after this long.
# The deadline is stored in the database (archived_tickets), so it survives restarts.
# Versions before that stored it in the topic as "| delete_at:<unix>"; still read.
ARCHIVE_RETENTION_SECONDS = 48 * 60 * 60
CLEANUP_INTERVAL_MINUTES = 30
# Open tickets still "Awaiting Response" this long after creation get one staff ping.
SLA_THRESHOLD_SECONDS = 15 * 60
SLA_CHECK_INTERVAL_MINUTES = 5
OPEN_PREFIX = "ticket-"
CLOSED_PREFIX = "closed-"  # only on tickets closed by older versions, which renamed them
# discord.py silently waits out rate limits, which would hang the interaction. Give up after this.
CHANNEL_EDIT_TIMEOUT_SECONDS = 15
CLOSE_REASON_MAX_LENGTH = 200
_DELETE_AT_RE = re.compile(r"\|\s*delete_at:(\d+)")

# Embed accent colours (Discord's own palette).
COLOR_FAQ = discord.Color(0x5865F2)     # Blurple
COLOR_TICKET = discord.Color(0x2ECC71)  # Emerald
COLOR_CLOSE = discord.Color(0xED4245)   # Crimson


def ticket_owner_id(channel: discord.abc.GuildChannel | discord.Thread | None) -> int | None:
    if not isinstance(channel, discord.TextChannel) or not channel.topic:
        return None
    if not channel.topic.startswith(TICKET_TOPIC_PREFIX):
        return None
    try:
        return int(channel.topic.removeprefix(TICKET_TOPIC_PREFIX).split()[0])
    except (ValueError, IndexError):
        return None


def ticket_delete_at(channel: discord.abc.GuildChannel | discord.Thread | None) -> int | None:
    """Deletion time stored in the topic by older versions, or None. The live source of
    truth is TicketsCog.delete_at, which checks the database first."""
    if ticket_owner_id(channel) is None:
        return None
    match = _DELETE_AT_RE.search(channel.topic)
    return int(match.group(1)) if match else None


def ticket_topic(owner_id: int, delete_at: int | None = None) -> str:
    topic = f"{TICKET_TOPIC_PREFIX}{owner_id}"
    return topic if delete_at is None else f"{topic} | delete_at:{delete_at}"


def swap_name_prefix(name: str, old: str, new: str) -> str:
    # Works from the current name rather than the owner's username, which may
    # have changed (or the owner may have left) since the ticket was opened.
    return (new + name[len(old):])[:100] if name.startswith(old) else name


def ticket_channel_name(member: discord.abc.User) -> str:
    slug = re.sub(r"[^a-z0-9_-]", "",member.name.lower().replace(" ", "-").replace(".", "-"))
    return f"{OPEN_PREFIX}{slug or member.id}"[:100]


ZWSP = "\N{ZERO WIDTH SPACE}"
# Zero-width / bidi-control / BOM characters users may paste in. Stripped so they
# can't hide a ``` from the escaper below or smuggle invisible junk into embeds.
_INVISIBLE_CHARS = re.compile(r"[​-‏‪-‮⁠-⁤﻿]")
_CODE_FENCE = "```\n{}\n```"


def _code_block(text: str, limit: int = 1000) -> str:
    """Wrap user text in a code block whose total length never exceeds ``limit``."""
    text = _INVISIBLE_CHARS.sub("", text).strip()
    # Split every pair of adjacent backticks so user text can't close the block
    # early. (Replacing only "```" isn't enough: "````" becomes "`<ZWSP>```".)
    # Escaping happens before truncation so the final length is exact.
    text = re.sub(r"`(?=`)", f"`{ZWSP}", text)
    budget = limit - len(_CODE_FENCE.format(""))
    if len(text) > budget:
        text = text[: budget - 3].rstrip() + "..."
    return _CODE_FENCE.format(text)


def _blockquote(text: str) -> str:
    # Line-by-line "> " (unlike ">>>") ends with the text, so whatever follows
    # stays outside the quote. Blank lines get a ZWSP: a bare ">" isn't a quote
    # in Discord and would split the bar in two.
    return "\n".join(f"> {line}" if line.strip() else f"> {ZWSP}" for line in text.splitlines())


def build_ticket_welcome_embed(
    member: discord.Member, staff_role: discord.Role | None, question: str | None
) -> discord.Embed:
    team = staff_role.mention if staff_role else "our support team"
    embed = discord.Embed(
        title="🎫 Support Desk • Private Ticket",
        description=(
            f"Hi {member.mention}, thanks for reaching out! 👋\n"
            f"A member of {team} will be with you shortly.\n\n"
            "Please describe your issue in as much detail as possible — "
            "screenshots, error messages and steps to reproduce all help."
        ),
        color=COLOR_TICKET,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="👤 Ticket Owner", value=f"{member.mention}\n`{member.id}`", inline=True)
    embed.add_field(name="🛡️ Assigned Team", value=staff_role.mention if staff_role else "Administrators", inline=True)
    embed.add_field(name=STATUS_FIELD_NAME, value=STATUS_AWAITING, inline=True)
    if question and _INVISIBLE_CHARS.sub("", question).strip():
        embed.add_field(name="💬 Original Question", value=_code_block(question), inline=False)
    embed.set_footer(text="Staff: claim this ticket below • Use the button below or /close to close it.")
    return embed


# The status lives in the welcome embed itself, so it survives bot restarts
# without a database. These helpers read and rewrite that one field.
STATUS_FIELD_NAME = "📊 Ticket Status"
STATUS_AWAITING = "🟢 Awaiting Response"
STATUS_IN_PROGRESS = "🟡 In Progress"
STATUS_ARCHIVED = "🔒 Closed / Archived (Deletion scheduled)"
# The pre-close status is kept inside the archived value so re-opening can restore
# it exactly (including who claimed it) without any other storage.
_BEFORE_CLOSE_RE = re.compile(r"\n\*Before closing: (.+)\*$")


def archived_status(previous: str) -> str:
    if previous.startswith(STATUS_ARCHIVED):
        return previous  # already archived; don't nest "Before closing" twice
    return f"{STATUS_ARCHIVED}\n*Before closing: {previous}*"


def restored_status(archived: str) -> str:
    if not archived.startswith(STATUS_ARCHIVED):
        return archived  # not archived; leave as is
    match = _BEFORE_CLOSE_RE.search(archived)
    return match.group(1) if match else STATUS_AWAITING


def _status_field_index(embed: discord.Embed) -> int | None:
    for i, f in enumerate(embed.fields):
        if f.name == STATUS_FIELD_NAME:
            return i
    return None


def get_ticket_status(message: discord.Message) -> str | None:
    if not message.embeds:
        return None
    embed = message.embeds[0]
    i = _status_field_index(embed)
    return None if i is None else embed.fields[i].value


def with_ticket_status(message: discord.Message, status: str) -> discord.Embed | None:
    """Copy of the welcome embed with the status field replaced (None if it has no status field)."""
    if not message.embeds:
        return None
    # Embed.copy() is shallow (shares the fields list), so deep-copy the dict.
    embed = discord.Embed.from_dict(copy.deepcopy(message.embeds[0].to_dict()))
    i = _status_field_index(embed)
    if i is None:
        return None
    embed.set_field_at(i, name=STATUS_FIELD_NAME, value=status, inline=True)
    return embed


_CLAIMED_BY_RE = re.compile(r"Claimed by <@!?(\d+)>")


def claimed_staff_id(status: str | None) -> int | None:
    match = _CLAIMED_BY_RE.search(status or "")
    return int(match.group(1)) if match else None


ARCHIVE_TITLE = "🔒 Ticket Closed & Archived"
_ARCHIVE_CLOSED_BY_FIELD = "🔒 Closed By"
_ARCHIVE_REASON_FIELD = "📝 Reason"
_MENTION_RE = re.compile(r"<@!?(\d+)>")


def build_archive_embed(closed_by: discord.abc.User, delete_at: int, reason: str) -> discord.Embed:
    # <t:...:R> / <t:...:F> render client-side as a live countdown and a full date.
    embed = discord.Embed(
        title=ARCHIVE_TITLE,
        description=(
            "This ticket has been marked as resolved. You can read previous messages, "
            "but sending new messages is disabled.\n\n"
            f"⏳ **Automatic deletion:** <t:{delete_at}:R> (<t:{delete_at}:F>)."
        ),
        color=COLOR_CLOSE,
        timestamp=discord.utils.utcnow(),
    )
    # These fields double as storage: the transcript reads them back at deletion
    # time, which may be days later and after a restart.
    embed.add_field(name=_ARCHIVE_CLOSED_BY_FIELD, value=closed_by.mention, inline=True)
    embed.add_field(name=_ARCHIVE_REASON_FIELD, value=reason, inline=True)
    embed.set_footer(text="Re-open the ticket if you need more help.")
    return embed


@dataclass(frozen=True)
class ArchiveInfo:
    closed_by_id: int | None
    reason: str
    closed_at: dt.datetime


def parse_archive_message(message: discord.Message) -> ArchiveInfo | None:
    if not message.embeds or message.embeds[0].title != ARCHIVE_TITLE:
        return None
    fields = {f.name: f.value for f in message.embeds[0].fields}
    match = _MENTION_RE.search(fields.get(_ARCHIVE_CLOSED_BY_FIELD) or "")
    return ArchiveInfo(
        closed_by_id=int(match.group(1)) if match else None,
        reason=fields.get(_ARCHIVE_REASON_FIELD) or DEFAULT_CLOSE_REASON,
        closed_at=message.created_at,
    )


def build_reopen_embed(reopened_by: discord.abc.User) -> discord.Embed:
    return discord.Embed(description=f"🔓 Ticket re-opened by {reopened_by.mention}.", color=COLOR_TICKET)


def build_delete_embed(deleted_by: discord.abc.User, deadline: int) -> discord.Embed:
    # Discord renders <t:...:R> as a live relative countdown ("in 5 seconds"),
    # avoiding a flurry of rate-limited message edits.
    return discord.Embed(
        title="🗑️ Deleting Ticket",
        description=(
            f"{deleted_by.mention} is deleting this ticket.\n"
            f"The channel will be deleted <t:{deadline}:R>.\n\n"
            "-# Thank you for contacting support!"
        ),
        color=COLOR_CLOSE,
    )


def format_duration(delta: dt.timedelta) -> str:
    """Two most significant units: "45s", "4m 12s", "1h 23m", "2d 5h"."""
    seconds = max(int(delta.total_seconds()), 0)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


DEFAULT_CLOSE_REASON = "Resolved"
OWNER_CLOSE_REASON = "Closed by the ticket owner"


# Shared by the claim notice and the response-time metric, which looks for it.
CLAIM_NOTICE_MARKER = "has claimed this ticket"


async def _send_ephemeral(interaction: discord.Interaction, content: str) -> None:
    """Reply ephemerally whether or not the interaction has been responded to yet."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)
    except discord.HTTPException:
        log.warning("Failed to send ephemeral message to %s", interaction.user)


# How long shutdown waits for a cancelled loop iteration to unwind (it should take
# milliseconds; this only guards against something swallowing the cancellation).
LOOP_STOP_TIMEOUT_SECONDS = 10


async def stop_loop(loop: tasks.Loop) -> None:
    """Cancel a tasks.Loop and wait for its task to finish.

    Loop.cancel() alone returns immediately, leaving the iteration running while the
    database or HTTP session it uses is being closed. Every loop here is restart-safe
    (state lives in channel topics and the DB), so interrupting one mid-iteration is fine.
    """
    task = loop.get_task()
    loop.cancel()
    if task is None or task.done():
        return
    with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=LOOP_STOP_TIMEOUT_SECONDS)
