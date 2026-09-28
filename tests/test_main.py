"""Startup wiring, slash-command contract, message routing, the CLI safety guards,
signal handling and graceful shutdown."""

from __future__ import annotations

import asyncio
import signal
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.ext import tasks

import helpers as h
from caudal_bot import __version__, main as main_module
from caudal_bot.common import stop_loop
from caudal_bot.main import FAQBot, dry_run, install_signal_handlers, serve

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PERSISTENT_IDS = {"faq:open_ticket", "ticket:claim", "ticket:close", "ticket:reopen", "ticket:delete_now"}


async def started(bot: FAQBot) -> FAQBot:
    """Run the real setup_hook with only the Discord API call (command sync) faked."""
    await bot._async_setup_hook()                      # what login() does first: attach the event loop
    bot.tree.sync = AsyncMock(return_value=[object()] * 4)
    await bot.setup_hook()
    return bot


# ---- wiring ----------------------------------------------------------------------------

async def test_setup_hook_wires_everything(make_config):
    b = await started(FAQBot(make_config(database_path="t.db")))
    try:
        store = b._connection._view_store
        assert {key[1] for key in store._views.get(None, {})} == PERSISTENT_IDS
        assert all(v.timeout is None for v in store.persistent_views)
        from caudal_bot.views.faq import QuestionTicketButton, ShowAnswerButton
        assert set(store._dynamic_items.values()) == {ShowAnswerButton, QuestionTicketButton}
        assert set(b.cogs) == {"Dashboard", "Database", "Tickets", "FAQ", "Commands", "TranscriptCleanup",
                              "Health"}
        assert b.db is not None and b.config.database_file.exists()      # opened before the cogs
        assert b.get_cog("Tickets") is b.tickets and b.tickets.bot is b and b.transcripts.bot is b
        # Global (every server), then GUILD_ID's old server-only copies are cleared.
        assert [c.kwargs.get("guild") for c in b.tree.sync.await_args_list][0] is None
        assert b.tree.sync.await_args_list[1].kwargs["guild"].id == h.GUILD_ID
        assert b.tree.get_commands(guild=discord.Object(h.GUILD_ID)) == []   # ...by syncing an empty list
        for loop in (b.tickets._cleanup_expired, b.tickets._sla_check, b.maintenance._backup_database,
                     b.transcript_cleanup._purge_loop):
            assert loop.is_running()
        assert b.dashboard.server is None                                  # no token: never starts
        assert not b.health.running                                        # no HEALTH_PORT: off
    finally:
        await b.close()


def payload(bot, name):
    cmd = next(c for c in bot.ticket_commands.get_app_commands() if c.name == name)
    d = cmd.to_dict(bot.tree)
    options = [(o["name"], o["type"], o.get("required", False), [c["value"] for c in o.get("choices", [])])
               for o in d.get("options", [])]
    perms = d.get("default_member_permissions")
    return d["description"], options, None if perms is None else str(perms), d.get("contexts")


def test_slash_command_contract(bot):
    """Exact API payloads; a change here changes what server members see."""
    assert payload(bot, "close") == (
        "Close (archive) this support ticket. It is deleted after 48 hours.", [("reason", 3, False, [])], None, [0])
    assert payload(bot, "ticket-purge") == (
        "Admin: delete ticket channels (transcripts are logged first).",
        [("category_type", 3, True, ["Active", "Archived", "Both"])], "8", [0])   # 8 = Administrator
    assert payload(bot, "staff-stats") == (
        "Staff: ticket metrics for yourself or another staff member.", [("member", 6, False, [])], None, [0])
    assert payload(bot, "staff-leaderboard") == (
        "Staff: top 5 staff by tickets handled.",
        [("timeframe", 3, False, ["Last 7 Days", "Last 30 Days", "All Time"])], None, [0])
    assert payload(bot, "set-staff-role") == (
        "Admin: choose the role that staffs support tickets.", [("role", 8, True, [])], "8", [0])
    assert payload(bot, "admin-help") == ("Staff: list the staff and admin commands.", [], None, [0])
    assert payload(bot, "setup") == (
        "Admin: create the ticket categories and log channels for this server.",
        [("staff_role", 8, True, [])], "8", [0])
    assert payload(bot, "set-faq-channels") == (
        "Admin: limit FAQ answers to some channels (empty = all).", [("channels", 3, False, [])], "8", [0])
    assert payload(bot, "faq") == (
        "Search the FAQ. Only you see the answer.", [("query", 3, False, [])], None, [0])
    assert sorted(c.name for c in bot.ticket_commands.get_app_commands()) == [
        "admin-help", "close", "faq", "set-faq-channels", "set-staff-role", "setup", "staff-leaderboard",
        "staff-stats", "ticket-purge"]


