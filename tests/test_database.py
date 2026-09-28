"""SQLite layer: schema creation/migration, write persistence, queries (stats, leaderboard,
SLA compliance), the read-only schema check, and backups with integrity checks and retention."""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import time
from unittest.mock import AsyncMock

import pytest

import helpers as h
from caudal_bot import database
from caudal_bot.database import TicketDB, inspect_schema, timeframe_since, verify_schema

NOW = h.now().replace(microsecond=0)
ago = lambda **kw: NOW - dt.timedelta(**kw)


async def add(db, channel_id, *, staff=2, opened_min=120, response_min=5, closed_min=60, opener=1):
    await db.upsert_metrics(
        channel_id=channel_id, opener_id=opener, staff_id=staff, created_at=ago(minutes=opened_min),
        first_response_at=None if response_min is None else ago(minutes=opened_min) + dt.timedelta(minutes=response_min),
        closed_at=ago(minutes=closed_min))


def columns(path, table="ticket_metrics") -> list[str]:
    with sqlite3.connect(path) as con:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def tables(path) -> set[str]:
    with sqlite3.connect(path) as con:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master")}


# ---- schema & migration ----------------------------------------------------------------

async def test_open_creates_the_schema(tmp_path):
    db = await TicketDB.open(tmp_path / "t.db")
    await db.close()
    assert {"ticket_metrics", "sla_alerts", "idx_metrics_staff_closed"} <= tables(tmp_path / "t.db")
    assert "csat_score" not in columns(tmp_path / "t.db")          # the survey is gone


async def test_reopening_is_idempotent_and_keeps_data(tmp_path):
    db = await TicketDB.open(tmp_path / "t.db")
    await add(db, 100)
    await db.mark_sla_alerted(100)
    await db.close()
    db = await TicketDB.open(tmp_path / "t.db")    # schema script runs again: must be a no-op
    stats = await db.staff_stats(2)
    assert (stats.tickets, stats.avg_response_seconds) == (1, 300) and await db.sla_alerted(100)
    await db.close()


async def test_older_database_is_migrated_in_place(tmp_path):
    """A database from before the SLA table and index existed is upgraded without data loss."""
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as con:
        con.execute("""CREATE TABLE ticket_metrics (
            ticket_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL UNIQUE, opener_id INTEGER NOT NULL,
            staff_id INTEGER, created_at INTEGER NOT NULL, first_response_at INTEGER,
            closed_at INTEGER NOT NULL, csat_score INTEGER CHECK (csat_score BETWEEN 1 AND 5))""")
        con.execute("INSERT INTO ticket_metrics VALUES (1, 500, 1, 2, 0, 60, 3600, 4)")
    assert "sla_alerts" not in tables(path)
    db = await TicketDB.open(path)
    stats = await db.staff_stats(2)
    assert (stats.tickets, stats.avg_response_seconds, stats.avg_resolution_seconds) == (1, 60, 3600)
    await db.close()
    assert {"sla_alerts", "idx_metrics_staff_closed"} <= tables(path)
    assert await verify_schema(path) == []


async def test_legacy_csat_column_is_kept_but_never_touched(tmp_path):
    """Databases from the survey era keep their csat_score column and its votes; new
    writes (insert and update) work around it, and an extra column isn't schema drift."""
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as con:
        con.execute("""CREATE TABLE ticket_metrics (
            ticket_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL UNIQUE, opener_id INTEGER NOT NULL,
            staff_id INTEGER, created_at INTEGER NOT NULL, first_response_at INTEGER,
            closed_at INTEGER NOT NULL, csat_score INTEGER CHECK (csat_score BETWEEN 1 AND 5))""")
        con.execute("INSERT INTO ticket_metrics VALUES (1, 500, 1, 2, 0, 60, 3600, 4)")
    db = await TicketDB.open(path)
    await add(db, 500, staff=None)                       # update an old row
    await add(db, 501)                                   # insert a new one
    assert (await db.staff_stats(2)).tickets == 2
    assert set((await db.recent_tickets())[0]) == {
        "channel_id", "opener_id", "staff_id", "created_at", "first_response_at", "closed_at"}
    await db.close()
    with sqlite3.connect(path) as con:
        rows = con.execute("SELECT channel_id, csat_score FROM ticket_metrics ORDER BY channel_id").fetchall()
    assert rows == [(500, 4), (501, None)]               # historical vote preserved, nothing new written
    assert await verify_schema(path) == []


# ---- read-only schema verification (used by --dry-run) ------------------------------------

async def test_verify_accepts_a_missing_file_without_creating_it(tmp_path):
    assert await verify_schema(tmp_path / "absent.db") == []
    assert not (tmp_path / "absent.db").exists()


async def test_verify_accepts_a_current_database_and_never_writes(tmp_path):
    path = tmp_path / "t.db"
    db = await TicketDB.open(path)
    await add(db, 1)
    await db.close()
    before = (path.stat().st_mtime_ns, path.read_bytes())
    assert await verify_schema(path) == []
    assert (path.stat().st_mtime_ns, path.read_bytes()) == before


