"""Configuration: .env loading, validation, and where runtime data lives."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Runtime data (tickets.db, transcripts/, backups/) lives in the project root, where
# bot.py used to be, so existing data is picked up unchanged after the refactor.
DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent


def _optional_int_set(name: str) -> frozenset[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return frozenset()
    try:
        return frozenset(int(part) for part in raw.split(",") if part.strip())
    except ValueError:
        raise RuntimeError(f"{name} must be a comma-separated list of channel IDs") from None


def _optional_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"Environment variable {name} must be an integer, got {raw!r}") from None


def _port(name: str, *, default: int | None) -> int | None:
    port = _optional_int(name)
    if port is None:
        return default
    if not 1 <= port <= 65535:
        raise RuntimeError(f"{name} must be between 1 and 65535, got {port}")
    return port


# Per-server IDs that only make sense together with GUILD_ID.
_GUILD_SCOPED_VARS = (
    "STAFF_ROLE_ID", "TICKETS_CATEGORY_ID", "ARCHIVE_CATEGORY_ID",
    "TRANSCRIPT_LOG_CHANNEL_ID", "SLA_ALERT_CHANNEL_ID", "FAQ_CHANNEL_IDS",
)

DEFAULT_HEALTH_PORT = 8080
_DISABLED = {"off", "0", "false", "no", "none", "disabled"}


def _health_port() -> int | None:
    """HEALTH_PORT: empty = 8080, "off" (or 0) = disabled, otherwise a port number."""
    if os.getenv("HEALTH_PORT", "").strip().lower() in _DISABLED:
        return None
    return _port("HEALTH_PORT", default=DEFAULT_HEALTH_PORT)


@dataclass(frozen=True)
class Config:
    bot_token: str
    # Everything from here to sla_alert_channel_id is OPTIONAL and applies to one server
    # only: GUILD_ID. Servers are normally configured with /setup (stored per server in
    # the database); these values are the fallback for GUILD_ID until it runs /setup.
    guild_id: int | None = None
    staff_role_id: int | None = None
    tickets_category_id: int | None = None
    # Empty set = listen in every channel that @everyone can see.
    faq_channel_ids: frozenset[int] = field(default_factory=frozenset)
    # Seconds before the same FAQ can be auto-answered again in the same channel.
    faq_cooldown: float = 30.0
    # Channel that receives a summary + .txt transcript of every closed ticket. None = disabled.
    transcript_log_channel_id: int | None = None
    # Category that archived (closed) tickets are moved into, so they don't use up the
    # 50-channel limit of the tickets category during their 48-hour retention. None = archive in place.
    archive_category_id: int | None = None
    # SQLite file for ticket analytics. Relative paths are resolved against data_dir.
    database_path: str = "tickets.db"
    # Where SLA warnings go. None = post in the ticket channel itself.
    sla_alert_channel_id: int | None = None
    # Metrics dashboard. It only starts when a token is set, so it never serves data unauthenticated.
    dashboard_token: str = ""
    dashboard_host: str = "127.0.0.1"  # loopback by default; set 0.0.0.0 only behind a firewall/HTTPS proxy
    dashboard_port: int = 8080
    # Unauthenticated liveness endpoint for supervisors (Docker HEALTHCHECK, systemd timer).
    # On by default; it reports status flags only, never ticket data. None = disabled.
    # When the dashboard runs on the same port, /health is served by the dashboard's server.
    health_host: str = "127.0.0.1"
    health_port: int | None = DEFAULT_HEALTH_PORT
    # Where tickets.db (if relative), transcripts/ and backups/ live.
    data_dir: Path = DEFAULT_DATA_DIR

    @property
    def health_on_dashboard(self) -> bool:
        """/health shares the dashboard's server when both are on the same port
        (two servers can't bind one port). The dashboard serves it without auth."""
        return bool(self.dashboard_token) and self.health_port == self.dashboard_port

    @property
    def database_file(self) -> Path:
        path = Path(self.database_path)
        return path if path.is_absolute() else self.data_dir / path

    @property
    def transcripts_dir(self) -> Path:
        return self.data_dir / "transcripts"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @classmethod
    def from_env(cls) -> Config:
        load_dotenv()
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("Missing required environment variable: BOT_TOKEN")
        try:
            cooldown = float(os.getenv("FAQ_COOLDOWN_SECONDS", "30"))
        except ValueError:
            raise RuntimeError("FAQ_COOLDOWN_SECONDS must be a number") from None
        if cooldown < 0:
            raise RuntimeError("FAQ_COOLDOWN_SECONDS must be >= 0")
        guild_id = _optional_int("GUILD_ID")
        if guild_id is None:
            stray = [name for name in _GUILD_SCOPED_VARS if os.getenv(name, "").strip()]
            if stray:
                raise RuntimeError(
                    f"{', '.join(stray)} only apply to GUILD_ID, which isn't set. Set GUILD_ID, "
                    "or remove them and run /setup in each server instead."
                )
        return cls(
            bot_token=token,
            guild_id=guild_id,
            staff_role_id=_optional_int("STAFF_ROLE_ID"),
            tickets_category_id=_optional_int("TICKETS_CATEGORY_ID"),
            faq_channel_ids=_optional_int_set("FAQ_CHANNEL_IDS"),
            faq_cooldown=cooldown,
            transcript_log_channel_id=_optional_int("TRANSCRIPT_LOG_CHANNEL_ID"),
            archive_category_id=_optional_int("ARCHIVE_CATEGORY_ID"),
            database_path=os.getenv("DATABASE_PATH", "").strip() or "tickets.db",
            sla_alert_channel_id=_optional_int("SLA_ALERT_CHANNEL_ID"),
            dashboard_token=os.getenv("DASHBOARD_AUTH_TOKEN", "").strip(),
            dashboard_host=os.getenv("DASHBOARD_HOST", "").strip() or "127.0.0.1",
            dashboard_port=_port("DASHBOARD_PORT", default=8080),
            health_host=os.getenv("HEALTH_HOST", "").strip() or "127.0.0.1",
            health_port=_health_port(),
        )
