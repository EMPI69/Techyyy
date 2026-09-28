"""Entry point: the bot class (shared state, lookups, persistent views, command sync),
graceful shutdown, and the command line.

    python bot.py --run          connect to Discord
    python bot.py --dry-run      validate the setup offline (alias: --test)
    python bot.py --version

Also runnable as ``python -m caudal_bot``. A launch without --run never logs in.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
import os
import pkgutil
import platform
import signal
import sys
from typing import Callable

import discord
from discord import app_commands
from discord.ext import commands

from . import __version__
from .common import _send_ephemeral
from .config import Config
from .database import DatabaseMaintenance, TicketDB, inspect_schema
from .guild_settings import GuildSettings, GuildSettingsStore
from .cogs.commands import TicketCommands
from .cogs.dashboard import DashboardCog
from .cogs.faq import FAQCog
from .cogs.health import HealthCog
from .cogs.tickets import TicketsCog
from .transcripts.cleanup import TranscriptCleanup
from .transcripts.delivery import TranscriptDelivery
from .views.faq import OpenTicketView, QuestionTicketButton, ShowAnswerButton
from .views.ticket_lifecycle import ClosedTicketView, TicketControlView

log = logging.getLogger("faq_bot")

class FAQBot(commands.Bot):
    """Holds what everything shares (config, the database, per-server settings, Discord lookups).

    Behaviour lives in the cogs and services created here. They reach each other
    through this object (``bot.tickets``, ``bot.transcripts``, ...), never by
    importing one another, so the package has no circular imports.

    The bot serves every server it's in. Each server's staff role, categories and
    channels come from ``bot.settings`` (see guild_settings.py), never straight from .env.
    """

    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()  # includes guilds + guild_messages
        intents.message_content = True       # privileged: enable it in the Developer Portal too
        intents.guilds = True
        super().__init__(
            # commands.Bot requires a prefix; this bot has no prefix commands (see on_message).
            command_prefix=commands.when_mentioned,
            help_command=None,
            intents=intents,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
        )
        self.config = config
        self.tree.on_error = self._on_app_command_error
        self.db: TicketDB | None = None  # None if it failed to open; analytics then degrade gracefully
        self.settings = GuildSettingsStore(self)

        self.transcripts = TranscriptDelivery(self)
        self.tickets = TicketsCog(self)
        self.faq = FAQCog(self)
        self.ticket_commands = TicketCommands(self)
        self.maintenance = DatabaseMaintenance(self)
        self.transcript_cleanup = TranscriptCleanup(self)
        self.dashboard = DashboardCog(self)
        self.health = HealthCog(self)

    # ---- lifecycle -------------------------------------------------------- #

    async def setup_hook(self) -> None:
        # Register persistent views so buttons on old messages still work after restart.
        self.add_view(OpenTicketView(self))
        self.add_view(TicketControlView(self))
        self.add_view(ClosedTicketView(self))
        # FAQ "Show answer" and question-aware ticket buttons carry data in their custom_id.
        self.add_dynamic_items(ShowAnswerButton, QuestionTicketButton)
        # The database opens before any cog loads, since their loops and the dashboard use it.
        await self.open_database()
        await self.load_settings()
        # Each cog's cog_load starts its own loop or server (same order as before the
        # refactor): dashboard, daily backup, 48h cleanup + SLA check, transcript purge.
        for cog in (self.dashboard, self.maintenance, self.tickets, self.faq, self.ticket_commands,
                    self.transcript_cleanup, self.health):
            await self.add_cog(cog)
        await self.sync_commands()

    async def sync_commands(self) -> None:
        """Register the slash commands globally, so they work in every server the bot joins.

        Versions before multi-server support registered them to GUILD_ID only. Those copies
        are removed here, or that server would list every command twice.
        """
        synced = await self.tree.sync()
        log.info("Synced %d app command(s) globally", len(synced))
        if self.config.guild_id is not None:
            try:
                await self.tree.sync(guild=discord.Object(id=self.config.guild_id))  # empty: clears old copies
            except discord.HTTPException:
                log.warning("Could not remove old server-only commands from %s", self.config.guild_id)

    async def on_message(self, message: discord.Message, /) -> None:
        # commands.Bot would parse prefix commands here. This bot has none, so do nothing;
        # the FAQ and Tickets cogs still get every message through their own listeners.
        return

    async def open_database(self) -> None:
        path = self.config.database_file
        try:
            self.db = await TicketDB.open(path)
            log.info("Analytics database ready at %s", path)
        except Exception:
            # Tickets keep working without analytics; the commands say it's unavailable.
            log.exception("Could not open analytics database %s; analytics and SLA alerts are disabled", path)
            self.db = None

    async def load_settings(self) -> None:
        """Load every server's settings. Without a database, .env values apply to GUILD_ID."""
        await self.settings.load()
        if self.db is None or self.config.guild_id is None:
            return
        # Metrics recorded before they were per-server all came from GUILD_ID.
        try:
            if tagged := await self.db.assign_unowned_metrics(self.config.guild_id):
                log.info("Assigned %d earlier ticket record(s) to server %s", tagged, self.config.guild_id)
        except Exception:
            log.exception("Could not assign earlier ticket records to server %s", self.config.guild_id)

    async def close(self) -> None:
        # commands.Bot.close() removes every cog first; their cog_unload cancels the
        # loops and stops the dashboard. The database closes after, once nothing uses it.
        await super().close()
        if self.db is not None:
            await self.db.close()
            self.db = None

    # ---- shared lookups used by the cogs ---------------------------------- #

    async def on_ready(self) -> None:
        log.info("Logged in as %s (ID: %s) in %d server(s)", self.user, self.user.id if self.user else "?",
                 len(self.guilds))
        for guild in self.managed_guilds():
            self.log_setup_problems(guild)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.info("Joined server %s (%s); an administrator can run /setup there", guild.name, guild.id)

    def log_setup_problems(self, guild: discord.Guild) -> None:
        """Log settings that point at roles or channels that no longer exist."""
        s = self.settings.get(guild.id)
        if s == GuildSettings():
            log.info("%s (%s) isn't set up yet; tickets still work, run /setup to finish", guild.name, guild.id)
            return
        checks = (
            ("staff role", s.staff_role_id, self.staff_role(guild)),
            ("tickets category", s.tickets_category_id, self.tickets_category(guild)),
            ("archive category", s.archive_category_id, self.archive_category(guild)),
            ("transcript channel", s.transcript_log_channel_id, self.transcript_channel(guild)),
            ("SLA alert channel", s.sla_alert_channel_id, self.sla_alert_channel(guild)),
        )
        for label, configured, found in checks:
            if configured is not None and found is None:
                log.error("%s (%s): the %s %s no longer exists; run /setup to fix it",
                          guild.name, guild.id, label, configured)

    def managed_guilds(self) -> list[discord.Guild]:
        """Every server the bot is in (the background loops walk these)."""
        return list(self.guilds)

    def staff_role_id(self, guild_id: int | None) -> int | None:
        """The server's staff role: set with /setup or /set-staff-role (else .env for GUILD_ID)."""
        return self.settings.get(guild_id).staff_role_id

    def staff_role(self, guild: discord.Guild | None) -> discord.Role | None:
        role_id = self.staff_role_id(guild.id) if guild is not None else None
        return guild.get_role(role_id) if role_id is not None else None

    def is_staff(self, user: discord.abc.User) -> bool:
        """Has this server's staff role, or is an administrator there."""
        if not isinstance(user, discord.Member):
            return False
        role_id = self.staff_role_id(user.guild.id)
        return user.guild_permissions.administrator or (role_id is not None and user.get_role(role_id) is not None)

    @staticmethod
    def is_admin(user: discord.abc.User) -> bool:
        return isinstance(user, discord.Member) and user.guild_permissions.administrator

    def _lookup(self, guild: discord.Guild | None, channel_id: int | None, kind: type):
        if guild is None or channel_id is None:
            return None
        channel = guild.get_channel(channel_id)
        return channel if isinstance(channel, kind) else None

    def tickets_category(self, guild: discord.Guild | None) -> discord.CategoryChannel | None:
        return self._lookup(guild, self.settings.get(guild.id if guild else None).tickets_category_id,
                            discord.CategoryChannel)

    def archive_category(self, guild: discord.Guild | None) -> discord.CategoryChannel | None:
        """None = archive closed tickets in place (not configured, deleted, or the tickets category itself)."""
        s = self.settings.get(guild.id if guild else None)
        if s.archive_category_id == s.tickets_category_id:
            return None
        return self._lookup(guild, s.archive_category_id, discord.CategoryChannel)

    def transcript_channel(self, guild: discord.Guild | None) -> discord.TextChannel | None:
        return self._lookup(guild, self.settings.get(guild.id if guild else None).transcript_log_channel_id,
                            discord.TextChannel)

    def sla_alert_channel(self, guild: discord.Guild | None) -> discord.TextChannel | None:
        return self._lookup(guild, self.settings.get(guild.id if guild else None).sla_alert_channel_id,
                            discord.TextChannel)

    async def resolve_user(self, guild: discord.Guild, user_id: int | None) -> discord.abc.User | None:
        if user_id is None:
            return None
        # The members intent is off, so the cache may miss them; fall back to the API.
        user = guild.get_member(user_id) or self.get_user(user_id)
        if user is None:
            try:
                user = await self.fetch_user(user_id)
            except discord.HTTPException:
                return None
        return user

    async def _on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        log.exception("App command error", exc_info=error)
        await _send_ephemeral(interaction, "Something went wrong while running that command.")


