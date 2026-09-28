"""Ticket lifecycle: creation and permissions, claiming and the status card,
close -> archive -> re-open / delete, the 48-hour cleanup loop, the 15-minute SLA
alert loop, and purge execution."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import TYPE_CHECKING

import discord
from discord.ext import commands, tasks

from ..common import (
    ARCHIVE_RETENTION_SECONDS, CATEGORY_CHANNEL_LIMIT, CHANNEL_EDIT_TIMEOUT_SECONDS, CLAIM_NOTICE_MARKER,
    CLEANUP_INTERVAL_MINUTES, CLOSE_COUNTDOWN_SECONDS, CLOSE_REASON_MAX_LENGTH, CLOSED_PREFIX,
    GUILD_CHANNEL_LIMIT, OPEN_PREFIX, _INVISIBLE_CHARS,
    SLA_CHECK_INTERVAL_MINUTES, SLA_THRESHOLD_SECONDS, STATUS_AWAITING, STATUS_IN_PROGRESS, TICKET_TOPIC_PREFIX,
    _send_ephemeral, archived_status, stop_loop, build_archive_embed, build_delete_embed, build_reopen_embed,
    build_ticket_welcome_embed, get_ticket_status, restored_status, swap_name_prefix, ticket_channel_name,
    ticket_delete_at, ticket_owner_id, ticket_topic, with_ticket_status,
)
from ..provisioning import TICKETS_CATEGORY_NAME, find_or_create_category, private_overwrites
from ..transcripts.generator import TRANSCRIPT_TIMEOUT_SECONDS
from ..views.ticket_lifecycle import (
    CloseReasonModal, ClosedTicketView, ConfirmCloseView, TicketControlView,
)

if TYPE_CHECKING:
    from ..main import FAQBot

log = logging.getLogger("faq_bot")


class TicketsCog(commands.Cog, name="Tickets"):
    def __init__(self, bot: FAQBot) -> None:
        self.bot = bot
        self._ticket_locks: dict[int, asyncio.Lock] = {}   # per-user, prevents double-click races
        self._closing_channels: set[int] = set()            # prevents double-close
        self._status_locks: dict[int, asyncio.Lock] = {}   # per-channel, serialises status edits
        # Tickets whose status has left "Awaiting Response" (or can't be tracked), so
        # later staff messages skip the lookup. Rebuilt lazily after a restart.
        self._status_settled: set[int] = set()
        # Per-channel: serialises archive / re-open / delete so they can't interleave.
        self._lifecycle_locks: dict[int, asyncio.Lock] = {}
        self._purge_running = False
        # Open tickets needing no further SLA checks (answered, or already alerted), so the loop
        # stops re-reading them. In memory only; rebuilt cheaply from the DB after a restart.
        self._sla_done: set[int] = set()
        # channel_id -> deletion time of each archived ticket. Mirrors the archived_tickets
        # table (loaded in cog_load); the channel itself is never renamed or re-topiced,
        # because Discord allows only 2 name/topic edits per channel every 10 minutes.
        self._deadlines: dict[int, int] = {}
        # Per-server: serialises creating the tickets category, so two tickets opened at
        # once in a server that has none can't create it twice.
        self._provision_locks: dict[int, asyncio.Lock] = {}

    async def cog_load(self) -> None:
        if self.bot.db is not None:
            try:
                self._deadlines = await self.bot.db.archive_deadlines()
            except Exception:
                log.exception("Could not load archived-ticket deadlines")
        self._cleanup_expired.start()
        self._sla_check.start()

    # ---- archive state --------------------------------------------------- #

    def delete_at(self, channel) -> int | None:
        """When an archived ticket gets deleted, or None if it's open (or not a ticket).

        Tickets closed by versions before the database held this carry it in their
        topic instead ("| delete_at:<unix>"); those are still honoured.
        """
        if ticket_owner_id(channel) is None:
            return None
        return self._deadlines.get(channel.id) or ticket_delete_at(channel)

    async def _set_deadline(self, channel_id: int, delete_at: int) -> None:
        self._deadlines[channel_id] = delete_at  # before any await, so a second click sees it
        if self.bot.db is None:
            log.warning("No database: the deletion time of ticket %s won't survive a restart", channel_id)
            return
        try:
            await self.bot.db.set_archive_deadline(channel_id, delete_at)
        except Exception:
            log.exception("Could not save the deletion time of ticket %s; it won't survive a restart", channel_id)

    async def _clear_deadline(self, channel_id: int) -> None:
        self._deadlines.pop(channel_id, None)
        if self.bot.db is None:
            return
        try:
            await self.bot.db.clear_archive_deadline(channel_id)
        except Exception:
            log.exception("Could not clear the deletion time of ticket %s", channel_id)

    @commands.Cog.listener("on_guild_channel_delete")
    async def _on_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        # Deleted by hand (or by us): drop its deadline so the table doesn't collect strays.
        if channel.id in self._deadlines:
            await self._clear_deadline(channel.id)

    async def cog_unload(self) -> None:
        await stop_loop(self._cleanup_expired)
        await stop_loop(self._sla_check)

    @commands.Cog.listener("on_message")
    async def _route_ticket_message(self, message: discord.Message) -> None:
        # Same condition the old single on_message used to route to the ticket handler.
        if (
            not message.author.bot
            and message.guild is not None
            and ticket_owner_id(message.channel) is not None
        ):
            await self._on_ticket_message(message)

    async def ensure_tickets_category(self, guild: discord.Guild) -> discord.CategoryChannel | None:
        """The server's tickets category. If it isn't configured (or was deleted), reuse one
        named "🎫 TICKETS" or create it, and save it. None if that fails (no permission)."""
        category = self.bot.tickets_category(guild)
        if category is not None:
            return category
        async with self._provision_locks.setdefault(guild.id, asyncio.Lock()):
            category = self.bot.tickets_category(guild)   # created while we waited
            if category is not None:
                return category
            try:
                category, created = await find_or_create_category(
                    guild, TICKETS_CATEGORY_NAME, known_id=None,
                    overwrites=private_overwrites(guild, self.bot.staff_role(guild)), enforce=False)
            except discord.HTTPException:
                log.exception("Could not find or create a tickets category in %s", guild.name)
                return None
            await self.bot.settings.update(guild.id, tickets_category_id=category.id)
            log.info("%s the tickets category %r in %s", "Created" if created else "Using", category.name, guild.name)
            return category

    def _find_existing_ticket(self, guild: discord.Guild, user_id: int) -> discord.TextChannel | None:
        category = self.bot.tickets_category(guild)
        if category is None:
            return None
        for channel in category.text_channels:
            # Archived tickets don't count, so a user can open a new one while an old one awaits deletion.
            if (
                ticket_owner_id(channel) == user_id
                and self.delete_at(channel) is None
                and channel.id not in self._closing_channels
            ):
                return channel
        return None

    async def _original_question(
        self, interaction: discord.Interaction, source: tuple[int, int] | None = None
    ) -> str | None:
        """Fetch the member message the FAQ answered, for the ticket's welcome card.

        ``source`` is its (channel_id, message_id), carried by the ticket button on a
        private FAQ answer, which can't reply to it. Without it, the button's own message
        is a public reply to the question (older FAQ replies).
        """
        original = None
        if source is not None:
            channel = interaction.guild.get_channel(source[0]) if interaction.guild else None
            message_id = source[1]
        else:
            msg = interaction.message
            if msg is None or msg.reference is None or msg.reference.message_id is None:
                return None
            channel, message_id = interaction.channel, msg.reference.message_id
            original = msg.reference.cached_message
        if original is None and isinstance(channel, discord.abc.Messageable):
            try:
                original = await channel.fetch_message(message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        if original is None or not original.content:
            return None
        text = original.content
        if original.author.id != interaction.user.id:
            text = f"(originally asked by {original.author.display_name})\n{text}"
        return text if len(text) <= 1000 else text[:997] + "..."

    async def open_ticket(self, interaction: discord.Interaction, question: tuple[int, int] | None = None) -> None:
        """``question``: (channel_id, message_id) of the FAQ question, when the button knows it."""
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await _send_ephemeral(interaction, "Tickets can only be opened inside the server.")
            return

        # Channel creation can exceed Discord's 3s response window, so defer first.
        await interaction.response.defer(ephemeral=True, thinking=True)

        lock = self._ticket_locks.setdefault(member.id, asyncio.Lock())
        async with lock:
            existing = self._find_existing_ticket(guild, member.id)
            if existing is not None:
                await interaction.followup.send(
                    f"You already have an open ticket: {existing.mention}", ephemeral=True
                )
                return

            category = await self.ensure_tickets_category(guild)
            if category is None:
                await interaction.followup.send(
                    "I couldn't find or create a tickets category (I need the Manage Channels and "
                    "Manage Roles permissions). Please notify an administrator.",
                    ephemeral=True,
                )
                return
            # No staff role yet (server not set up): the ticket is still created, and
            # administrators can see and handle it.
            staff_role = self.bot.staff_role(guild)
            if staff_role is None:
                log.warning("%s has no staff role; only administrators will see new tickets (run /setup)", guild.name)

            # Discord hard limits: 50 channels per category, 500 per guild. Without this
            # pre-check the create call fails with a generic 400 and users see "try again".
            if len(category.channels) >= CATEGORY_CHANNEL_LIMIT or len(guild.channels) >= GUILD_CHANNEL_LIMIT:
                log.warning("Ticket capacity reached (category=%d, guild=%d)", len(category.channels), len(guild.channels))
                await interaction.followup.send(
                    "All ticket slots are currently full. Please try again later or ping a staff member.",
                    ephemeral=True,
                )
                return

            overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                member: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                ),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, manage_channels=True,
                    read_message_history=True, embed_links=True,
                ),
            }
            if staff_role is not None:
                overwrites[staff_role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )

            question = await self._original_question(interaction, question)
            try:
                channel = await guild.create_text_channel(
                    name=ticket_channel_name(member),
                    category=category,
                    overwrites=overwrites,
                    topic=f"{TICKET_TOPIC_PREFIX}{member.id}",
                    reason=f"Support ticket opened by {member} ({member.id})",
                )
            except discord.Forbidden:
                log.error("Missing Manage Channels / Manage Roles permission to create tickets")
                await interaction.followup.send(
                    "I don't have permission to create ticket channels. Please notify a staff member.",
                    ephemeral=True,
                )
                return
            except discord.HTTPException:
                log.exception("Failed to create ticket channel for %s", member)
                await interaction.followup.send("Couldn't create your ticket. Please try again.", ephemeral=True)
                return

            embed = build_ticket_welcome_embed(member, staff_role, question)

            try:
                # Role pings only notify if the role is mentionable or the bot has "Mention Everyone".
                await channel.send(
                    content=f"{member.mention} {staff_role.mention}" if staff_role else member.mention,
                    embed=embed,
                    view=TicketControlView(self.bot),
                    allowed_mentions=discord.AllowedMentions(users=[member], roles=[staff_role] if staff_role else []),
                )
            except discord.HTTPException:
                log.exception("Failed to send welcome message in %s", channel)

            await interaction.followup.send(f"Your ticket has been created: {channel.mention}", ephemeral=True)
            log.info("Opened ticket #%s for %s", channel.name, member)

    async def _find_welcome_message(self, channel: discord.TextChannel) -> discord.Message | None:
        # The welcome card is the bot's first message in the channel.
        try:
            async for msg in channel.history(limit=5, oldest_first=True):
                if msg.author == self.bot.user and get_ticket_status(msg) is not None:
                    return msg
        except discord.HTTPException:
            log.warning("Could not read history in #%s", channel.name)
        return None

    async def _on_ticket_message(self, message: discord.Message) -> None:
        """First staff reply moves an unclaimed ticket from Awaiting Response to In Progress."""
        channel = message.channel
        assert isinstance(channel, discord.TextChannel)
        if channel.id in self._status_settled or channel.id in self._closing_channels:
            return
        if message.author.id == ticket_owner_id(channel) or not self.bot.is_staff(message.author):
            return

        async with self._status_locks.setdefault(channel.id, asyncio.Lock()):
            if channel.id in self._status_settled:
                return
            welcome = await self._find_welcome_message(channel)
            # Settle even on failure, so a ticket without a status card isn't re-scanned on every message.
            self._status_settled.add(channel.id)
            if welcome is None or get_ticket_status(welcome) != STATUS_AWAITING:
                return
            embed = with_ticket_status(welcome, STATUS_IN_PROGRESS)
            try:
                await welcome.edit(embed=embed)  # omitting view= keeps the existing buttons
            except discord.HTTPException:
                log.warning("Failed to update status in #%s", channel.name)

    async def claim_ticket(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        if ticket_owner_id(channel) is None or not isinstance(channel, discord.TextChannel):
            await _send_ephemeral(interaction, "This button only works inside a ticket channel.")
            return
        if not self.bot.is_staff(interaction.user):
            await _send_ephemeral(interaction, "Only staff can claim tickets.")
            return
        if interaction.message is None:
            await _send_ephemeral(interaction, "Couldn't find the ticket card to update.")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self._status_locks.setdefault(channel.id, asyncio.Lock()):
            # Re-fetch inside the lock: interaction.message is a snapshot from click
            # time and may predate another staff member's claim.
            try:
                welcome = await channel.fetch_message(interaction.message.id)
            except discord.HTTPException:
                await interaction.followup.send("Couldn't load the ticket card. Please try again.", ephemeral=True)
                return

            # Checked inside the status lock: archiving rewrites the status card under it.
            if self.delete_at(channel) is not None:
                await interaction.followup.send("This ticket is closed. Re-open it before claiming.", ephemeral=True)
                return
            status = get_ticket_status(welcome)
            if status is None:
                await interaction.followup.send("This ticket card has no status to update.", ephemeral=True)
                return
            if "Claimed by" in status:
                await interaction.followup.send(f"This ticket is already {status}.", ephemeral=True)
                return

            embed = with_ticket_status(welcome, f"{STATUS_IN_PROGRESS} (Claimed by {interaction.user.mention})")
            try:
                await welcome.edit(embed=embed, view=TicketControlView(self.bot, claimed_by=interaction.user))
            except discord.HTTPException:
                log.exception("Failed to mark #%s as claimed", channel.name)
                await interaction.followup.send("Couldn't claim this ticket. Please try again.", ephemeral=True)
                return
            self._status_settled.add(channel.id)

        # Public notice. Only the owner is pinged; the claiming staff member already knows.
        owner_id = ticket_owner_id(channel)
        try:
            await channel.send(
                f"<@{owner_id}> 👋 {interaction.user.mention} {CLAIM_NOTICE_MARKER} "
                "and will be assisting you shortly!",
                allowed_mentions=discord.AllowedMentions(users=[discord.Object(owner_id)]),
            )
        except discord.HTTPException:
            log.warning("Could not post claim notice in #%s", channel.name)

        await interaction.followup.send("✅ You've claimed this ticket.", ephemeral=True)
        log.info("Ticket #%s claimed by %s", channel.name, interaction.user)

    def _lifecycle_lock(self, channel_id: int) -> asyncio.Lock:
        return self._lifecycle_locks.setdefault(channel_id, asyncio.Lock())

    def _forget_channel(self, channel_id: int) -> None:
        self._closing_channels.discard(channel_id)
        self._status_settled.discard(channel_id)
        self._status_locks.pop(channel_id, None)
        self._lifecycle_locks.pop(channel_id, None)

    async def _check_can_close(self, interaction: discord.Interaction) -> discord.TextChannel | None:
        """Validate a close attempt, replying with the reason if it isn't allowed."""
        channel = interaction.channel
        owner_id = ticket_owner_id(channel)
        if owner_id is None or not isinstance(channel, discord.TextChannel):
            await _send_ephemeral(interaction, "This command can only be used inside a ticket channel.")
            return None
        if interaction.user.id != owner_id and not self.bot.is_staff(interaction.user):
            await _send_ephemeral(interaction, "Only staff or the ticket creator can close this ticket.")
            return None
        if channel.id in self._closing_channels:
            await _send_ephemeral(interaction, "This ticket is being deleted.")
            return None
        if self.delete_at(channel) is not None:
            await _send_ephemeral(interaction, "This ticket is already closed. Use the buttons on the archive message.")
            return None
        return channel

    async def close_ticket(self, interaction: discord.Interaction, reason: str | None = None) -> None:
        """Entry point for the Close button and /close [reason]. Staff who gave a reason close
        straight away, other staff get the reason modal, and owners get a confirmation."""
        if await self._check_can_close(interaction) is None:
            return
        # Staff check comes first so a staff member closing their own ticket still gives a reason.
        if self.bot.is_staff(interaction.user):
            reason = _INVISIBLE_CHARS.sub("", reason or "").strip()[:CLOSE_REASON_MAX_LENGTH]
            if reason:
                await self.archive_ticket(interaction, reason)
            else:
                await interaction.response.send_modal(CloseReasonModal(self.bot))
        else:
            await interaction.response.send_message(
                "Close this ticket? You'll be able to read it but not send messages, "
                "and it will be deleted automatically after 48 hours.",
                view=ConfirmCloseView(self.bot),
                ephemeral=True,
            )

    def _owner_overwrites(
        self, channel: discord.TextChannel, owner_id: int, *, can_send: bool
    ) -> dict[discord.Role | discord.Member | discord.Object, discord.PermissionOverwrite]:
        """The channel's overwrites with only the owner's send permission changed."""
        overwrites = dict(channel.overwrites)
        # Reuse the existing key (Member or Object) so we update rather than duplicate it.
        # If the owner has no overwrite (e.g. it was removed by hand), target them by ID.
        key = next((k for k in overwrites if k.id == owner_id), None) or discord.Object(owner_id, type=discord.Member)
        overwrite = overwrites.get(key, discord.PermissionOverwrite())
        overwrite.update(view_channel=True, read_message_history=True, send_messages=can_send)
        overwrites[key] = overwrite
        return overwrites

    async def _edit_ticket_channel(
        self,
        channel: discord.TextChannel,
        *,
        overwrites,
        reason: str,
        category: discord.CategoryChannel | None = None,
        name: str | None = None,
        topic: str | None = None,
    ) -> str | None:
        """Re-permission and (optionally) move in ONE request. Returns an error message on failure.

        Closing and re-opening never pass ``name`` or ``topic``: Discord allows only 2
        name/topic edits per channel every 10 minutes, while permission and category
        changes aren't limited that way. (Only re-opening a ticket closed by an older
        version passes them, once, to undo that version's rename and topic marker.)
        Moving without ``position`` stays in the same PATCH, and sync_permissions defaults
        to False, so the new category's permissions never overwrite the owner lock.
        """
        kwargs = {"overwrites": overwrites, "reason": reason[:512]}
        if name is not None and name != channel.name:
            kwargs["name"] = name
        if topic is not None and topic != channel.topic:
            kwargs["topic"] = topic
        if category is not None and category.id != channel.category_id:
            kwargs["category"] = category
        try:
            await asyncio.wait_for(channel.edit(**kwargs), timeout=CHANNEL_EDIT_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            log.warning("Editing #%s timed out", channel.name)
            return "Discord is taking too long to update this channel. Please try again in a moment."
        except discord.Forbidden:
            log.error("Missing Manage Channels / Manage Roles permission to edit #%s", channel.name)
            return "I don't have permission to edit or move this channel. Please notify an administrator."
        except discord.HTTPException:
            log.exception("Failed to edit #%s", channel.name)
            return "Couldn't update this channel. Please try again."
        # The cached channel only updates when the gateway event arrives; set the fields
        # now so a follow-up click can't read a stale topic/category and act twice.
        channel.name, channel.topic = kwargs.get("name", channel.name), kwargs.get("topic", channel.topic)
        if "category" in kwargs:
            channel.category_id = category.id
        return None

    async def _update_welcome_status(self, channel: discord.TextChannel, transform) -> None:
        """Rewrite the welcome card's status via ``transform(old) -> new``. Failures are logged, never raised."""
        async with self._status_locks.setdefault(channel.id, asyncio.Lock()):
            welcome = await self._find_welcome_message(channel)
            status = get_ticket_status(welcome) if welcome else None
            if status is None:
                return  # pre-status ticket, or the card was deleted
            new_status = transform(status)
            if new_status == status:
                return
            try:
                await welcome.edit(embed=with_ticket_status(welcome, new_status))  # keeps the buttons
            except discord.HTTPException:
                log.warning("Failed to update welcome card status in #%s", channel.name)

    async def archive_ticket(self, interaction: discord.Interaction, reason: str) -> None:
        """Stage 1: lock the owner out of sending, move to the archive category (if
        configured), schedule deletion in 48h, then DM the owner a transcript. The channel
        keeps its name and topic, so this never hits Discord's rename limit."""
        # Re-checked: the ticket may have been closed while the modal/confirmation was open.
        channel = await self._check_can_close(interaction)
        if channel is None:
            return
        if not interaction.response.is_done():  # modal path; the owner path already edited its prompt
            await interaction.response.defer(ephemeral=True, thinking=True)

        async with self._lifecycle_lock(channel.id):
            if self.delete_at(channel) is not None or channel.id in self._closing_channels:
                await interaction.followup.send("This ticket is already closed.", ephemeral=True)
                return
            owner_id = ticket_owner_id(channel)
            delete_at = int(time.time()) + ARCHIVE_RETENTION_SECONDS

            archive_category = self.bot.archive_category(channel.guild)   # None: archive in place
            if archive_category is not None and len(archive_category.channels) >= CATEGORY_CHANNEL_LIMIT:
                log.warning("Archive category is full; archiving #%s in place", channel.name)
                archive_category = None

            error = await self._edit_ticket_channel(
                channel,
                overwrites=self._owner_overwrites(channel, owner_id, can_send=False),
                category=archive_category,
                reason=f"Ticket closed by {interaction.user} ({interaction.user.id}): {reason}",
            )
            if error:
                await interaction.followup.send(error, ephemeral=True)
                return
            await self._set_deadline(channel.id, delete_at)
            try:
                await channel.send(
                    embed=build_archive_embed(interaction.user, delete_at, reason),
                    view=ClosedTicketView(self.bot),
                )
            except discord.HTTPException:
                # The channel is still archived and the cleanup loop will still delete it.
                log.exception("Failed to post archive message in #%s", channel.name)
            await self._update_welcome_status(channel, archived_status)

        await interaction.followup.send(f"🔒 Ticket closed. It will be deleted <t:{delete_at}:R>.", ephemeral=True)
        log.info("Archived ticket #%s (by %s): %s", channel.name, interaction.user, reason)

        # Outside the lock so slow analytics or a failing DM never hold up Re-open / Delete Now.
        # The history is read once and shared by the metrics record and the DM.
        try:
            data = await asyncio.wait_for(
                self.bot.transcripts.gather(channel, deleted_by=None, deleted=False), timeout=TRANSCRIPT_TIMEOUT_SECONDS
            )
        except Exception:
            log.exception("Could not read #%s for metrics/DM after closing", channel.name)
            return
        await self.bot.transcripts.record_metrics(channel, data)
        await self.bot.transcripts.store(channel, data)
        try:
            await asyncio.wait_for(
                self.bot.transcripts.dm_owner(channel, data, interaction.user, reason, delete_at),
                timeout=TRANSCRIPT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            log.warning("DM transcript for #%s timed out", channel.name)
        except Exception:
            log.exception("DM transcript for #%s failed", channel.name)

    async def reopen_ticket(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        owner_id = ticket_owner_id(channel)
        if owner_id is None or not isinstance(channel, discord.TextChannel):
            await _send_ephemeral(interaction, "This button only works inside a ticket channel.")
            return
        if interaction.user.id != owner_id and not self.bot.is_staff(interaction.user):
            await _send_ephemeral(interaction, "Only staff or the ticket creator can re-open this ticket.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        async with self._lifecycle_lock(channel.id):
            if channel.id in self._closing_channels:
                await interaction.followup.send("This ticket is being deleted.", ephemeral=True)
                return
            if self.delete_at(channel) is None:
                await interaction.followup.send("This ticket is already open.", ephemeral=True)
                return

            # Move back only if it was relocated; a ticket archived in place stays put.
            # (If the tickets category was deleted meanwhile, it's recreated.)
            tickets_category = None
            current = self.bot.tickets_category(channel.guild)
            if current is None or channel.category_id != current.id:
                tickets_category = await self.ensure_tickets_category(channel.guild)
                if tickets_category is None:
                    await interaction.followup.send(
                        "I couldn't find or create the tickets category, so I can't move this ticket back. "
                        "Please notify an administrator.",
                        ephemeral=True,
                    )
                    return
                if tickets_category.id == channel.category_id:
                    tickets_category = None
                elif len(tickets_category.channels) >= CATEGORY_CHANNEL_LIMIT:
                    await interaction.followup.send(
                        "The tickets category is full right now, so this ticket can't be re-opened. "
                        "Please try again later or open a new ticket.",
                        ephemeral=True,
                    )
                    return

            # Closed by an older version: undo its "closed-" rename and topic marker. This is
            # the only name/topic edit left, and it happens at most once per such ticket.
            legacy = ticket_delete_at(channel) is not None
            error = await self._edit_ticket_channel(
                channel,
                overwrites=self._owner_overwrites(channel, owner_id, can_send=True),
                category=tickets_category,
                name=swap_name_prefix(channel.name, CLOSED_PREFIX, OPEN_PREFIX) if legacy else None,
                topic=ticket_topic(owner_id) if legacy else None,
                reason=f"Ticket re-opened by {interaction.user} ({interaction.user.id})",
            )
            if error:
                await interaction.followup.send(error, ephemeral=True)
                return
            await self._clear_deadline(channel.id)
            if interaction.message is not None:
                try:
                    await interaction.message.delete()
                except discord.HTTPException:
                    log.warning("Could not delete archive message in #%s", channel.name)
            try:
                await channel.send(embed=build_reopen_embed(interaction.user))
            except discord.HTTPException:
                log.warning("Could not post re-open notice in #%s", channel.name)
            await self._update_welcome_status(channel, restored_status)
            # Let the first staff reply move a restored "Awaiting Response" to "In Progress" again.
            self._status_settled.discard(channel.id)
            self._sla_done.discard(channel.id)

        await interaction.followup.send("🔓 Ticket re-opened.", ephemeral=True)
        log.info("Re-opened ticket #%s (by %s)", channel.name, interaction.user)

    async def delete_ticket_now(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        if ticket_owner_id(channel) is None or not isinstance(channel, discord.TextChannel):
            await _send_ephemeral(interaction, "This button only works inside a ticket channel.")
            return
        if not self.bot.is_staff(interaction.user):
            await _send_ephemeral(interaction, "Only staff can delete tickets.")
            return
        if channel.id in self._closing_channels:
            await _send_ephemeral(interaction, "This ticket is already being deleted.")
            return
        # Not waiting on the lock: a slow re-open could hold it past the 3-second
        # interaction deadline.
        if self._lifecycle_lock(channel.id).locked():
            await _send_ephemeral(interaction, "This ticket is being updated. Please try again in a moment.")
            return
        if self.delete_at(channel) is None:
            await _send_ephemeral(interaction, "This ticket has been re-opened. Close it first.")
            return

        # No await between the checks above and this add, so nothing can slip in between.
        self._closing_channels.add(channel.id)
        deadline = int(time.time()) + CLOSE_COUNTDOWN_SECONDS
        try:
            await interaction.response.send_message(embed=build_delete_embed(interaction.user, deadline))
        except discord.HTTPException:
            log.warning("Could not post delete countdown in #%s; deleting anyway", channel.name)
        await self._delete_ticket(
            channel,
            deleted_by=interaction.user,
            audit_reason=f"Ticket deleted by {interaction.user} ({interaction.user.id})",
            countdown=CLOSE_COUNTDOWN_SECONDS,
            interaction=interaction,
        )

    async def _delete_ticket(
        self,
        channel: discord.TextChannel,
        *,
        deleted_by: discord.abc.User | None,
        audit_reason: str,
        countdown: float,
        interaction: discord.Interaction | None = None,
    ) -> bool:
        """Post the transcript (bounded by a timeout), wait out the countdown, delete.

        Returns True if the channel is gone afterwards. Caller marks the channel closing.
        """
        self._closing_channels.add(channel.id)
        # try/finally guarantees the channel is never stuck in _closing_channels,
        # which would otherwise block every future delete/re-open attempt.
        try:
            started = time.monotonic()
            # One history read feeds both the final metrics row and the log-channel summary.
            try:
                data = await asyncio.wait_for(
                    self.bot.transcripts.gather(channel, deleted_by=deleted_by, deleted=True),
                    timeout=TRANSCRIPT_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                log.warning("Reading #%s timed out; deleting without transcript/metrics", channel.name)
                data = None
            except Exception:
                log.exception("Reading #%s failed; deleting without transcript/metrics", channel.name)
                data = None
            if data is not None:
                await self.bot.transcripts.record_metrics(channel, data)
                await self.bot.transcripts.store(channel, data)
                if self.bot.transcript_channel(channel.guild) is not None:
                    try:
                        await asyncio.wait_for(
                            self.bot.transcripts.post_to_log(channel, deleted_by, data), timeout=TRANSCRIPT_TIMEOUT_SECONDS
                        )
                    except asyncio.TimeoutError:
                        log.warning("Transcript for #%s timed out; deleting without it", channel.name)
                    except Exception:
                        log.exception("Transcript for #%s failed; deleting without it", channel.name)
            await asyncio.sleep(max(0.0, countdown - (time.monotonic() - started)))
            await channel.delete(reason=audit_reason[:512])
            log.info("Deleted ticket #%s (%s)", channel.name, audit_reason)
            await self._clear_deadline(channel.id)
            return True
        except discord.NotFound:
            await self._clear_deadline(channel.id)
            return True  # already deleted by someone else
        except discord.HTTPException:
            log.exception("Failed to delete ticket channel %s", channel)
            if interaction is not None:
                await _send_ephemeral(interaction, "I couldn't delete this channel. Please check my permissions.")
            return False
        finally:
            self._forget_channel(channel.id)

    @tasks.loop(minutes=CLEANUP_INTERVAL_MINUTES)
    async def _cleanup_expired(self) -> None:
        # Runs once immediately on start, so tickets that expired while the bot was
        # offline are removed right after a restart. Deadlines live in the database,
        # so no state is lost across restarts.
        guilds = self.bot.managed_guilds()
        # Archived tickets usually live in the archive category, but ones archived in place
        # (no archive category, or it was full) are still in the tickets category.
        candidates: dict[int, discord.TextChannel] = {}
        for guild in guilds:
            for category in (self.bot.tickets_category(guild), self.bot.archive_category(guild)):
                if category is not None:
                    candidates.update((ch.id, ch) for ch in category.text_channels)
        now = time.time()
        # Deadlines of channels that no longer exist (deleted while the bot was offline).
        # Skipped during a Discord outage, when a server's channels are briefly missing.
        if not any(getattr(g, "unavailable", False) for g in guilds):
            for channel_id in [cid for cid in self._deadlines if cid not in candidates]:
                if self.bot.get_channel(channel_id) is None:
                    await self._clear_deadline(channel_id)
        for channel in list(candidates.values()):
            delete_at = self.delete_at(channel)
            if delete_at is None or now < delete_at:
                continue
            # Skip anything mid-action (being re-opened / deleted); the next run retries.
            if channel.id in self._closing_channels or self._lifecycle_lock(channel.id).locked():
                continue
            self._closing_channels.add(channel.id)
            try:
                await self._delete_ticket(
                    channel, deleted_by=None, audit_reason="48-hour post-closure retention expired", countdown=0
                )
            except Exception:
                # Never let one channel's failure stop the loop (an unhandled error would end it).
                log.exception("Cleanup failed for #%s", channel.name)

    @_cleanup_expired.before_loop
    async def _before_cleanup(self) -> None:
        await self.bot.wait_until_ready()

    async def _check_sla(self, channel: discord.TextChannel, staff_role: discord.Role | None, now: dt.datetime) -> None:
        owner_id = ticket_owner_id(channel)
        # Open tickets only; cheap in-memory checks first so settled tickets cost nothing.
        if owner_id is None or self.delete_at(channel) is not None:
            return
        if channel.id in self._sla_done or channel.id in self._closing_channels:
            return
        if (now - channel.created_at).total_seconds() < SLA_THRESHOLD_SECONDS:
            return
        if await self.bot.db.sla_alerted(channel.id):
            self._sla_done.add(channel.id)
            return

        messages = [m async for m in channel.history(limit=50, oldest_first=True)]
        welcome = next((m for m in messages if m.author == self.bot.user and get_ticket_status(m) is not None), None)
        if welcome is None or get_ticket_status(welcome) != STATUS_AWAITING:
            self._sla_done.add(channel.id)  # claimed, in progress, or no card to judge by
            return
        if self.bot.transcripts.first_response_at(messages, owner_id, now) is not None:
            # Staff did reply, but the card missed it (e.g. the bot was offline). Fix the card, don't alert.
            self._sla_done.add(channel.id)
            await self._update_welcome_status(
                channel, lambda s: STATUS_IN_PROGRESS if s == STATUS_AWAITING else s
            )
            return

        warning = (
            "⚠️ **SLA Warning:** This ticket has been waiting for staff response for over "
            f"{SLA_THRESHOLD_SECONDS // 60} minutes!" + (f" {staff_role.mention}" if staff_role else "")
        )
        # Role pings only notify if the role is mentionable or the bot has "Mention Everyone".
        mentions = discord.AllowedMentions(roles=[staff_role] if staff_role else [])
        target = self.bot.sla_alert_channel(channel.guild)
        sent = False
        if target is not None:
            try:
                await target.send(f"{channel.mention} — {warning}", allowed_mentions=mentions)
                sent = True
            except discord.HTTPException:
                log.warning("Could not post to the SLA alert channel #%s; alerting in the ticket instead", target.name)
        if not sent:
            # A failure here propagates: the loop logs it and the next run retries.
            await channel.send(warning, allowed_mentions=mentions)
        # Marked after sending: a failed send is retried next run instead of being lost.
        # (The loop never overlaps itself, so this can't double-send.)
        await self.bot.db.mark_sla_alerted(channel.id)
        self._sla_done.add(channel.id)
        log.info("SLA warning sent for #%s", channel.name)

    @tasks.loop(minutes=SLA_CHECK_INTERVAL_MINUTES)
    async def _sla_check(self) -> None:
        # The once-per-ticket guarantee lives in the database, so without it we'd risk
        # re-pinging staff after every restart. Better to send nothing.
        if self.bot.db is None:
            return
        now = discord.utils.utcnow()
        for guild in self.bot.managed_guilds():
            category = self.bot.tickets_category(guild)
            if category is None:
                continue
            staff_role = self.bot.staff_role(guild)   # None: alert without a ping
            for channel in list(category.text_channels):
                try:
                    await self._check_sla(channel, staff_role, now)
                except Exception:
                    # One bad channel must never end the loop.
                    log.exception("SLA check failed for #%s", channel.name)

    @_sla_check.before_loop
    async def _before_sla(self) -> None:
        await self.bot.wait_until_ready()

    def _purge_targets(self, guild: discord.Guild, category_type: str) -> list[discord.TextChannel]:
        """Ticket channels to purge. Active = open tickets, Archived = closed ones, wherever they live."""
        categories = [c for c in (self.bot.tickets_category(guild), self.bot.archive_category(guild)) if c is not None]
        targets: dict[int, discord.TextChannel] = {}
        for category in categories:
            for channel in category.text_channels:
                # Both checks: the name pattern the command promises, and the ticket topic,
                # so a hand-made channel that happens to be called "ticket-rules" is safe.
                # (closed-* names come from older versions, which renamed closed tickets.)
                if ticket_owner_id(channel) is None or not channel.name.startswith((OPEN_PREFIX, CLOSED_PREFIX)):
                    continue
                archived = self.delete_at(channel) is not None
                if (category_type == "Active" and archived) or (category_type == "Archived" and not archived):
                    continue
                targets[channel.id] = channel
        return list(targets.values())

    async def run_purge(self, interaction: discord.Interaction, category_type: str) -> None:
        # Re-checked: permissions can change while the confirmation is open.
        if interaction.guild is None or not self.bot.is_admin(interaction.user):
            await interaction.response.edit_message(content="Only administrators can purge tickets.", embed=None, view=None)
            return
        if self._purge_running:
            await interaction.response.edit_message(content="A purge is already running.", embed=None, view=None)
            return
        self._purge_running = True
        try:
            await interaction.response.edit_message(
                content="🧹 Purging tickets… this can take a while if there are many.", embed=None, view=None
            )
            deleted = failed = skipped = 0
            # Recomputed: tickets may have been opened, closed or deleted since the preview.
            for channel in self._purge_targets(interaction.guild, category_type):
                # Mid-action (being deleted / re-opened / archived): leave it alone.
                if channel.id in self._closing_channels or self._lifecycle_lock(channel.id).locked():
                    skipped += 1
                    continue
                self._closing_channels.add(channel.id)
                try:
                    ok = await self._delete_ticket(
                        channel,
                        deleted_by=interaction.user,
                        audit_reason=f"Emergency ticket purge by {interaction.user} ({interaction.user.id})",
                        countdown=0,
                    )
                except Exception:
                    log.exception("Purge failed for #%s", channel.name)
                    self._forget_channel(channel.id)
                    ok = False
                deleted += ok
                failed += not ok
            log.warning(
                "Ticket purge (%s) by %s: %d deleted, %d failed, %d skipped",
                category_type, interaction.user, deleted, failed, skipped,
            )
            kind = "" if category_type == "Both" else f"{category_type.lower()} "
            report = f"🧹 Purge complete: deleted **{deleted}** {kind}ticket channel(s)."
            if failed:
                report += f"\n❌ {failed} could not be deleted (check my permissions)."
            if skipped:
                report += f"\n⏭️ {skipped} skipped because they were already being closed or deleted."
            try:
                await interaction.followup.send(report, ephemeral=True)
            except discord.HTTPException:
                log.warning("Could not send purge report to %s", interaction.user)
        finally:
            self._purge_running = False
