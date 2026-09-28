"""Slash commands: /setup, /close, /ticket-purge, /staff-stats, /staff-leaderboard,
/set-staff-role, /set-faq-channels, /admin-help. Everything applies to the server it's run in."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Literal

import discord
from discord import app_commands
from discord.ext import commands

from ..common import CLOSE_REASON_MAX_LENGTH, COLOR_CLOSE, COLOR_FAQ, _send_ephemeral
from ..database import format_avg_response, timeframe_since
from ..provisioning import missing_permissions, provision_guild
from ..views.ticket_lifecycle import PurgeConfirmView

if TYPE_CHECKING:
    from ..main import FAQBot

log = logging.getLogger("faq_bot")

# (name, description) of every staff/admin command, in the order /admin-help lists them.
ADMIN_HELP = (
    ("/setup [staff_role]", "One-click setup: creates the ticket categories and log channels. *(Admin only)*"),
    ("/close [reason]", "Close and archive the current ticket."),
    ("/ticket-purge [category_type]", "Emergency cleanup of open/closed tickets. *(Admin only)*"),
    ("/staff-stats [member]", "View individual ticket volume and response metrics."),
    ("/staff-leaderboard [timeframe]", "Display top-performing staff members."),
    ("/set-staff-role [role]", "Configure the active staff role dynamically. *(Admin only)*"),
    ("/set-faq-channels [channels]", "Limit FAQ answers to some channels, or leave empty for all. *(Admin only)*"),
)

_CHANNEL_TOKEN = re.compile(r"<#(\d+)>|(\d+)")


def parse_channels(text: str, guild: discord.Guild) -> tuple[list[discord.TextChannel], list[str]]:
    """Text channels named in ``text`` (mentions or IDs, separated by spaces or commas),
    and the tokens that aren't text channels in this server."""
    found: dict[int, discord.TextChannel] = {}
    bad: list[str] = []
    for token in filter(None, re.split(r"[\s,]+", text.strip())):
        match = _CHANNEL_TOKEN.fullmatch(token)
        channel = guild.get_channel(int(match.group(1) or match.group(2))) if match else None
        if isinstance(channel, discord.TextChannel):
            found[channel.id] = channel
        else:
            bad.append(token)
    return list(found.values()), bad


def _staff_role_problem(role: discord.Role) -> str | None:
    if role.is_default():
        return "@everyone can't be the staff role: everyone would see every ticket."
    if role.managed:
        return f"{role.mention} is managed by an integration. Choose a regular role."
    return None


_NOT_SAVED = "\n⚠️ The database is unavailable, so this lasts only until the bot restarts."