async def test_messages_route_to_exactly_one_handler(make_config, monkeypatch):
    monkeypatch.setattr(FAQBot, "user", property(lambda self: h.ME))
    b = await started(FAQBot(make_config()))
    try:
        b.tickets._on_ticket_message = AsyncMock()

        def message(content, topic=None, category_id=77, author_bot=False):
            ch = MagicMock(spec=discord.TextChannel)
            ch.topic, ch.category_id, ch.id = topic, category_id, 55
            ch.permissions_for = lambda _r: NS(view_channel=True)
            m = MagicMock()
            m.content, m.channel, m.reply = content, ch, AsyncMock()
            m.author = NS(bot=author_bot, id=3, mention="<@3>")
            m.guild = NS(id=h.GUILD_ID, default_role=object())
            return m

        async def dispatch(m):
            b.dispatch("message", m)
            await asyncio.sleep(0.05)

        in_ticket = message("snorkel login?", topic="ticket-owner:3", category_id=h.TICKETS_CAT)
        await dispatch(in_ticket)
        b.tickets._on_ticket_message.assert_awaited_once_with(in_ticket)
        in_ticket.reply.assert_not_awaited()
        public = message("snorkel login?")
        await dispatch(public)
        public.reply.assert_awaited_once()
        assert b.tickets._on_ticket_message.await_count == 1
        b.process_commands = AsyncMock()          # commands.Bot's prefix parser must never run
        await dispatch(message("<@999> help"))
        b.process_commands.assert_not_awaited()
    finally:
        await b.close()


# ---- CLI guards --------------------------------------------------------------------------

@pytest.fixture
def cli(monkeypatch, make_config):
    """main() with config loading and logging setup redirected to the test sandbox."""
    loaded = []
    def from_env():
        loaded.append(True)
        return make_config()
    monkeypatch.setattr(main_module.Config, "from_env", staticmethod(from_env))
    monkeypatch.setattr(discord.utils, "setup_logging", lambda **_kw: None)
    serve_calls = []
    async def fake_serve(bot, token, **_kw):
        serve_calls.append((bot, token))
    monkeypatch.setattr(main_module, "serve", fake_serve)
    return NS(loaded=loaded, serve_calls=serve_calls)


def run_main(*argv):
    with pytest.raises(SystemExit) as exc:
        main_module.main(list(argv))
    return exc.value.code


def test_bare_launch_refuses_to_start(cli, capsys):
    assert run_main() == 2
    assert "Refusing to start without --run" in capsys.readouterr().err
    assert not cli.loaded and not cli.serve_calls       # config never even read


def test_help_and_version_never_start_anything(cli, capsys):
    assert run_main("--help") == 0
    assert "Nothing connects to Discord unless --run is given." in capsys.readouterr().out
    assert run_main("--version") == 0
    assert capsys.readouterr().out.startswith(f"caudal_bot {__version__} (discord.py ")
    assert not cli.loaded and not cli.serve_calls


@pytest.mark.parametrize("argv", [["--run", "--dry-run"], ["--run", "--test"], ["--rn"], ["run"]])
def test_conflicting_or_unknown_flags_are_rejected(cli, argv):
    assert run_main(*argv) == 2 and not cli.serve_calls


def test_run_flag_starts_the_bot(cli):
    main_module.main(["--run"])                          # fake serve returns normally
    (bot, token), = cli.serve_calls
    assert isinstance(bot, FAQBot) and token == "test-token"


