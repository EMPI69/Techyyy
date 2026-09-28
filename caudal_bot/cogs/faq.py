"""FAQ auto-responder: keyword matching, per-channel cooldowns, and private answers.

A message that matches an FAQ gets a one-line public prompt with a "Show answer" button,
which deletes itself after PROMPT_LIFETIME_SECONDS. The answer itself (and the ticket
button) is only ever shown privately, as an ephemeral response to that button or to /faq.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from ..common import COLOR_FAQ, _blockquote, _send_ephemeral, ticket_owner_id
from ..views.faq import answer_view, prompt_view

if TYPE_CHECKING:
    from ..main import FAQBot

log = logging.getLogger("faq_bot")


@dataclass(frozen=True)
class FAQEntry:
    keywords: tuple[str, ...]
    title: str
    answer: str


# HOW TO ADD MORE FAQ ENTRIES
#
#     FAQEntry(
#         keywords=("refund", "money back"),   # any of these triggers the answer
#         title="💸 Refund Policy",            # embed title, shown as written
#         answer="We offer refunds within 14 days of purchase. ...",
#     ),
#
# Notes:
#   * Matching is case-insensitive and on whole words, so "login" matches
#     "How do I login?" but not "logins". Add variants explicitly
#     (e.g. "log in", "signin", "sign in") if you want them to match too.
#   * Multi-word keywords ("time doctor login") tolerate any whitespace between words.
#   * The MOST SPECIFIC matching keyword wins, across all entries: more words first,
#     then the longer keyword. So "time doctor login" answers with Time Doctor even
#     though Platform Logins has "login", and "verification code" beats "verification".
#     Only a tie falls back to the order below (earlier wins).
#   * Embed descriptions support Markdown and are limited to 4096 characters.
FAQ_ENTRIES: list[FAQEntry] = [
    FAQEntry(
        keywords=("login", "log in", "signin", "sign in", "credentials", "password", "account"),
        title="🔐 Platform Logins & Credentials",
        answer=(
            "We use two main platforms requiring login:\n\n"
            "**1. Snorkel Platform (Tasks):**\n"
            "• Portal: https://experts.snorkel-ai.com/home\n"
            "• Use the credentials sent in your onboarding email/message.\n"
            "• Requires the Caudal AI Auth Codes Chrome extension for 2FA.\n\n"
            "**2. Time Doctor (Hour Tracking):**\n"
            "• Credentials and setup instructions are provided separately via Discord.\n\n"
            'Need specific help? Ask about **"Snorkel"** or **"Time Doctor"**, or click below to open a ticket!'
        ),
    ),
    FAQEntry(
        keywords=("snorkel", "snorkel login", "snorkel ai", "tasks portal", "task dashboard"),
        title="🌐 Snorkel Platform Access",
        answer=(
            "Access your task dashboard here:\n"
            "🔗 **Link:** https://experts.snorkel-ai.com/home\n\n"
            "Log in using the credentials provided in your onboarding email/message. "
            "When prompted for verification, use the **Caudal AI Auth Codes** extension."
        ),
    ),
    FAQEntry(
        keywords=("time doctor", "timedoctor", "td", "time doctor login", "tracking", "work hours", "track hours"),
        title="⏱️ Time Doctor Setup & Credentials",
        answer=(
            "All working hours must be tracked using **Time Doctor**.\n\n"
            "Our team will provide your Time Doctor login credentials, guide you through installation, "
            "and explain usage directly here in Discord. If you haven't received your credentials yet, "
            "open a ticket below!"
        ),
    ),
    FAQEntry(
        keywords=("role", "verify", "verification", "how to begin", "welcome", "project role"),
        title="🏷️ Getting Started & Discord Roles",
        answer=(
            "To get verified and start working:\n"
            "1. Check the **#how-to-begin** channel for the onboarding walkthrough.\n"
            "2. Head to the **#welcome** channel and request your specific project role to access your "
            "assigned channels and project updates."
        ),
    ),
    FAQEntry(
        keywords=("extension", "auth code", "auth codes", "chrome extension", "2fa", "verification code"),
        title="🔑 Caudal AI Auth Codes Extension",
        answer=(
            "When logging into Snorkel and prompted for a code, install the Chrome extension:\n"
            "🔗 **Extension Link:** https://chromewebstore.google.com/detail/caudal-ai-auth-codes/"
            "dieaafhpaldhiohgmdfhnjmdkgaobnbm\n\n"
            "**Important Steps:**\n"
            "• Log in using your Snorkel credentials to complete the code verification.\n"
            '• Make sure to check **"Remember me for 30 days"** so you don\'t have to re-enter codes every session.'
        ),
    ),
]


def _compile_keyword(keyword: str) -> re.Pattern[str]:
    # Whole-word match; any run of whitespace between words is accepted.
    body = r"\s+".join(re.escape(part) for part in keyword.split())
    return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)


def _specificity(keyword: str) -> tuple[int, int]:
    """How specific a keyword is: more words first, then more characters."""
    words = keyword.split()
    return len(words), len(" ".join(words))


# Pre-compiled once at import time so per-message matching stays cheap. Each entry's
# keywords are sorted most specific first, so the first hit is that entry's best one.
_COMPILED_FAQ: list[tuple[int, FAQEntry, tuple[tuple[tuple[int, int], re.Pattern[str]], ...]]] = [
    (index, entry, tuple(sorted(((_specificity(k), _compile_keyword(k)) for k in entry.keywords),
                                key=lambda pair: pair[0], reverse=True)))
    for index, entry in enumerate(FAQ_ENTRIES)
]


def match_faq(content: str) -> FAQEntry | None:
    """The entry whose matching keyword is most specific; ties go to the earlier entry."""
    best: tuple[tuple[int, int], int, FAQEntry] | None = None
    for index, entry, patterns in _COMPILED_FAQ:
        hit = next((spec for spec, pattern in patterns if pattern.search(content)), None)
        # Negated index: at equal specificity, the earlier entry ranks higher.
        if hit is not None and (best is None or (hit, -index) > (best[0], -best[1])):
            best = (hit, index, entry)
    return best[2] if best else None


# The public prompt removes itself after this; the private answers stay with each reader.
PROMPT_LIFETIME_SECONDS = 60


def find_entry(query: str) -> FAQEntry | None:
    """An exact topic title (what /faq's autocomplete fills in), else keyword matching."""
    wanted = query.strip().casefold()
    return next((e for e in FAQ_ENTRIES if e.title.casefold() == wanted), None) or match_faq(query)


def build_faq_embed(entry: FAQEntry, author: discord.abc.User | None = None) -> discord.Embed:
    asked = f"Asked by {author.mention} • " if author is not None else ""
    return discord.Embed(
        title=entry.title,  # entries carry their own emoji
        description=(
            f"{_blockquote(entry.answer)}\n\n"
            f"-# {asked}Still stuck? Click the button below to open a private ticket."
        ),
        color=COLOR_FAQ,
    )


def build_topics_embed(query: str | None) -> discord.Embed:
    intro = f"No FAQ matched **{discord.utils.escape_markdown(query)}**." if query else "Here's what the FAQ covers."
    return discord.Embed(
        title="📘 FAQ topics",
        description=f"{intro} Try `/faq` with one of these, or open a ticket below.\n\n"
                    + "\n".join(f"• {e.title}" for e in FAQ_ENTRIES),
        color=COLOR_FAQ,
    )


class FAQCog(commands.Cog, name="FAQ"):
    def __init__(self, bot: FAQBot) -> None:
        self.bot = bot
        self._faq_cooldowns: dict[tuple[int, str], float] = {}

    def _should_listen(self, message: discord.Message) -> bool:
        if message.author.bot or message.guild is None:
            return False
        channel = message.channel
        if not isinstance(channel, discord.TextChannel):
            return False
        settings = self.bot.settings.get(message.guild.id)   # cached: no database read per message
        if settings.tickets_category_id is not None and channel.category_id == settings.tickets_category_id:
            return False  # never auto-reply inside tickets
        if settings.faq_channel_ids:  # set with /set-faq-channels (or FAQ_CHANNEL_IDS for GUILD_ID)
            return channel.id in settings.faq_channel_ids
        # No allow-list configured: only public channels (@everyone can view).
        return channel.permissions_for(message.guild.default_role).view_channel

    def _on_cooldown(self, channel_id: int, entry: FAQEntry) -> bool:
        key = (channel_id, entry.title)
        now = time.monotonic()
        if now - self._faq_cooldowns.get(key, 0.0) < self.bot.config.faq_cooldown:
            return True
        self._faq_cooldowns[key] = now
        return False

    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message) -> None:
        # Ticket channels belong to the Tickets cog. This is the exact condition the old
        # single on_message used to route them there before any FAQ matching.
        if (
            not message.author.bot
            and message.guild is not None
            and ticket_owner_id(message.channel) is not None
        ):
            return
        if not self._should_listen(message) or not message.content:
            return
        entry = match_faq(message.content)
        if entry is None or self._on_cooldown(message.channel.id, entry):
            return

        # A public message can't be ephemeral, so post only a short prompt; the answer is
        # shown privately to whoever clicks it. The reply links the prompt to the question.
        try:
            await message.reply(
                f"📘 **{entry.title}**: tap **Show answer** to read it privately.",
                view=prompt_view(FAQ_ENTRIES.index(entry)),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
                delete_after=PROMPT_LIFETIME_SECONDS,
            )
        except discord.Forbidden:
            log.warning("Missing permission to reply in #%s", message.channel)
        except discord.HTTPException:
            log.exception("Failed to send FAQ prompt in #%s", message.channel)

    async def show_answer(self, interaction: discord.Interaction, index: int) -> None:
        """The "Show answer" button: the full answer, visible only to the person who clicked."""
        if not 0 <= index < len(FAQ_ENTRIES):
            await _send_ephemeral(interaction, "This answer is no longer available. Try /faq instead.")
            return
        prompt = interaction.message
        question = None
        if prompt is not None and prompt.reference is not None and prompt.reference.message_id and interaction.channel:
            question = (interaction.channel.id, prompt.reference.message_id)
        await interaction.response.send_message(
            embed=build_faq_embed(FAQ_ENTRIES[index]), view=answer_view(self.bot, question), ephemeral=True
        )

    async def answer_query(self, interaction: discord.Interaction, query: str | None) -> None:
        """/faq [query]: the matching answer, or the list of topics, visible only to the caller."""
        entry = find_entry(query) if query else None
        embed = build_faq_embed(entry) if entry else build_topics_embed(query)
        await interaction.response.send_message(embed=embed, view=answer_view(self.bot, None), ephemeral=True)

    @staticmethod
    def suggest(current: str) -> list[discord.app_commands.Choice[str]]:
        """/faq autocomplete: topics whose title or a keyword contains what's typed so far."""
        typed = current.strip().casefold()
        return [
            discord.app_commands.Choice(name=e.title, value=e.title)
            for e in FAQ_ENTRIES
            if not typed or typed in e.title.casefold() or any(typed in k for k in e.keywords)
        ][:25]
