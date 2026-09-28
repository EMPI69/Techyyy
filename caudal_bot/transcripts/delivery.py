"""Turns a ticket channel into its outputs.

One history read feeds everything: the metrics row, the owner's close DM (a summary
only, no files), the log-channel summary with the .txt transcript, and the .html copy
kept on disk for the dashboard.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import TYPE_CHECKING

import discord

from ..common import (
    CLAIM_NOTICE_MARKER, COLOR_CLOSE, _blockquote, claimed_staff_id, format_duration,
    get_ticket_status, parse_archive_message, ticket_owner_id,
)
from .cleanup import save_html_transcript
from .generator import (
    TRANSCRIPT_HISTORY_LIMIT, TranscriptData, _Resolver, _user_label, build_html_transcript, build_transcript,
)

if TYPE_CHECKING:
    from ..main import FAQBot

log = logging.getLogger("faq_bot")


class TranscriptDelivery:
    def __init__(self, bot: FAQBot) -> None:
        self.bot = bot

    def first_response_at(
        self, messages: list[discord.Message], owner_id: int | None, until: dt.datetime
    ) -> dt.datetime | None:
        """When staff first engaged: their first message, or the claim notice, whichever came first."""
        for m in messages:  # oldest first
            if m.created_at > until:
                break
            if m.author == self.bot.user:
                if CLAIM_NOTICE_MARKER in (getattr(m, "content", "") or ""):
                    return m.created_at
                continue
            if not m.author.bot and m.author.id != owner_id and self.bot.is_staff(m.author):
                return m.created_at
        return None

    async def gather(
        self, channel: discord.TextChannel, *, deleted_by: discord.abc.User | None, deleted: bool
    ) -> TranscriptData:
        messages = [m async for m in channel.history(limit=TRANSCRIPT_HISTORY_LIMIT, oldest_first=True)]
        ours = [m for m in messages if m.author == self.bot.user]
        welcome = next((m for m in ours if get_ticket_status(m) is not None), None)
        # The latest archive card holds who closed the ticket and why.
        archive = next((info for m in reversed(ours) if (info := parse_archive_message(m))), None)

        owner_id = ticket_owner_id(channel)
        staff_id = claimed_staff_id(get_ticket_status(welcome) if welcome else None)
        closer_id = archive.closed_by_id if archive else None
        # Claimer first; otherwise whoever closed it, unless that was the owner themself.
        handled_by_id = staff_id or (closer_id if closer_id not in (None, owner_id) else None)
        owner = await self.bot.resolve_user(channel.guild, owner_id)
        staff = await self.bot.resolve_user(channel.guild, staff_id)
        closer = await self.bot.resolve_user(channel.guild, closer_id)
        now = discord.utils.utcnow()
        closed_at = archive.closed_at if archive else now
        reason = archive.reason if archive else "—"
        first_response = self.first_response_at(messages, owner_id, closed_at)
        response_time = format_duration(first_response - channel.created_at) if first_response else "No staff response"

        text = build_transcript(
            channel_name=channel.name,
            opener=_user_label(owner, owner_id),
            claimed_staff=_user_label(staff, staff_id) if staff_id else "Unclaimed",
            closed_by=_user_label(closer, closer_id) if closer_id else "—",
            reason=reason,
            opened_at=channel.created_at,
            closed_at=closed_at,
            messages=messages,
            truncated=len(messages) >= TRANSCRIPT_HISTORY_LIMIT,
            deleted_by=(_user_label(deleted_by) if deleted_by else "Automatic (48-hour retention)") if deleted else None,
            deleted_at=now if deleted else None,
            response_time=response_time,
        )
        guild = channel.guild
        resolver = _Resolver(
            users={m.author.id: getattr(m.author, "display_name", None) or m.author.name for m in messages}
            | {u.id: getattr(u, "display_name", None) or u.name for u in (owner, staff, closer) if u is not None},
            roles={r.id: r.name for r in getattr(guild, "roles", [])},
            channels={c.id: c.name for c in getattr(guild, "channels", [])},
        )
        meta = [
            ("Opener", _user_label(owner, owner_id)),
            ("Handled By", _user_label(await self.bot.resolve_user(guild, handled_by_id), handled_by_id) if handled_by_id else "—"),
            ("Closed By", _user_label(closer, closer_id) if closer_id else "—"),
            ("Close Reason", reason),
            ("Response Time", response_time),
            ("Resolution", format_duration(closed_at - channel.created_at)),
            ("Opened", channel.created_at.strftime("%Y-%m-%d %H:%M UTC")),
            ("Closed", closed_at.strftime("%Y-%m-%d %H:%M UTC") if archive else "— (still open)"),
        ]
        if deleted:
            meta.append(("Deleted By", _user_label(deleted_by) if deleted_by else "Automatic (48-hour retention)"))
        page = build_html_transcript(
            guild_name=getattr(guild, "name", "Discord server"),
            channel_name=channel.name,
            meta=meta,
            messages=messages,
            resolver=resolver,
            generated_by=getattr(self.bot.user, "name", "Support Bot"),
        )
        return TranscriptData(
            text=text,
            owner=owner,
            owner_id=owner_id,
            staff_id=staff_id,
            closer_id=closer_id,
            reason=reason,
            duration=format_duration(closed_at - channel.created_at),
            message_count=len(messages),
            response_time=response_time,
            handled_by_id=handled_by_id,
            opened_at=channel.created_at,
            closed_at=closed_at,
            first_response_at=first_response,
            html=page,
        )

    async def post_to_log(
        self, channel: discord.TextChannel, deleted_by: discord.abc.User | None, data: TranscriptData
    ) -> None:
        log_channel = self.bot.transcript_channel(channel.guild)
        if log_channel is None:
            return
        perms = log_channel.permissions_for(log_channel.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            log.warning("Missing View/Send/Embed Links in #%s; skipping transcript", log_channel.name)
            return

        embed = discord.Embed(
            title=f"📁 Ticket Deleted • #{channel.name}", color=COLOR_CLOSE, timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="👤 Owner", value=f"<@{data.owner_id}>\n`{data.owner_id}`", inline=True)
        embed.add_field(name="🛡️ Handled By", value=f"<@{data.handled_by_id}>" if data.handled_by_id else "—", inline=True)
        embed.add_field(name="🔒 Closed By", value=f"<@{data.closer_id}>" if data.closer_id else "—", inline=True)
        embed.add_field(name="⚡ Response Time", value=data.response_time, inline=True)
        embed.add_field(name="⏱️ Total Resolution Time", value=data.duration, inline=True)
        embed.add_field(name="🗑️ Deleted By", value=deleted_by.mention if deleted_by else "Automatic (48h)", inline=True)
        embed.add_field(name="💬 Messages", value=str(data.message_count), inline=True)
        embed.add_field(name="📝 Reason", value=_blockquote(data.reason), inline=False)

        kwargs: dict = {"embed": embed, "allowed_mentions": discord.AllowedMentions.none()}
        if perms.attach_files:
            # Plain text only; the styled .html stays on disk for the dashboard.
            kwargs["files"] = [data.as_file(channel.name)]
        else:
            log.warning("Missing Attach Files in #%s; posting summary without transcript", log_channel.name)
            embed.set_footer(text="Transcript not attached: the bot lacks Attach Files permission here.")
        try:
            await log_channel.send(**kwargs)
        except discord.HTTPException:
            log.exception("Failed to post transcript for #%s", channel.name)

    async def dm_owner(
        self,
        channel: discord.TextChannel,
        data: TranscriptData,
        closed_by: discord.abc.User,
        reason: str,
        delete_at: int,
    ) -> None:
        """Best-effort DM to the ticket owner: a closure summary embed, no attachments."""
        if data.owner is None:
            log.info("Ticket owner %s of #%s could not be resolved; no DM sent", data.owner_id, channel.name)
            return
        embed = discord.Embed(
            title="🔒 Your ticket was closed",
            description=(
                f"Your support ticket **#{channel.name}** in **{channel.guild.name}** was closed.\n\n"
                f"You can still read it and re-open it from the channel until it's deleted "
                f"<t:{delete_at}:R>."
            ),
            color=COLOR_CLOSE,
            timestamp=discord.utils.utcnow(),
        )
        # Plain names, not mentions: mentions of users the client hasn't cached render as raw IDs in DMs.
        embed.add_field(name="🔒 Closed By", value=closed_by.name, inline=True)
        embed.add_field(name="📝 Reason", value=_blockquote(reason), inline=False)
        embed.set_footer(text=channel.guild.name)
        try:
            await data.owner.send(embed=embed)
        except discord.Forbidden:
            log.info("%s has DMs closed; closure notice for #%s not delivered", data.owner, channel.name)
        except discord.HTTPException:
            log.warning("Failed to DM the closure notice for #%s to %s", channel.name, data.owner)

    async def record_metrics(self, channel: discord.TextChannel, data: TranscriptData) -> None:
        """Upsert this ticket's metrics row. Called on close and on delete; never raises."""
        if self.bot.db is None or data.owner_id is None or data.opened_at is None:
            return
        try:
            await self.bot.db.upsert_metrics(
                channel_id=channel.id,
                opener_id=data.owner_id,
                staff_id=data.handled_by_id,
                created_at=data.opened_at,
                first_response_at=data.first_response_at,
                closed_at=data.closed_at or discord.utils.utcnow(),
                guild_id=channel.guild.id,
            )
        except Exception:
            log.exception("Failed to record metrics for #%s", channel.name)

    async def store(self, channel: discord.TextChannel, data: TranscriptData) -> None:
        """Keep the HTML transcript on disk for the dashboard. Best-effort; never raises."""
        if not data.html:
            return
        try:
            await asyncio.to_thread(
                save_html_transcript, self.bot.config.transcripts_dir, channel.id, channel.name, data.html
            )
        except Exception:
            log.exception("Failed to save HTML transcript for #%s", channel.name)