@pytest.mark.parametrize("error", [discord.LoginFailure("bad"), discord.PrivilegedIntentsRequired(None)])
def test_login_errors_exit_1(cli, monkeypatch, error):
    async def failing(*_a, **_k):
        raise error
    monkeypatch.setattr(main_module, "serve", failing)
    assert run_main("--run") == 1


def test_invalid_config_exits_1(monkeypatch):
    monkeypatch.setattr(discord.utils, "setup_logging", lambda **_kw: None)
    def broken():
        raise RuntimeError("Missing required environment variable: BOT_TOKEN")
    monkeypatch.setattr(main_module.Config, "from_env", staticmethod(broken))
    assert run_main("--dry-run") == 1


def test_dry_run_flag_passes_offline(cli, capsys, make_config):
    assert run_main("--test") == 0
    out = capsys.readouterr().out
    assert "dry run passed" in out and "will be created on first run" in out
    assert not make_config().database_file.exists()     # read-only: nothing created
    assert not cli.serve_calls


async def test_dry_run_fails_on_a_broken_database(make_config, capsys):
    cfg = make_config()
    with sqlite3.connect(cfg.database_file) as con:
        con.execute("CREATE TABLE ticket_metrics (ticket_id INTEGER PRIMARY KEY)")
    assert await dry_run(cfg) == 1
    assert "missing column channel_id" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [[], ["--help"], ["--version"]])
@pytest.mark.parametrize("entry", [["bot.py"], ["-m", "caudal_bot"]])
def test_real_entry_points_are_safe(entry, argv):
    """The actual launchers, in a subprocess. None of these reach config loading."""
    result = subprocess.run([sys.executable, *entry, *argv], cwd=PROJECT_ROOT, capture_output=True, text=True,
                            timeout=60, env={"PATH": "", "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")})
    if not argv:
        assert result.returncode == 2 and "Refusing to start" in result.stderr
    else:
        assert result.returncode == 0 and ("usage:" in result.stdout or __version__ in result.stdout)


# ---- signals ---------------------------------------------------------------------------------

@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
async def test_signals_request_a_graceful_stop(sig):
    stop = asyncio.Event()
    previous = signal.getsignal(sig)
    restore = install_signal_handlers(asyncio.get_running_loop(), stop)
    try:
        # Never deliver a signal the process isn't handling (SIGTERM's default is to terminate).
        assert signal.getsignal(sig) not in (signal.SIG_DFL, previous)
        signal.raise_signal(sig)
        await asyncio.wait_for(stop.wait(), timeout=2)
    finally:
        restore()
    assert signal.getsignal(sig) == previous


async def test_second_signal_forces_exit(monkeypatch):
    exits = []
    monkeypatch.setattr(main_module.os, "_exit", exits.append)
    stop = asyncio.Event()
    restore = install_signal_handlers(asyncio.get_running_loop(), stop)
    try:
        signal.raise_signal(signal.SIGINT)
        await asyncio.wait_for(stop.wait(), timeout=2)
        signal.raise_signal(signal.SIGINT)
        for _ in range(40):
            if exits:
                break
            await asyncio.sleep(0.05)
    finally:
        restore()
    assert exits == [130]


class FakeGatewayBot:
    """Just enough of discord.Client for serve(): async context manager, start(), close()."""

    def __init__(self, *, fail: Exception | None = None, ignore_close: bool = False):
        self.fail, self.ignore_close = fail, ignore_close
        self._closed = asyncio.Event()
        self.close_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        if not self._closed.is_set():
            await self.close()

    async def start(self, token):
        if self.fail:
            raise self.fail
        await (asyncio.Event().wait() if self.ignore_close else self._closed.wait())

    async def close(self):
        self.close_calls += 1
        self._closed.set()