class TicketCommands(commands.Cog, name="Commands"):
    def __init__(self, bot: FAQBot) -> None:
        self.bot = bot

    @app_commands.command(name="setup", description="Admin: create the ticket categories and log channels for this server.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(staff_role="The role whose members handle tickets")
    async def setup_command(self, interaction: discord.Interaction, staff_role: discord.Role) -> None:
        await self.run_setup(interaction, staff_role)

    @app_commands.command(name="set-faq-channels", description="Admin: limit FAQ answers to some channels (empty = all).")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channels="Channel mentions or IDs, e.g. #help #general. Leave empty for every channel.")
    async def set_faq_channels_command(self, interaction: discord.Interaction, channels: str | None = None) -> None:
        await self.set_faq_channels(interaction, channels)

    @app_commands.command(name="faq", description="Search the FAQ. Only you see the answer.")
    @app_commands.guild_only()
    @app_commands.describe(query="A topic or question, e.g. 'time doctor' or 'how do I log in?'")
    async def faq_command(self, interaction: discord.Interaction, query: str | None = None) -> None:
        await self.bot.faq.answer_query(interaction, query)

    @faq_command.autocomplete("query")
    async def _faq_autocomplete(self, _interaction: discord.Interaction, current: str):
        return self.bot.faq.suggest(current)

    @app_commands.command(name="close", description="Close (archive) this support ticket. It is deleted after 48 hours.")
    @app_commands.guild_only()
    @app_commands.describe(reason="Staff: why it's being closed (leave empty to be asked)")
    async def close_command(
        self, interaction: discord.Interaction,
        reason: app_commands.Range[str, 1, CLOSE_REASON_MAX_LENGTH] | None = None,
    ) -> None:
        await self.bot.tickets.close_ticket(interaction, reason)

    @app_commands.command(name="ticket-purge", description="Admin: delete ticket channels (transcripts are logged first).")
    @app_commands.guild_only()
    # Hides the command from non-admins in the client; start_purge re-checks at runtime,
    # since server admins can override command visibility in Integrations settings.
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(category_type="Active = open tickets, Archived = closed tickets, Both = all")
    async def purge_command(
        self, interaction: discord.Interaction, category_type: Literal["Active", "Archived", "Both"]
    ) -> None:
        await self.start_purge(interaction, category_type)

    @app_commands.command(name="staff-stats", description="Staff: ticket metrics for yourself or another staff member.")
    @app_commands.guild_only()
    @app_commands.describe(member="Staff member to look up (defaults to you)")
    async def staff_stats_command(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        await self.show_staff_stats(interaction, member)

    @app_commands.command(name="staff-leaderboard", description="Staff: top 5 staff by tickets handled.")
    @app_commands.guild_only()
    @app_commands.describe(timeframe="Period to rank (default: All Time)")
    async def leaderboard_command(
        self,
        interaction: discord.Interaction,
        timeframe: Literal["Last 7 Days", "Last 30 Days", "All Time"] = "All Time",
    ) -> None:
        await self.show_leaderboard(interaction, timeframe)

    @app_commands.command(name="set-staff-role", description="Admin: choose the role that staffs support tickets.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(role="The role whose members can see, claim and close tickets")
    async def set_staff_role_command(self, interaction: discord.Interaction, role: discord.Role) -> None:
        await self.set_staff_role(interaction, role)

    @app_commands.command(name="admin-help", description="Staff: list the staff and admin commands.")
    @app_commands.guild_only()
    async def admin_help_command(self, interaction: discord.Interaction) -> None:
        await self.show_admin_help(interaction)

    async def run_setup(self, interaction: discord.Interaction, staff_role: discord.Role) -> None:
        guild = interaction.guild
        # default_permissions only hides the command; server admins can override that, so re-check.
        if guild is None or not self.bot.is_admin(interaction.user):
            await _send_ephemeral(interaction, "Only administrators can run setup.")
            return
        if problem := _staff_role_problem(staff_role):
            await _send_ephemeral(interaction, problem)
            return
        if missing := missing_permissions(guild):
            await _send_ephemeral(interaction, f"I need the **{'** and **'.join(missing)}** permission to set up "
                                               "this server. Grant it to my role and run /setup again.")
            return
        # Several channel creations can exceed Discord's 3-second response window.
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await provision_guild(guild, staff_role, self.bot.settings.get(guild.id))
        except discord.HTTPException:
            log.exception("Setup failed in %s", guild.name)
            await interaction.followup.send(
                "Setup stopped partway because Discord refused a change (check my role's permissions). "
                "Run /setup again; it reuses everything already created.", ephemeral=True)
            return
        saved = await self.bot.settings.update(
            guild.id,
            staff_role_id=staff_role.id,
            tickets_category_id=result.tickets[0].id,
            archive_category_id=result.archive[0].id,
            transcript_log_channel_id=result.transcripts[0].id,
            sla_alert_channel_id=result.sla_alerts[0].id,
        )
        log.info("Setup completed in %s (%s) by %s", guild.name, guild.id, interaction.user)

        def line(item, *, mention: bool) -> str:
            obj, created = item
            return f"{obj.mention if mention else f'**{obj.name}**'} · {'created' if created else 'already existed'}"

        embed = discord.Embed(
            title="✅ Setup complete",
            description=f"Tickets are ready in this server. {staff_role.mention} handles them." + (
                "" if saved else _NOT_SAVED),
            color=discord.Color.blurple(),
        )
        embed.add_field(name="🎫 Tickets category", value=line(result.tickets, mention=False), inline=False)
        embed.add_field(name="📦 Archive category", value=line(result.archive, mention=False), inline=False)
        embed.add_field(name="📁 Transcripts", value=line(result.transcripts, mention=True), inline=False)
        embed.add_field(name="⚠️ SLA alerts", value=line(result.sla_alerts, mention=True), inline=False)
        embed.set_footer(text="Run /setup again any time: it reuses what exists and only adds what's missing.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def set_staff_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        # default_permissions only hides the command; server admins can override that, so re-check.
        if interaction.guild is None or not self.bot.is_admin(interaction.user):
            await _send_ephemeral(interaction, "Only administrators can change the staff role.")
            return
        if problem := _staff_role_problem(role):
            await _send_ephemeral(interaction, problem)
            return
        saved = await self.bot.settings.update(interaction.guild.id, staff_role_id=role.id)
        log.info("Staff role in %s set to %s (%s) by %s", interaction.guild.id, role.name, role.id, interaction.user)
        await interaction.response.send_message(
            f"✅ Staff role has been updated to {role.mention}." + ("" if saved else _NOT_SAVED), ephemeral=True)

    async def set_faq_channels(self, interaction: discord.Interaction, channels: str | None) -> None:
        guild = interaction.guild
        if guild is None or not self.bot.is_admin(interaction.user):
            await _send_ephemeral(interaction, "Only administrators can change the FAQ channels.")
            return
        found, bad = parse_channels(channels or "", guild)
        if bad:
            await _send_ephemeral(interaction, f"These aren't text channels in this server: {', '.join(bad)}")
            return
        saved = await self.bot.settings.update(guild.id, faq_channel_ids=frozenset(c.id for c in found))
        if found:
            text = f"✅ FAQ answers are now limited to {', '.join(c.mention for c in found)}."
        else:
            text = "✅ FAQ answers are now enabled in every channel members can see."
        log.info("FAQ channels in %s set to %s by %s", guild.id, [c.id for c in found] or "all", interaction.user)
        await interaction.response.send_message(text + ("" if saved else _NOT_SAVED), ephemeral=True)

    async def show_admin_help(self, interaction: discord.Interaction) -> None:
        if not self.bot.is_staff(interaction.user):  # staff role or Administrator
            await _send_ephemeral(interaction, "Only staff can view the admin commands.")
            return
        embed = discord.Embed(
            title="🛠️ Staff & Admin Commands",
            description="Commands available to the support team. *(Admin only)* needs the Administrator permission.",
            color=discord.Color.blurple(),
        )
        for name, value in ADMIN_HELP:
            embed.add_field(name=name, value=value, inline=False)
        role = self.bot.staff_role(interaction.guild) if interaction.guild else None
        embed.set_footer(text=f"Active staff role: @{role.name}" if role else "Active staff role: not found")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _analytics_guard(self, interaction: discord.Interaction) -> bool:
        if not self.bot.is_staff(interaction.user):
            await _send_ephemeral(interaction, "Only staff can view ticket analytics.")
            return False
        if self.bot.db is None:
            await _send_ephemeral(interaction, "The analytics database is unavailable. Check the bot's logs.")
            return False
        return True

    async def show_staff_stats(self, interaction: discord.Interaction, member: discord.Member | None) -> None:
        if not await self._analytics_guard(interaction):
            return
        target = member or interaction.user
        # Only this server's tickets: the database holds every server's.
        guild_id = interaction.guild.id if interaction.guild else None
        overall = await self.bot.db.staff_stats(target.id, guild_id=guild_id)
        recent = await self.bot.db.staff_stats(target.id, timeframe_since("Last 30 Days"), guild_id=guild_id)

        embed = discord.Embed(title=f"📊 Staff Stats • {target.display_name}", color=COLOR_FAQ)
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="🎫 Tickets Handled", value=f"**{overall.tickets}** all time\n{recent.tickets} in the last 30 days", inline=True)
        embed.add_field(name="⚡ Avg First Response", value=format_avg_response(overall.avg_response_seconds), inline=True)
        embed.set_footer(text="Handled = claimed the ticket, or closed it if nobody claimed it.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def show_leaderboard(self, interaction: discord.Interaction, timeframe: str) -> None:
        if not await self._analytics_guard(interaction):
            return
        guild_id = interaction.guild.id if interaction.guild else None
        rows = await self.bot.db.leaderboard(timeframe_since(timeframe), limit=5, guild_id=guild_id)
        embed = discord.Embed(title=f"🏆 Staff Leaderboard • {timeframe}", color=COLOR_FAQ, timestamp=discord.utils.utcnow())
        if not rows:
            embed.description = "No handled tickets in this timeframe yet."
        else:
            medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
            embed.description = "\n".join(
                f"{medals[i]} <@{r.staff_id}> — **{r.tickets}** ticket{'s' * (r.tickets != 1)}\n"
                f"-# ⚡ {format_avg_response(r.avg_response_seconds)} avg first response"
                for i, r in enumerate(rows)
            )
        embed.set_footer(text="Ranked by tickets handled (claimed, or closed if unclaimed), then by response speed")
        # Mentions render as names but must not ping anyone.
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def start_purge(self, interaction: discord.Interaction, category_type: str) -> None:
        if interaction.guild is None or not self.bot.is_admin(interaction.user):
            await _send_ephemeral(interaction, "Only administrators can purge tickets.")
            return
        if self.bot.tickets._purge_running:
            await _send_ephemeral(interaction, "A purge is already running.")
            return
        targets = self.bot.tickets._purge_targets(interaction.guild, category_type)
        if not targets:
            await _send_ephemeral(interaction, f"There are no {category_type.lower()} ticket channels to purge.")
            return
        logged = (
            "A transcript of each ticket will be posted to the log channel first."
            if self.bot.transcript_channel(interaction.guild) is not None
            else "⚠️ **No transcript log channel is configured, so these conversations will be lost.**"
        )
        embed = discord.Embed(
            title="⚠️ Confirm Ticket Purge",
            description=(
                f"This will permanently delete **{len(targets)}** ticket channel(s) "
                f"(**{category_type}**). This cannot be undone.\n\n{logged}"
            ),
            color=COLOR_CLOSE,
        )
        await interaction.response.send_message(embed=embed, view=PurgeConfirmView(self.bot, category_type), ephemeral=True)