def build_bot(config: Config) -> FAQBot:
    return FAQBot(config)


# --------------------------------------------------------------------------- #
# Graceful shutdown
# --------------------------------------------------------------------------- #

# After close() starts, how long to wait for the gateway task to finish before cancelling it.
GATEWAY_STOP_TIMEOUT_SECONDS = 10


def install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> Callable[[], None]:
    """Make SIGINT/SIGTERM (and SIGBREAK on Windows) set ``stop``. Returns an undo function.

    The first signal starts a graceful shutdown; a second one force-quits, for when
    something hangs. Unix uses the loop's native handlers; Windows has none, so it
    falls back to signal.signal and hands the event to the loop thread-safely.
    """
    def trigger(name: str) -> None:
        if stop.is_set():
            log.warning("Received %s again; forcing exit", name)
            os._exit(130)
        log.info("Received %s; shutting down gracefully (send it again to force quit)", name)
        stop.set()

    signals = [signal.SIGINT, signal.SIGTERM] + ([signal.SIGBREAK] if hasattr(signal, "SIGBREAK") else [])
    undo: list[Callable[[], object]] = []
    for sig in signals:
        try:
            loop.add_signal_handler(sig, trigger, sig.name)
            undo.append(lambda sig=sig: loop.remove_signal_handler(sig))
        except (NotImplementedError, RuntimeError):
            previous = signal.signal(sig, lambda signum, _frame: loop.call_soon_threadsafe(
                trigger, signal.Signals(signum).name))
            undo.append(lambda sig=sig, previous=previous: signal.signal(sig, previous))

    def restore() -> None:
        for fn in reversed(undo):
            fn()
    return restore