async def test_serve_closes_cleanly_on_signal():
    bot, stop = FakeGatewayBot(), asyncio.Event()
    previous = signal.getsignal(signal.SIGINT)
    task = asyncio.create_task(serve(bot, "tok", stop=stop))
    await asyncio.sleep(0.05)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert bot.close_calls == 1
    assert signal.getsignal(signal.SIGINT) == previous   # whatever was installed before is restored


async def test_serve_propagates_login_failure_after_closing():
    bot = FakeGatewayBot(fail=discord.LoginFailure("bad token"))
    with pytest.raises(discord.LoginFailure):
        await serve(bot, "tok")
    assert bot.close_calls == 1


async def test_serve_does_not_hang_on_a_stuck_gateway(monkeypatch):
    monkeypatch.setattr(main_module, "GATEWAY_STOP_TIMEOUT_SECONDS", 0.05)
    bot, stop = FakeGatewayBot(ignore_close=True), asyncio.Event()
    stop.set()
    await asyncio.wait_for(serve(bot, "tok", stop=stop), timeout=2)
    assert bot.close_calls == 1


# ---- graceful shutdown of the real bot -----------------------------------------------------

async def test_stop_loop_waits_for_an_in_flight_iteration():
    events = []

    @tasks.loop(seconds=60)
    async def slow():
        events.append("start")
        try:
            await asyncio.sleep(30)
        finally:
            events.append("cleanup")

    slow.start()
    await asyncio.sleep(0.05)
    task = slow.get_task()
    await stop_loop(slow)
    assert events == ["start", "cleanup"] and task.done() and not slow.is_running()
    await stop_loop(slow)   # stopping twice (or a never-started loop) is harmless


async def test_close_stops_loops_dashboard_and_database(make_config):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    b = await started(FAQBot(make_config(database_path="t.db", dashboard_token="x" * 20, dashboard_port=port)))
    assert b.dashboard.server is not None
    loop_tasks = [loop.get_task() for loop in (b.tickets._cleanup_expired, b.tickets._sla_check,
                                               b.maintenance._backup_database, b.transcript_cleanup._purge_loop)]
    await b.db.mark_sla_alerted(123)
    db_file = b.config.database_file

    await b.close()

    assert all(t.done() for t in loop_tasks)            # awaited, not merely cancelled
    assert not b.cogs and b.db is None and b.dashboard.server is None
    with socket.socket() as probe:                       # the aiohttp runner released the port
        assert probe.connect_ex(("127.0.0.1", port)) != 0
    with sqlite3.connect(db_file) as con:                # everything was flushed to disk
        assert con.execute("SELECT channel_id FROM sla_alerts").fetchall() == [(123,)]
    await b.close()                                      # a second close (second signal) is harmless


async def test_dry_run_checks_only_the_folders_it_writes(make_config, capsys, tmp_path, monkeypatch):
    """A read-only code directory is fine (the container and the systemd unit enforce one);
    a read-only database/transcripts/backups folder is not."""
    cfg = make_config(database_path="data/tickets.db")
    for folder in ("data", "transcripts", "backups"):   # as in the container: all three are volumes
        (tmp_path / folder).mkdir()
    real_access = main_module.os.access
    monkeypatch.setattr(main_module.os, "access",
                        lambda path, mode: False if Path(path) == tmp_path else real_access(path, mode))
    assert await dry_run(cfg) == 0            # root not writable, but nothing is written there
    out = capsys.readouterr().out
    assert "database folder writable" in out and "health endpoint disabled" in out
    monkeypatch.setattr(main_module.os, "access",
                        lambda path, mode: False if Path(path) == tmp_path / "data" else real_access(path, mode))
    assert await dry_run(cfg) == 1
    assert "database folder not writable" in capsys.readouterr().out


async def test_dry_run_flags_a_missing_folder_it_cannot_create(make_config, capsys, tmp_path, monkeypatch):
    real_access = main_module.os.access
    monkeypatch.setattr(main_module.os, "access",
                        lambda path, mode: False if Path(path) == tmp_path else real_access(path, mode))
    assert await dry_run(make_config()) == 1   # transcripts/ doesn't exist and its parent is read-only
    assert "transcripts folder not writable" in capsys.readouterr().out