@pytest.mark.parametrize("ddl, expected", [
    ("CREATE TABLE ticket_metrics (ticket_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL UNIQUE)",
     "missing column opener_id"),
    ("CREATE TABLE ticket_metrics (ticket_id INTEGER PRIMARY KEY, channel_id TEXT NOT NULL UNIQUE, "
     "opener_id INTEGER NOT NULL, staff_id INTEGER, created_at INTEGER NOT NULL, first_response_at INTEGER, "
     "closed_at INTEGER NOT NULL)", "ticket_metrics.channel_id: expected"),
])
async def test_verify_reports_schema_drift(tmp_path, ddl, expected):
    path = tmp_path / "drift.db"
    with sqlite3.connect(path) as con:
        con.execute(ddl)
    problems, to_create = await inspect_schema(path)
    assert any(expected in p for p in problems), problems
    assert "table sla_alerts" in to_create          # missing tables are added on start, not errors


async def test_tables_added_by_newer_versions_are_not_problems(tmp_path):
    """A database from before bot_settings/archived_tickets passes the dry run; open() adds them."""
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as con:
        con.execute("""CREATE TABLE ticket_metrics (
            ticket_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL UNIQUE, opener_id INTEGER NOT NULL,
            staff_id INTEGER, created_at INTEGER NOT NULL, first_response_at INTEGER, closed_at INTEGER NOT NULL)""")
    problems, to_create = await inspect_schema(path)
    assert problems == [] and {"table bot_settings", "table archived_tickets"} <= set(to_create)
    await (await TicketDB.open(path)).close()
    assert {"bot_settings", "archived_tickets"} <= tables(path)
    assert await inspect_schema(path) == ([], [])


async def test_verify_reports_a_file_that_isnt_a_database(tmp_path):
    path = tmp_path / "junk.db"
    path.write_bytes(b"definitely not sqlite" * 100)
    problems = await verify_schema(path)
    assert problems and ("cannot open" in problems[0] or "integrity" in problems[0])


# ---- writes & persistence ---------------------------------------------------------------

async def test_writes_are_durable_immediately(tmp_path):
    db = await TicketDB.open(tmp_path / "t.db")
    await add(db, 100, staff=4)
    # Read through a second, independent connection while the first is still open.
    with sqlite3.connect(tmp_path / "t.db") as con:
        assert con.execute("SELECT staff_id FROM ticket_metrics WHERE channel_id = 100").fetchone() == (4,)
    await db.close()


async def test_close_flushes_and_checkpoints(tmp_path):
    path = tmp_path / "t.db"
    db = await TicketDB.open(path)
    await add(db, 100)
    await db.close()
    assert not (tmp_path / "t.db-wal").exists() or (tmp_path / "t.db-wal").stat().st_size == 0
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT count(*) FROM ticket_metrics").fetchone() == (1,)


async def test_later_snapshots_never_erase_known_values(db):
    await add(db, 100)
    # A delete-time upsert that can't see the claim or the first response must keep both.
    await db.upsert_metrics(channel_id=100, opener_id=1, staff_id=None, created_at=ago(minutes=120),
                            first_response_at=None, closed_at=ago(minutes=1))
    stats = await db.staff_stats(2)
    assert (stats.tickets, stats.avg_response_seconds) == (1, 300)
    (row,) = await db.recent_tickets()
    assert row["closed_at"] == int(ago(minutes=1).timestamp())   # closed_at does update


async def test_sla_alert_marker_is_claimed_once(db):
    assert await db.mark_sla_alerted(7) is True
    assert await db.mark_sla_alerted(7) is False
    assert await db.sla_alerted(7) and not await db.sla_alerted(8)


async def test_settings_round_trip_and_survive_reopening(tmp_path):
    db = await TicketDB.open(tmp_path / "t.db")
    assert await db.get_setting("staff_role_id") is None
    await db.set_setting("staff_role_id", "123")
    await db.set_setting("staff_role_id", "456")          # overwrite, not duplicate
    await db.close()
    db = await TicketDB.open(tmp_path / "t.db")
    assert await db.get_setting("staff_role_id") == "456"
    await db.close()


async def test_archive_deadlines(db):
    await db.set_archive_deadline(1, 100)
    await db.set_archive_deadline(2, 200)
    await db.set_archive_deadline(1, 150)                   # re-closed: new deadline
    assert await db.archive_deadlines() == {1: 150, 2: 200}
    await db.clear_archive_deadline(1)
    await db.clear_archive_deadline(999)                    # unknown: a no-op
    assert await db.archive_deadlines() == {2: 200}


# ---- queries ------------------------------------------------------------------------------

