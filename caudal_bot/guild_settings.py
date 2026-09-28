"""Per-server settings (staff role, categories, log channels, FAQ channels).

Each server's settings live in the guild_settings table and are cached here, so no
event ever waits on the database. Where they come from, per server:

1. its guild_settings row, once /setup, /set-staff-role or /set-faq-channels has run
   there. The row is then authoritative: NULL means "not configured".
2. otherwise, for GUILD_ID only: the IDs in .env (plus a staff role saved by
   /set-staff-role before settings were per-server, in bot_settings).
3. otherwise: nothing configured. Tickets still work; the tickets category is
   created on first use and the rest falls back (see TicketsCog).

The first change in a server copies its current effective settings (2 or 3) into its
row, so changing one setting never drops the .env fallback for the others.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .main import FAQBot

log = logging.getLogger("faq_bot")

# bot_settings key used by /set-staff-role before settings were per-server.
LEGACY_STAFF_ROLE_KEY = "staff_role_id"


@dataclass(frozen=True)
class GuildSettings:
    staff_role_id: int | None = None
    tickets_category_id: int | None = None
    archive_category_id: int | None = None
    transcript_log_channel_id: int | None = None
    sla_alert_channel_id: int | None = None
    # Empty = answer FAQs in every channel @everyone can see.
    faq_channel_ids: frozenset[int] = field(default_factory=frozenset)

    def to_row(self) -> dict:
        row = dataclasses.asdict(self)
        row["faq_channel_ids"] = ",".join(map(str, sorted(self.faq_channel_ids))) or None
        return row

    @classmethod
    def from_row(cls, row: dict) -> GuildSettings:
        raw = row.get("faq_channel_ids") or ""
        return cls(
            **{k: row.get(k) for k in ("staff_role_id", "tickets_category_id", "archive_category_id",
                                        "transcript_log_channel_id", "sla_alert_channel_id")},
            faq_channel_ids=frozenset(int(p) for p in raw.split(",") if p.strip().isdigit()),
        )


class GuildSettingsStore:
    def __init__(self, bot: FAQBot) -> None:
        self.bot = bot
        self._rows: dict[int, GuildSettings] = {}
        self._legacy_staff_role: int | None = None

    async def load(self) -> None:
        """Read every server's row (and the pre-per-server staff role) into the cache."""
        db = self.bot.db
        if db is None:
            return
        try:
            self._rows = {gid: GuildSettings.from_row(row) for gid, row in (await db.all_guild_settings()).items()}
            legacy = await db.get_setting(LEGACY_STAFF_ROLE_KEY)
        except Exception:
            log.exception("Could not read server settings; using .env values for GUILD_ID")
            return
        if legacy is not None and legacy.isdigit():
            self._legacy_staff_role = int(legacy)
        log.info("Loaded settings for %d server(s)", len(self._rows))

    def env_defaults(self) -> GuildSettings:
        """What .env configures for GUILD_ID."""
        cfg = self.bot.config
        return GuildSettings(
            staff_role_id=self._legacy_staff_role or cfg.staff_role_id,
            tickets_category_id=cfg.tickets_category_id,
            archive_category_id=cfg.archive_category_id,
            transcript_log_channel_id=cfg.transcript_log_channel_id,
            sla_alert_channel_id=cfg.sla_alert_channel_id,
            faq_channel_ids=cfg.faq_channel_ids,
        )

    def get(self, guild_id: int | None) -> GuildSettings:
        if guild_id in self._rows:
            return self._rows[guild_id]
        if guild_id is not None and guild_id == self.bot.config.guild_id:
            return self.env_defaults()
        return GuildSettings()

    def is_configured(self, guild_id: int) -> bool:
        """True once the server has its own row (i.e. an admin configured it here)."""
        return guild_id in self._rows

    async def update(self, guild_id: int, **changes) -> bool:
        """Apply ``changes`` to a server's settings. The cache always updates; returns
        False if they couldn't be saved (no database), i.e. they last until a restart."""
        new = dataclasses.replace(self.get(guild_id), **changes)
        self._rows[guild_id] = new
        if self.bot.db is None:
            log.warning("No database: settings for server %s last only until the bot restarts", guild_id)
            return False
        try:
            await self.bot.db.save_guild_settings(guild_id, new.to_row())
        except Exception:
            log.exception("Could not save settings for server %s", guild_id)
            return False
        return True
