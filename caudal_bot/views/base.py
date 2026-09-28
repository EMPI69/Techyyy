"""Base class for the bot's persistent views."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from ..common import _send_ephemeral

if TYPE_CHECKING:
    from ..main import FAQBot

log = logging.getLogger("faq_bot")


class _BaseView(discord.ui.View):
    def __init__(self, bot: FAQBot) -> None:
        super().__init__(timeout=None)  # timeout=None is required for persistence
        self.bot = bot

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[discord.ui.View],
    ) -> None:
        log.exception("Error in view item %r", item.custom_id if hasattr(item, "custom_id") else item, exc_info=error)
        await _send_ephemeral(interaction, "Something went wrong. Please try again or contact a staff member.")
