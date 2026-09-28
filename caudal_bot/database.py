"""Analytics database (SQLite via aiosqlite): schema, queries, and the daily backup task."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
import discord
from discord.ext import commands, tasks

from .common import SLA_THRESHOLD_SECONDS, format_duration, stop_loop

if TYPE_CHECKING:
    from .main import FAQBot

log = logging.getLogger("faq_bot")


_SCHEMA = """
-- Databases created before the survey was removed also have a csat_score column. It is
-- left in place (and its data untouched); nothing reads or writes it any more.
CREATE TABLE IF NOT EXISTS ticket_metrics (
    ticket_id          INTEGER PRIMARY KEY,
    channel_id         INTEGER NOT NULL UNIQUE,
    opener_id          INTEGER NOT NULL,
    staff_id           INTEGER,
    created_at         INTEGER NOT NULL,
    first_response_at  INTEGER,
    closed_at          INTEGER NOT NULL,
    guild_id           INTEGER
);
CREATE INDEX IF NOT EXISTS idx_metrics_staff_closed ON ticket_metrics (staff_id, closed_at);

-- One row per ticket that has had its SLA warning, so it's only ever sent once.
CREATE TABLE IF NOT EXISTS sla_alerts (
    channel_id  INTEGER PRIMARY KEY,
    alerted_at  INTEGER NOT NULL
);

-- Bot-wide settings. Before per-server settings existed, /set-staff-role stored its
-- role here as "staff_role_id"; that value is still honoured for GUILD_ID (see guild_settings.py).
CREATE TABLE IF NOT EXISTS bot_settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

-- Per-server configuration written by /setup, /set-staff-role and /set-faq-channels.
-- NULL = not configured; faq_channel_ids NULL = answer in every public channel.
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id                   INTEGER PRIMARY KEY,
    staff_role_id              INTEGER,
    tickets_category_id        INTEGER,
    archive_category_id        INTEGER,
    transcript_log_channel_id  INTEGER,
    sla_alert_channel_id       INTEGER,
    faq_channel_ids            TEXT
);

