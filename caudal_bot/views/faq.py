"""FAQ buttons.

- "Not helpful? Open a Ticket" (faq:open_ticket): persistent, on FAQ answers.
- "Show answer" (faq:show:<entry>): on the short public prompt the bot posts when a
  message matches an FAQ. Clicking it shows the answer privately (ephemeral), since
  Discord only allows ephemeral messages in response to an interaction.
- The ticket button on that private answer (faq:ticket:<channel>:<message>) remembers
  which message asked the question, so the ticket's welcome card can quote it.

The last two are DynamicItems: registered once (main.setup_hook), they handle clicks
on any message carrying a matching custom_id, including after a restart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .base import _BaseView

if TYPE_CHECKING:
    from ..main import FAQBot

TICKET_LABEL, TICKET_EMOJI = "Not helpful? Open a Ticket", "🎫"
# How long the in-memory views below stay attached to their messages. Clicks after
# that are still answered, by the registered DynamicItems; this only bounds memory.
VIEW_LIFETIME_SECONDS = 15 * 60


class OpenTicketView(_BaseView):
    @discord.ui.button(
        label=TICKET_LABEL,
        style=discord.ButtonStyle.secondary,
        emoji=TICKET_EMOJI,
        custom_id="faq:open_ticket",
    )
    async def open_ticket(self, interaction: discord.Interaction, _: discord.ui.Button[OpenTicketView]) -> None:
        await self.bot.tickets.open_ticket(interaction)


class ShowAnswerButton(discord.ui.DynamicItem[discord.ui.Button], template=r"faq:show:(?P<index>[0-9]+)"):
    def __init__(self, index: int) -> None:
        super().__init__(discord.ui.Button(
            label="Show answer", emoji="📘", style=discord.ButtonStyle.primary, custom_id=f"faq:show:{index}"))
        self.index = index

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match) -> ShowAnswerButton:
        return cls(int(match["index"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.client.faq.show_answer(interaction, self.index)


class QuestionTicketButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"faq:ticket:(?P<channel>[0-9]+):(?P<message>[0-9]+)"
):
    def __init__(self, channel_id: int, message_id: int) -> None:
        super().__init__(discord.ui.Button(
            label=TICKET_LABEL, emoji=TICKET_EMOJI, style=discord.ButtonStyle.secondary,
            custom_id=f"faq:ticket:{channel_id}:{message_id}"))
        self.source = (channel_id, message_id)

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match
    ) -> QuestionTicketButton:
        return cls(int(match["channel"]), int(match["message"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.client.tickets.open_ticket(interaction, question=self.source)


def prompt_view(index: int) -> discord.ui.View:
    view = discord.ui.View(timeout=VIEW_LIFETIME_SECONDS)
    view.add_item(ShowAnswerButton(index))
    return view


def answer_view(bot: FAQBot, question: tuple[int, int] | None) -> discord.ui.View:
    """The ticket button under a private answer, quoting ``question`` if known."""
    if question is None:
        view = OpenTicketView(bot)
        view.timeout = VIEW_LIFETIME_SECONDS   # the registered persistent view takes over after this
        return view
    view = discord.ui.View(timeout=VIEW_LIFETIME_SECONDS)
    view.add_item(QuestionTicketButton(*question))
    return view