async def serve(bot: FAQBot, token: str, *, stop: asyncio.Event | None = None) -> None:
    """Run the bot until the gateway stops or a shutdown signal arrives, then close cleanly.

    bot.close() removes every cog (each cog_unload cancels and awaits its loops, and the
    dashboard's aiohttp runner is cleaned up), closes the Discord connection so no
    session or heartbeat is left dangling, and finally closes the database.
    Login errors (bad token, missing intents) propagate to the caller.
    """
    stop = stop or asyncio.Event()
    restore = install_signal_handlers(asyncio.get_running_loop(), stop)
    try:
        async with bot:
            gateway = asyncio.create_task(bot.start(token), name="discord-gateway")
            stopper = asyncio.create_task(stop.wait(), name="shutdown-signal")
            await asyncio.wait({gateway, stopper}, return_when=asyncio.FIRST_COMPLETED)
            stopper.cancel()
            await bot.close()
            try:
                # Re-raises LoginFailure etc. if that's why the gateway stopped.
                await asyncio.wait_for(gateway, timeout=GATEWAY_STOP_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                log.warning("Gateway did not stop within %ss; cancelling it", GATEWAY_STOP_TIMEOUT_SECONDS)
            except asyncio.CancelledError:
                pass
    finally:
        restore()
    log.info("Shutdown complete")


# --------------------------------------------------------------------------- #
# Dry run: validate everything without touching Discord
# --------------------------------------------------------------------------- #

async def dry_run(config: Config) -> int:
    """Import every module, check config, paths and the database schema. Never logs in.

    Read-only: an existing tickets.db is opened in read-only mode, and a missing one is
    not created (its schema is validated in memory instead). Returns an exit code.
    """
    problems: list[str] = []
    ok = lambda msg: print(f"  ok    {msg}")
    fail = lambda msg: (problems.append(msg), print(f"  FAIL  {msg}"))
    warn = lambda msg: print(f"  warn  {msg}")

    print(f"caudal_bot {__version__} dry run (no Discord connection)")
    import caudal_bot
    # Entry-point modules (__main__) are skipped: importing them is what runs the CLI.
    modules = [m.name for m in pkgutil.walk_packages(caudal_bot.__path__, "caudal_bot.")
               if not m.name.endswith("__main__")]
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as exc:
            fail(f"import {name}: {exc!r}")
    ok(f"imported {len(modules)} modules")

    if config.guild_id is None:
        ok("config: no GUILD_ID; every server is configured with /setup")
    else:
        ok(f"config: .env fallback for server {config.guild_id} (staff role {config.staff_role_id or '—'}, "
           f"tickets category {config.tickets_category_id or '—'}); /setup overrides it")
    if config.dashboard_token:
        (warn if len(config.dashboard_token) < 16 else ok)(
            f"dashboard enabled on {config.dashboard_host}:{config.dashboard_port}")
    else:
        ok("dashboard disabled (DASHBOARD_AUTH_TOKEN not set)")
    if config.health_port is None:
        ok("health endpoint disabled (HEALTH_PORT=off)")
    elif config.health_on_dashboard:
        ok(f"health endpoint on {config.dashboard_host}:{config.dashboard_port}/health (shared with the dashboard)")
    else:
        ok(f"health endpoint on {config.health_host}:{config.health_port}/health")

    # Only these three locations are written to. The code directory itself may be
    # read-only (the container image and the systemd unit both make it so).
    for label, target in (("database folder", config.database_file.parent),
                          ("transcripts folder", config.transcripts_dir), ("backups folder", config.backups_dir)):
        probe = target if target.exists() else target.parent  # created on first use if missing
        if probe.is_dir() and os.access(probe, os.W_OK):
            ok(f"{label} writable: {target}")
        else:
            fail(f"{label} not writable: {target}")

    db_file = config.database_file
    schema_problems, to_create = await inspect_schema(db_file)
    for p in schema_problems:
        fail(f"database: {p}")
    if not schema_problems:
        ok(f"database schema valid: {db_file}" if db_file.exists()
           else f"database schema valid (in memory); {db_file.name} will be created on first run")
    if to_create:
        ok(f"database: {', '.join(to_create)} will be added on the next start")

    try:
        bot = FAQBot(config)  # builds cogs and views; no network, nothing started
        ids = [i.custom_id for v in (OpenTicketView(bot), TicketControlView(bot), ClosedTicketView(bot))
               for i in v.children]
        if len(ids) != len(set(ids)):
            fail("duplicate persistent custom_id")
        commands_ = sorted(c.name for c in bot.ticket_commands.get_app_commands())
        ok(f"{len(ids)} persistent buttons; slash commands: {', '.join('/' + c for c in commands_)}")
        await bot.close()
    except Exception as exc:
        fail(f"building the bot failed: {exc!r}")

    print(("dry run passed" if not problems else f"dry run FAILED ({len(problems)} problem(s))"))
    return 0 if not problems else 1


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #

def build_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Caudal support bot: FAQ auto-replies with ticket escalation.",
        epilog="Nothing connects to Discord unless --run is given.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run", action="store_true", help="connect to Discord and run the bot")
    mode.add_argument("--dry-run", "--test", dest="dry_run", action="store_true",
                      help="validate config, imports and the database schema, then exit (never logs in)")
    mode.add_argument("--version", action="version",
                      version=f"caudal_bot {__version__} (discord.py {discord.__version__}, "
                              f"Python {platform.python_version()})")
    return parser


def main(argv: list[str] | None = None, *, prog: str | None = None) -> None:
    parser = build_parser(prog)
    args = parser.parse_args(argv)  # --help / --version print and exit here, before anything else
    if not (args.run or args.dry_run):
        # Safety guard: a bare launch (or a typo'd flag handled above) must never log in.
        parser.print_usage(sys.stderr)
        print("Refusing to start without --run (use --dry-run to check the setup offline).", file=sys.stderr)
        raise SystemExit(2)

    discord.utils.setup_logging(level=logging.INFO)
    try:
        config = Config.from_env()
    except RuntimeError as exc:
        log.critical("%s", exc)
        raise SystemExit(1) from exc

    if args.dry_run:
        raise SystemExit(asyncio.run(dry_run(config)))

    try:
        asyncio.run(serve(build_bot(config), config.bot_token))
    except discord.LoginFailure:
        log.critical("Invalid BOT_TOKEN")
        raise SystemExit(1)
    except discord.PrivilegedIntentsRequired:
        log.critical("Enable the MESSAGE CONTENT intent in the Discord Developer Portal (Bot tab)")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