-- Closed tickets and when they're due for deletion. Kept here rather than in the channel
-- topic because Discord allows only 2 name/topic edits per channel every 10 minutes.
CREATE TABLE IF NOT EXISTS archived_tickets (
    channel_id  INTEGER PRIMARY KEY,
    delete_at   INTEGER NOT NULL
);
"""


# Columns added to existing tables after release: (table, column, type). TicketDB.open
# adds any that are missing (CREATE TABLE IF NOT EXISTS never alters an existing table).
_ADDED_COLUMNS = (("ticket_metrics", "guild_id", "INTEGER"),)

GUILD_SETTING_COLUMNS = (
    "staff_role_id", "tickets_category_id", "archive_category_id",
    "transcript_log_channel_id", "sla_alert_channel_id", "faq_channel_ids",
)

# Restricts a ticket_metrics query to one server; :guild NULL = every server.
_GUILD_FILTER = " AND (:guild IS NULL OR guild_id = :guild)"


@dataclass(frozen=True)
class StaffStats:
    staff_id: int
    tickets: int
    avg_response_seconds: float | None
    avg_resolution_seconds: float | None = None
    sla_met: int = 0
    sla_eligible: int = 0


def _epoch(value: dt.datetime | None) -> int | None:
    return int(value.timestamp()) if value else None


class TicketDB:
    """Thin async wrapper around one aiosqlite connection.

    aiosqlite runs every query on its own worker thread, so nothing here blocks the
    event loop (and with it the gateway heartbeat). Each write commits immediately,
    so a crash or restart never loses a recorded ticket.
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    @classmethod
    async def open(cls, path: str | os.PathLike) -> TicketDB:
        conn = await aiosqlite.connect(path)
        # WAL lets the leaderboard read while a close is writing; NORMAL is safe with WAL.
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.executescript(_SCHEMA)
        for table, column, kind in _ADDED_COLUMNS:
            async with conn.execute(f'PRAGMA table_info("{table}")') as cur:
                existing = {row[1] for row in await cur.fetchall()}
            if column not in existing:
                await conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {kind}')
                log.info("Database: added column %s.%s", table, column)
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        # Every write already commits; this is a belt-and-braces flush before closing.
        # Closing the last connection also checkpoints the WAL into tickets.db.
        await self._conn.commit()
        await self._conn.close()

    async def upsert_metrics(
        self,
        *,
        channel_id: int,
        opener_id: int,
        staff_id: int | None,
        created_at: dt.datetime,
        first_response_at: dt.datetime | None,
        closed_at: dt.datetime,
        guild_id: int | None = None,
    ) -> None:
        """Insert or refresh a ticket's row. Called on close and again on delete.

        COALESCE keeps values we already have when the new snapshot lacks them, so a
        later close/delete that can't see the claimer or first response never wipes them.
        """
        await self._conn.execute(
            """
            INSERT INTO ticket_metrics
                (channel_id, opener_id, staff_id, created_at, first_response_at, closed_at, guild_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (channel_id) DO UPDATE SET
                staff_id          = COALESCE(excluded.staff_id, ticket_metrics.staff_id),
                first_response_at = COALESCE(excluded.first_response_at, ticket_metrics.first_response_at),
                closed_at         = excluded.closed_at,
                guild_id          = COALESCE(excluded.guild_id, ticket_metrics.guild_id)
            """,
            (channel_id, opener_id, staff_id, _epoch(created_at), _epoch(first_response_at), _epoch(closed_at),
             guild_id),
        )
        await self._conn.commit()

    # Columns in StaffStats order. The SLA pair uses the same rules as sla_compliance.
    _STATS_SELECT = """
        SELECT staff_id,
               COUNT(*),
               AVG(CASE WHEN first_response_at IS NOT NULL THEN first_response_at - created_at END),
               AVG(closed_at - created_at),
               COALESCE(SUM(first_response_at IS NOT NULL AND first_response_at - created_at <= :sla), 0),
               COALESCE(SUM(first_response_at IS NOT NULL OR closed_at - created_at > :sla), 0)
        FROM ticket_metrics
        WHERE staff_id IS NOT NULL AND closed_at >= :since
    """ + _GUILD_FILTER

    # Every query below takes guild_id: the server to report on (None = all servers).

    async def staff_stats(self, staff_id: int, since: int = 0, guild_id: int | None = None) -> StaffStats:
        params = {"since": since, "sla": SLA_THRESHOLD_SECONDS, "staff": staff_id, "guild": guild_id}
        async with self._conn.execute(self._STATS_SELECT + " AND staff_id = :staff", params) as cur:
            row = await cur.fetchone()
        # An aggregate with no matching rows still returns one row, with staff_id NULL.
        return StaffStats(staff_id, *row[1:])

    async def leaderboard(self, since: int = 0, limit: int = 5, guild_id: int | None = None) -> list[StaffStats]:
        """Most tickets first; ties go to the faster average first response (none = slowest)."""
        query = self._STATS_SELECT + """
            GROUP BY staff_id
            ORDER BY COUNT(*) DESC, AVG(first_response_at - created_at) IS NULL,
                     AVG(first_response_at - created_at), staff_id
            LIMIT :limit
        """
        params = {"since": since, "sla": SLA_THRESHOLD_SECONDS, "limit": limit, "guild": guild_id}
        async with self._conn.execute(query, params) as cur:
            return [StaffStats(*row) for row in await cur.fetchall()]

    async def sla_alerted(self, channel_id: int) -> bool:
        async with self._conn.execute("SELECT 1 FROM sla_alerts WHERE channel_id = ?", (channel_id,)) as cur:
            return await cur.fetchone() is not None

    async def mark_sla_alerted(self, channel_id: int) -> bool:
        """Claim the one-time SLA alert for a ticket. False if it was already claimed."""
        cursor = await self._conn.execute(
            "INSERT OR IGNORE INTO sla_alerts (channel_id, alerted_at) VALUES (?, ?)",
            (channel_id, int(time.time())),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    # ---- runtime settings ----

    async def get_setting(self, key: str) -> str | None:
        async with self._conn.execute("SELECT value FROM bot_settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self._conn.execute(
            "INSERT INTO bot_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self._conn.commit()

    # ---- per-server settings ----

    async def all_guild_settings(self) -> dict[int, dict]:
        """{guild_id: {column: value}} for every configured server."""
        cols = ", ".join(GUILD_SETTING_COLUMNS)
        async with self._conn.execute(f"SELECT guild_id, {cols} FROM guild_settings") as cur:
            return {row[0]: dict(zip(GUILD_SETTING_COLUMNS, row[1:])) for row in await cur.fetchall()}

    async def save_guild_settings(self, guild_id: int, values: dict) -> None:
        """Write a server's full settings row (every column; None stores NULL)."""
        cols = ", ".join(GUILD_SETTING_COLUMNS)
        marks = ", ".join("?" for _ in GUILD_SETTING_COLUMNS)
        updates = ", ".join(f"{c} = excluded.{c}" for c in GUILD_SETTING_COLUMNS)
        await self._conn.execute(
            f"INSERT INTO guild_settings (guild_id, {cols}) VALUES (?, {marks}) "
            f"ON CONFLICT (guild_id) DO UPDATE SET {updates}",
            (guild_id, *(values.get(c) for c in GUILD_SETTING_COLUMNS)),
        )
        await self._conn.commit()

    async def assign_unowned_metrics(self, guild_id: int) -> int:
        """Tag rows recorded before metrics were per-server with ``guild_id``. Returns the count."""
        cursor = await self._conn.execute("UPDATE ticket_metrics SET guild_id = ? WHERE guild_id IS NULL", (guild_id,))
        await self._conn.commit()
        return cursor.rowcount

    # ---- archived-ticket deadlines ----

    async def archive_deadlines(self) -> dict[int, int]:
        async with self._conn.execute("SELECT channel_id, delete_at FROM archived_tickets") as cur:
            return {cid: at for cid, at in await cur.fetchall()}

    async def set_archive_deadline(self, channel_id: int, delete_at: int) -> None:
        await self._conn.execute(
            "INSERT INTO archived_tickets (channel_id, delete_at) VALUES (?, ?) "
            "ON CONFLICT (channel_id) DO UPDATE SET delete_at = excluded.delete_at",
            (channel_id, delete_at),
        )
        await self._conn.commit()

    async def clear_archive_deadline(self, channel_id: int) -> None:
        await self._conn.execute("DELETE FROM archived_tickets WHERE channel_id = ?", (channel_id,))
        await self._conn.commit()

    # ---- dashboard queries (read-only; no schema changes) ----

    async def sla_compliance(
        self, since: int = 0, threshold: int = SLA_THRESHOLD_SECONDS, guild_id: int | None = None
    ) -> tuple[int, int]:
        """(met, eligible). Met = first staff response within the threshold. Tickets
        closed within the threshold with no staff response (self-resolved) aren't
        counted either way; unanswered tickets that stayed open longer are misses."""
        async with self._conn.execute(
            """
            SELECT COALESCE(SUM(first_response_at IS NOT NULL AND first_response_at - created_at <= :sla), 0),
                   COALESCE(SUM(first_response_at IS NOT NULL OR closed_at - created_at > :sla), 0)
            FROM ticket_metrics WHERE closed_at >= :since
            """ + _GUILD_FILTER,
            {"sla": threshold, "since": since, "guild": guild_id},
        ) as cur:
            met, eligible = await cur.fetchone()
        return int(met), int(eligible)

    async def period_summary(self, since: int = 0, guild_id: int | None = None) -> StaffStats:
        """Aggregate over all handled tickets in the period (staff_id field is unused: 0)."""
        params = {"since": since, "sla": SLA_THRESHOLD_SECONDS, "guild": guild_id}
        async with self._conn.execute(self._STATS_SELECT, params) as cur:
            row = await cur.fetchone()
        return StaffStats(0, *row[1:])

    async def recent_tickets(self, limit: int = 25, guild_id: int | None = None) -> list[dict]:
        async with self._conn.execute(
            """
            SELECT channel_id, opener_id, staff_id, created_at, first_response_at, closed_at
            FROM ticket_metrics WHERE 1""" + _GUILD_FILTER + """ ORDER BY closed_at DESC LIMIT :limit
            """,
            {"limit": limit, "guild": guild_id},
        ) as cur:
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in await cur.fetchall()]

    async def backup_to(self, path: Path) -> None:
        """Consistent online snapshot via SQLite's backup API (safe while the bot keeps writing)."""
        target = await aiosqlite.connect(path)
        try:
            await self._conn.backup(target)
            async with target.execute("PRAGMA quick_check") as cur:
                (result,) = await cur.fetchone()
            if result != "ok":
                raise RuntimeError(f"backup failed integrity check: {result}")
        finally:
            await target.close()



async def _table_layout(conn: aiosqlite.Connection) -> dict[str, list[tuple]]:
    """{table: [(column, type, notnull, pk), ...]} plus {"index:<name>": []} entries."""
    layout: dict[str, list[tuple]] = {}
    async with conn.execute("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'") as cur:
        objects = await cur.fetchall()
    for kind, name in objects:
        if kind == "table":
            async with conn.execute(f'PRAGMA table_info("{name}")') as cur:
                layout[name] = [(r[1], r[2].upper(), r[3], r[5]) for r in await cur.fetchall()]
        elif kind == "index":
            layout[f"index:{name}"] = []
    return layout


async def inspect_schema(path: Path) -> tuple[list[str], list[str]]:
    """(problems, to_create) for the database at ``path`` versus _SCHEMA. Read-only.

    A missing file is fine (it's created on first run), so only the schema SQL itself
    is validated then. An existing file is opened read-only and must pass SQLite's
    quick_check, and its existing tables must have every expected column. Tables and
    indexes it lacks aren't problems: TicketDB.open creates them (``to_create``), which
    is how a database from an older version picks up new tables.
    """
    async with aiosqlite.connect(":memory:") as mem:
        await mem.executescript(_SCHEMA)
        expected = await _table_layout(mem)
    if not path.exists():
        return [], []
    problems, to_create = [], []
    try:
        async with aiosqlite.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as conn:
            actual = await _table_layout(conn)
            async with conn.execute("PRAGMA quick_check") as cur:
                (check,) = await cur.fetchone()
    except Exception as exc:
        return [f"cannot open {path.name} read-only: {exc}"], []
    if check != "ok":
        problems.append(f"integrity check failed: {check}")
    for name, columns in expected.items():
        if name not in actual:
            to_create.append(f"{'index' if name.startswith('index:') else 'table'} {name.removeprefix('index:')}")
            continue
        have = {c[0]: c for c in actual[name]}
        added_later = {column for table, column, _ in _ADDED_COLUMNS if table == name}
        for col in columns:
            if col[0] not in have and col[0] in added_later:
                to_create.append(f"column {name}.{col[0]}")
            elif col[0] not in have:
                problems.append(f"{name}: missing column {col[0]}")
            elif have[col[0]] != col:
                problems.append(f"{name}.{col[0]}: expected {col[1:]} got {have[col[0]][1:]}")
    return problems, to_create


async def verify_schema(path: Path) -> list[str]:
    """Problems with the database at ``path`` (see inspect_schema); [] means OK."""
    return (await inspect_schema(path))[0]


def format_avg_response(seconds: float | None) -> str:
    return format_duration(dt.timedelta(seconds=seconds)) if seconds is not None else "—"


LEADERBOARD_TIMEFRAMES = {"Last 7 Days": 7, "Last 30 Days": 30, "All Time": None}


def timeframe_since(timeframe: str, now: float | None = None) -> int:
    days = LEADERBOARD_TIMEFRAMES.get(timeframe)
    return 0 if days is None else int((now or time.time()) - days * 86400)


BACKUP_RETENTION_DAYS = 14
# A restart re-runs the loop immediately; skip if a backup this recent already exists,
# so a crash loop can't fill the disk with near-identical snapshots.
BACKUP_MIN_GAP = dt.timedelta(hours=23)
_BACKUP_PREFIX = "tickets_backup_"
_BACKUP_TS = "%Y%m%d_%H%M%S"


def backup_timestamp(path: Path) -> dt.datetime | None:
    """Timestamp encoded in a backup's filename, or None if it isn't one of ours."""
    if not (path.name.startswith(_BACKUP_PREFIX) and path.suffix == ".db"):
        return None
    try:
        return dt.datetime.strptime(path.stem[len(_BACKUP_PREFIX):], _BACKUP_TS).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def list_backups(directory: Path) -> list[tuple[dt.datetime, Path]]:
    if not directory.is_dir():
        return []
    found = ((backup_timestamp(p), p) for p in directory.iterdir())
    return sorted((ts, p) for ts, p in found if ts is not None)


def purge_old_backups(
    directory: Path, now: dt.datetime, retention_days: int = BACKUP_RETENTION_DAYS
) -> list[Path]:
    """Delete our backups older than the retention window. Files we didn't name are never touched."""
    cutoff = now - dt.timedelta(days=retention_days)
    removed = []
    for ts, path in list_backups(directory):
        if ts < cutoff:
            path.unlink(missing_ok=True)
            removed.append(path)
    # Leftovers from a backup interrupted mid-write.
    if directory.is_dir():
        for partial in directory.glob(f"{_BACKUP_PREFIX}*.db.partial"):
            if dt.datetime.fromtimestamp(partial.stat().st_mtime, dt.timezone.utc) < now - dt.timedelta(hours=1):
                partial.unlink(missing_ok=True)
    return removed


class DatabaseMaintenance(commands.Cog, name="Database"):
    """Daily online backup of tickets.db into Config.backups_dir, with 14-day retention."""

    def __init__(self, bot: FAQBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self._backup_database.start()

    async def cog_unload(self) -> None:
        await stop_loop(self._backup_database)

    @tasks.loop(hours=24)
    async def _backup_database(self) -> None:
        try:
            await self.run_backup()
        except Exception:
            # Never let a failed backup end the loop; tomorrow's run tries again.
            log.exception("Database backup failed")

    @_backup_database.before_loop
    async def _before_backup(self) -> None:
        await self.bot.wait_until_ready()

    async def run_backup(self, *, force: bool = False) -> Path | None:
        """Snapshot tickets.db into backups/, then prune backups past the retention window.

        Returns the new backup's path, or None if skipped. The snapshot is written to a
        .partial file and renamed only after it passes an integrity check, so a crash
        mid-backup never leaves a corrupt file that looks valid.
        """
        if self.bot.db is None:
            log.warning("Skipping database backup: the database is not open")
            return None
        backups_dir = self.bot.config.backups_dir
        now = discord.utils.utcnow()
        backups = await asyncio.to_thread(list_backups, backups_dir)
        if not force and backups and now - backups[-1][0] < BACKUP_MIN_GAP:
            log.info("Skipping database backup: the latest (%s) is under 23 hours old", backups[-1][1].name)
            created = None
        else:
            await asyncio.to_thread(backups_dir.mkdir, exist_ok=True)
            final = backups_dir / f"{_BACKUP_PREFIX}{now.strftime(_BACKUP_TS)}.db"
            partial = final.with_name(final.name + ".partial")
            try:
                await self.bot.db.backup_to(partial)
                await asyncio.to_thread(os.replace, partial, final)
            except BaseException:
                await asyncio.to_thread(partial.unlink, missing_ok=True)
                raise
            size_kb = (await asyncio.to_thread(final.stat)).st_size / 1024
            log.info("Database backup created: %s (%.1f KB)", final.name, size_kb)
            created = final
        removed = await asyncio.to_thread(purge_old_backups, backups_dir, now)
        for path in removed:
            log.info("Removed expired backup (older than %d days): %s", BACKUP_RETENTION_DAYS, path.name)
        return created