async def test_staff_stats_and_timeframes(db):
    await add(db, 1, response_min=4)
    await add(db, 2, response_min=8, opened_min=180)
    await add(db, 3, response_min=None, closed_min=40 * 24 * 60, opened_min=40 * 24 * 60 + 60)
    await add(db, 4, staff=None)                         # unhandled: never counted
    stats = await db.staff_stats(2)
    assert stats.tickets == 3
    assert stats.avg_response_seconds == 360             # tickets without a staff reply are excluded
    assert stats.avg_resolution_seconds == 4800          # (60 + 120 + 60 min) / 3
    assert (stats.sla_met, stats.sla_eligible) == (2, 3)  # the unanswered hour-long ticket is a miss
    assert (await db.staff_stats(2, timeframe_since("Last 30 Days"))).tickets == 2
    nobody = await db.staff_stats(77)
    assert (nobody.tickets, nobody.avg_response_seconds, nobody.avg_resolution_seconds) == (0, None, None)
    assert (nobody.sla_met, nobody.sla_eligible) == (0, 0)


async def test_leaderboard_orders_by_volume_then_response_speed(db):
    rows = [(2, 10), (2, 10), (2, 10), (3, 5), (3, 5), (3, 5), (5, None), (5, None), (5, None), (4, 1)]
    for cid, (staff, response) in enumerate(rows):
        await add(db, cid, staff=staff, response_min=response)
    board = await db.leaderboard()
    # Ties on volume go to the faster responder; never responding ranks last.
    assert [(s.staff_id, s.tickets) for s in board] == [(3, 3), (2, 3), (5, 3), (4, 1)]
    assert len(await db.leaderboard(limit=2)) == 2


@pytest.mark.parametrize("rows, expected", [
    ([dict(response_min=5)], (1, 1)),                                   # answered in time
    ([dict(response_min=40)], (0, 1)),                                  # answered late
    ([dict(response_min=None, opened_min=70, closed_min=60)], (0, 0)),  # self-resolved in 10 min: excluded
    ([dict(response_min=None, opened_min=180, closed_min=60)], (0, 1)),  # ignored for 2h: a miss
    ([dict(response_min=15)], (1, 1)),                                   # exactly on the threshold counts
    ([], (0, 0)),
])
async def test_sla_compliance(db, rows, expected):
    for i, row in enumerate(rows):
        await add(db, i, **row)
    assert await db.sla_compliance() == expected


async def test_recent_tickets_newest_first(db):
    await add(db, 1, closed_min=90)
    await add(db, 2, closed_min=10)
    assert [r["channel_id"] for r in await db.recent_tickets(limit=5)] == [2, 1]


# ---- backups --------------------------------------------------------------------------------

@pytest.fixture
async def maint(make_config, tmp_path):
    from caudal_bot.main import FAQBot
    bot = FAQBot(make_config())
    bot.db = await TicketDB.open(tmp_path / "live.db")
    await add(bot.db, 1, staff=6)
    yield bot.maintenance, bot.config.backups_dir
    await bot.db.close()


async def test_backup_is_a_verified_snapshot(maint):
    m, folder = maint
    path = await m.run_backup()
    assert path.parent == folder and path.name.startswith("tickets_backup_") and path.suffix == ".db"
    with sqlite3.connect(path) as con:
        assert con.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert con.execute("SELECT staff_id FROM ticket_metrics").fetchone() == (6,)
    assert not list(folder.glob("*.partial"))


async def test_backup_skips_when_one_is_recent(maint):
    m, _ = maint
    assert await m.run_backup() is not None
    assert await m.run_backup() is None                 # a restart within 23h doesn't pile up copies


async def test_retention_deletes_only_our_expired_backups(maint):
    m, folder = maint
    folder.mkdir()
    stamp = lambda days: (NOW - dt.timedelta(days=days)).strftime("%Y%m%d_%H%M%S")
    keep = [folder / f"tickets_backup_{stamp(13)}.db", folder / "my_manual_copy.db", folder / "tickets_backup_notadate.db"]
    gone = folder / f"tickets_backup_{stamp(15)}.db"
    stale_partial = folder / "tickets_backup_20200101_000000.db.partial"
    for f in (*keep, gone, stale_partial):
        f.write_bytes(b"x")
    os.utime(stale_partial, (time.time() - 7200,) * 2)
    created = await m.run_backup(force=True)
    left = {p.name for p in folder.iterdir()}
    assert gone.name not in left and stale_partial.name not in left
    assert {p.name for p in keep} | {created.name} <= left


async def test_failed_backup_leaves_nothing_behind(maint, monkeypatch):
    m, folder = maint
    monkeypatch.setattr(m.bot.db, "backup_to", AsyncMock(side_effect=RuntimeError("disk full")))
    with pytest.raises(RuntimeError):
        await m.run_backup(force=True)
    assert not list(folder.glob("*"))
    await m._backup_database()   # the loop body logs the failure instead of dying


async def test_backup_without_a_database_is_skipped(make_config):
    from caudal_bot.main import FAQBot
    assert await FAQBot(make_config()).maintenance.run_backup() is None


def test_backup_names_round_trip():
    ts = dt.datetime(2026, 9, 25, 16, 18, 41, tzinfo=dt.timezone.utc)
    from pathlib import Path
    assert database.backup_timestamp(Path("tickets_backup_20260925_161841.db")) == ts
    assert database.backup_timestamp(Path("tickets_backup_20260925_161841.db.partial")) is None
    assert database.backup_timestamp(Path("other.db")) is None
