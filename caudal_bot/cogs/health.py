"""Liveness endpoint for process supervisors: ``GET /health``.

It needs no auth token (so Docker's HEALTHCHECK, a systemd timer or a quick
``Invoke-RestMethod`` can probe it), works when the dashboard is disabled, and returns
only status flags. It's on by default at 127.0.0.1:8080 (HEALTH_HOST / HEALTH_PORT;
``HEALTH_PORT=off`` disables it). If the dashboard runs on the same port, the dashboard's
server answers /health instead (see Config.health_on_dashboard), since two servers
can't share one port.
"""

from __future__ import annotations

import logging
import math
import time
from typing import TYPE_CHECKING

from aiohttp import web
from discord.ext import commands

if TYPE_CHECKING:
    from ..main import FAQBot

log = logging.getLogger("faq_bot")

_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def health_snapshot(bot: FAQBot, started_at: float) -> tuple[int, dict]:
    """(HTTP status, body). 200 only when the gateway is connected, the database is open
    and every background loop is running; otherwise 503 with what's wrong."""
    latency = bot.latency
    if bot.is_ready() and not bot.is_closed() and math.isfinite(latency):
        gateway = "connected"
    elif not bot.is_ready() and not bot.is_closed():
        gateway = "connecting"
    else:
        gateway = "disconnected"
    loops = {
        "cleanup": bot.tickets._cleanup_expired.is_running(),
        "sla": bot.tickets._sla_check.is_running(),
        "backup": bot.maintenance._backup_database.is_running(),
        "transcript_purge": bot.transcript_cleanup._purge_loop.is_running(),
    }
    database = "connected" if bot.db is not None else "unavailable"
    if gateway == "connected" and database == "connected" and all(loops.values()):
        status = "ok"
    elif gateway == "connecting":
        status = "starting"
    else:
        status = "degraded"
    body = {
        "status": status,
        "database": database,
        "gateway": gateway,
        "latency_ms": round(latency * 1000) if math.isfinite(latency) else None,
        "loops": loops,
        "uptime_s": int(time.monotonic() - started_at),
    }
    return (200 if status == "ok" else 503), body


class HealthCog(commands.Cog, name="Health"):
    def __init__(self, bot: FAQBot) -> None:
        self.bot = bot
        self._runner: web.AppRunner | None = None
        self._started_at = time.monotonic()

    async def cog_load(self) -> None:
        await self.start()

    async def cog_unload(self) -> None:
        await self.stop()

    @property
    def running(self) -> bool:
        """True if this cog runs its own server (False when disabled or on the dashboard's)."""
        return self._runner is not None

    async def start(self) -> None:
        cfg = self.bot.config
        if cfg.health_port is None:
            log.info("Health endpoint disabled (HEALTH_PORT=off)")
            return
        if cfg.health_on_dashboard:
            # The dashboard cog loads first and routes /health itself.
            if self.bot.dashboard.server is not None:
                log.info("Health endpoint at http://%s:%s/health (on the dashboard's server)",
                         cfg.dashboard_host, cfg.dashboard_port)
            return
        app = web.Application()
        app.router.add_get("/health", self.handle)
        # access_log=None: a probe every 30s would otherwise flood the logs.
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, cfg.health_host, cfg.health_port).start()
        except OSError:
            # A busy port must not stop the bot; the supervisor will just report it unhealthy.
            log.exception("Could not start health endpoint on %s:%s", cfg.health_host, cfg.health_port)
            await runner.cleanup()
            return
        self._runner = runner
        log.info("Health endpoint at http://%s:%s/health", cfg.health_host, cfg.health_port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def handle(self, _request: web.Request) -> web.Response:
        """GET /health, on this cog's own server or the dashboard's."""
        status, body = health_snapshot(self.bot, self._started_at)
        return web.json_response(body, status=status, headers=_HEADERS)
