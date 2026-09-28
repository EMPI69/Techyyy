"""Ticket buttons and dialogs: Claim/Close (ticket:claim, ticket:close), the close-reason
modal and owner confirmation, the archive card's Re-open/Delete Now (ticket:reopen,
ticket:delete_now), and the /ticket-purge confirmation.

Views only collect input; each button hands off to the Tickets cog (bot.tickets).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from ..common import (
    CLOSE_REASON_MAX_LENGTH, DEFAULT_CLOSE_REASON, OWNER_CLOSE_REASON, _INVISIBLE_CHARS, _send_ephemeral,
)
from .base import _BaseView

if TYPE_CHECKING:
    from ..main import FAQBot

log = logging.getLogger("faq_bot")


class TicketControlView(_BaseView):
    def __init__(self, bot: FAQBot, claimed_by: discord.abc.User | None = None) -> None:
        super().__init__(bot)
        if claimed_by is not None:
            # Sent in place of the original view once claimed. The custom_id stays
            # the same, so the view registered in setup_hook still routes clicks.
            self.claim.disabled = True
            self.claim.label = f"Claimed by {claimed_by.display_name}"[:80]

    @discord.ui.button(
        label="Claim Ticket",
        style=discord.ButtonStyle.secondary,
        emoji="🙋‍♂️",
        custom_id="ticket:claim",
    )
    async def claim(self, interaction: discord.Interaction, _: discord.ui.Button[TicketControlView]) -> None:
        await self.bot.tickets.claim_ticket(interaction)

    @discord.ui.button(
        label="Close Ticket",
        style=discord.ButtonStyle.danger,
        emoji="🔒",
        custom_id="ticket:close",
    )
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button[TicketControlView]) -> None:
        await self.bot.tickets.close_ticket(interaction)


class CloseReasonModal(discord.ui.Modal, title="Close Ticket"):
    # Modals are one-shot popups, not persistent views, so no custom_id is needed.
    reason = discord.ui.TextInput(
        label="Reason for closing",
        placeholder=DEFAULT_CLOSE_REASON,
        required=False,
        max_length=CLOSE_REASON_MAX_LENGTH,
    )

    def __init__(self, bot: FAQBot) -> None:
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        reason = _INVISIBLE_CHARS.sub("", self.reason.value).strip() or DEFAULT_CLOSE_REASON
        await self.bot.tickets.archive_ticket(interaction, reason)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Error in close modal", exc_info=error)
        await _send_ephemeral(interaction, "Something went wrong while closing the ticket.")


class ConfirmCloseView(discord.ui.View):
    """Ephemeral yes/no for the ticket owner. Short-lived by design, so not persistent."""

    def __init__(self, bot: FAQBot) -> None:
        super().__init__(timeout=60)
        self.bot = bot

    @discord.ui.button(label="Yes, close it", style=discord.ButtonStyle.danger, emoji="🔒")
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button[ConfirmCloseView]) -> None:
        self.stop()
        await interaction.response.edit_message(content="Closing your ticket…", view=None)
        await self.bot.tickets.archive_ticket(interaction, OWNER_CLOSE_REASON)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button[ConfirmCloseView]) -> None:
        self.stop()
        await interaction.response.edit_message(content="Ticket left open.", view=None)


class ClosedTicketView(_BaseView):
    """Buttons on the archive card. Persistent, so they work for the full 48 hours across restarts."""

    @discord.ui.button(
        label="Re-open Ticket",
        style=discord.ButtonStyle.secondary,
        emoji="🔓",
        custom_id="ticket:reopen",
    )
    async def reopen(self, interaction: discord.Interaction, _: discord.ui.Button[ClosedTicketView]) -> None:
        await self.bot.tickets.reopen_ticket(interaction)

    @discord.ui.button(
        label="Delete Now",
        style=discord.ButtonStyle.danger,
        emoji="🗑️",
        custom_id="ticket:delete_now",
    )
    async def delete_now(self, interaction: discord.Interaction, _: discord.ui.Button[ClosedTicketView]) -> None:
        await self.bot.tickets.delete_ticket_now(interaction)


class PurgeConfirmView(discord.ui.View):
    """Ephemeral second step for /ticket-purge. Short-lived, so not persistent."""

    def __init__(self, bot: FAQBot, category_type: str) -> None:
        super().__init__(timeout=60)
        self.bot = bot
        self.category_type = category_type

    @discord.ui.button(label="⚠️ Confirm Purge", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button[PurgeConfirmView]) -> None:
        self.stop()
        await self.bot.tickets.run_purge(interaction, self.category_type)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button[PurgeConfirmView]) -> None:
        self.stop()
        await interaction.response.edit_message(content="Purge cancelled. Nothing was deleted.", embed=None, view=None)
