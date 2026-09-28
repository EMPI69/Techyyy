"""Password-protected metrics dashboard (aiohttp on the bot's own event loop).

Serves /metrics and /transcripts/<channel_id>. Auth: HTTP Basic (token as password) or
?token=. Every response carries a no-script CSP and no-referrer, and aiohttp's access
log is disabled so tokens never reach log files.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hmac
import html
import logging
import time
import urllib.parse
from typing import TYPE_CHECKING

import discord
from aiohttp import web
from discord.ext import commands

from ..common import format_duration, ticket_owner_id
from ..database import StaffStats, format_avg_response
from ..transcripts.cleanup import find_transcript

if TYPE_CHECKING:
    from ..main import FAQBot

log = logging.getLogger("faq_bot")


# Scripts are banned outright; images only over https (Discord's CDN for avatars and
# attachments). no-referrer keeps a ?token= URL from leaking to that CDN.
_DASHBOARD_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; img-src https: data:; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
DASHBOARD_RANGES = {"7": ("Last 7 Days", 7), "30": ("Last 30 Days", 30), "all": ("All Time", None)}

_DASHBOARD_CSS = """
*{box-sizing:border-box}
body{margin:0;background:#1e1f22;color:#dbdee1;font:15px/1.45 "gg sans","Noto Sans","Helvetica Neue",Helvetica,Arial,sans-serif}
a{color:#00a8fc;text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 48px}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end;justify-content:space-between;margin-bottom:22px}
h1{margin:0;font-size:26px;color:#f2f3f5}.sub{color:#949ba4;font-size:13px}
.ranges a{display:inline-block;padding:6px 12px;border-radius:6px;background:#2b2d31;color:#dbdee1;margin-left:6px;font-size:13px}
.ranges a.on{background:#5865f2;color:#fff}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;margin-bottom:26px}
.card{background:#2b2d31;border-radius:10px;padding:16px 18px;border-top:3px solid var(--accent,#5865f2)}
.card .label{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.03em;color:#949ba4}
.card .value{font-size:30px;font-weight:700;color:#f2f3f5;margin-top:4px;font-variant-numeric:tabular-nums}
.card .hint{font-size:12px;color:#949ba4;margin-top:2px}
section{background:#2b2d31;border-radius:10px;padding:16px 18px;margin-bottom:22px}
h2{font-size:16px;margin:0 0 12px;color:#f2f3f5}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:14px;font-variant-numeric:tabular-nums}
th{text-align:left;font-size:12px;text-transform:uppercase;letter-spacing:.03em;color:#949ba4;padding:8px 10px;border-bottom:1px solid #3f4147;white-space:nowrap}
td{padding:9px 10px;border-bottom:1px solid #35373c;white-space:nowrap}
tr:last-child td{border-bottom:0}
.num{text-align:right}.muted{color:#949ba4}
.btn{display:inline-block;padding:3px 10px;border-radius:5px;background:#4e5058;color:#fff;font-size:12px;margin-right:4px}
.btn.primary{background:#5865f2}
.empty{color:#949ba4;padding:8px 0}
"""


def _pct(met: int, eligible: int) -> str:
    return f"{met / eligible * 100:.0f}%" if eligible else "—"


def render_dashboard(
    *,
    guild_name: str,
    range_key: str,
    token_query: str,
    open_count: int,
    avg_open_age: str,
    sla: tuple[int, int],
    avg_response: str,
    avg_resolution: str,
    handled: int,
    staff_rows: list[tuple[str, int, str, str, str]],
    recent_rows: list[dict],
    servers: list[tuple[int, str]] = (),
    guild_id: int | None = None,
) -> str:
    """Pure HTML render of the metrics page. All dynamic text is escaped here.

    ``servers`` ((id, name) pairs) adds a server switcher when there's more than one;
    ``guild_id`` is the one shown, and every link keeps it.
    """
    esc = html.escape
    label = DASHBOARD_RANGES[range_key][0]
    sep = "&amp;" if token_query else ""
    here = f"guild={guild_id}&amp;" if guild_id is not None else ""
    ranges = "".join(
        f'<a class="{"on" if k == range_key else ""}" href="/metrics?{esc(token_query)}{sep}{here}range={k}">{esc(v[0])}</a>'
        for k, v in DASHBOARD_RANGES.items()
    )
    if len(servers) > 1:
        ranges += '<br>' + "".join(
            f'<a class="{"on" if sid == guild_id else ""}" href="/metrics?{esc(token_query)}{sep}guild={sid}&amp;'
            f'range={range_key}">{esc(name)}</a>'
            for sid, name in servers
        )
    met, eligible = sla
    cards = [
        ("Open tickets", str(open_count), "in the tickets category right now", "#3ba55c"),
        ("Avg queue wait", avg_open_age, "average age of open tickets", "#faa61a"),
        ("SLA compliance", _pct(met, eligible), f"{met}/{eligible} answered within 15 min · {label}", "#5865f2"),
        ("Avg first response", avg_response, f"{handled} handled ticket(s) · {label}", "#eb459e"),
        ("Avg resolution", avg_resolution, f"opened → closed · {label}", "#fee75c"),
    ]
    cards_html = "".join(
        f'<div class="card" style="--accent:{accent}"><div class="label">{esc(t)}</div>'
        f'<div class="value">{esc(v)}</div><div class="hint">{esc(h)}</div></div>'
        for t, v, h, accent in cards
    )
    if staff_rows:
        staff_html = (
            '<div class="scroll"><table><tr><th>#</th><th>Staff</th><th class="num">Tickets</th>'
            '<th class="num">Avg first response</th><th class="num">Avg resolution</th>'
            '<th class="num">SLA met</th></tr>'
            + "".join(
                f'<tr><td class="muted">{i}</td><td>{esc(name)}</td><td class="num">{n}</td>'
                f'<td class="num">{esc(resp)}</td><td class="num">{esc(resolution)}</td>'
                f'<td class="num">{esc(sla_rate)}</td></tr>'
                for i, (name, n, resp, resolution, sla_rate) in enumerate(staff_rows, 1)
            )
            + "</table></div>"
        )
    else:
        staff_html = '<p class="empty">No handled tickets in this period yet.</p>'

    def links(url: str | None) -> str:
        if not url:
            return '<span class="muted">—</span>'
        joiner = "&amp;" if "?" in url else "?"
        return (f'<a class="btn primary" href="{esc(url)}">View</a>'
                f'<a class="btn" href="{esc(url)}{joiner}download=1">Download</a>')

    if recent_rows:
        recent_html = (
            '<div class="scroll"><table><tr><th>Closed</th><th>Ticket</th><th>Opener</th><th>Handled by</th>'
            '<th class="num">Response</th><th class="num">Resolution</th><th>Transcript</th></tr>'
            + "".join(
                f'<tr><td>{esc(r["closed"])}</td><td>{esc(r["ticket"])}</td><td>{esc(r["opener"])}</td>'
                f'<td>{esc(r["staff"])}</td><td class="num">{esc(r["response"])}</td>'
                f'<td class="num">{esc(r["resolution"])}</td>'
                f'<td>{links(r["transcript_url"])}</td></tr>'
                for r in recent_rows
            )
            + "</table></div>"
        )
    else:
        recent_html = '<p class="empty">No closed tickets recorded yet.</p>'

    now = discord.utils.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="referrer" content="no-referrer"><meta http-equiv="refresh" content="60">'
        f"<title>Ticket Metrics • {esc(guild_name)}</title><style>{_DASHBOARD_CSS}</style></head><body>"
        f'<div class="wrap"><header><div><h1>Ticket Metrics</h1><div class="sub">{esc(guild_name)} · '
        f"updated {now} · refreshes every 60s</div></div><nav class=\"ranges\">{ranges}</nav></header>"
        f'<div class="cards">{cards_html}</div>'
        f"<section><h2>Top staff · {esc(label)}</h2>{staff_html}</section>"
        f"<section><h2>Recent ticket activity</h2>{recent_html}</section>"
        "</div></body></html>"
    )


class Dashboard:
    """Password-protected metrics site, served by aiohttp on the bot's own event loop.

    Every handler is async and the DB work goes through aiosqlite's thread, so a page
    load never blocks the gateway heartbeat.
    """

    def __init__(self, bot: FAQBot, *, host: str, port: int, token: str) -> None:
        self.bot = bot
        self.host, self.port, self._token = host, port, token.encode()
        self._runner: web.AppRunner | None = None
        # Same port as the health endpoint: serve its /health here, without auth.
        self.serves_health = bot.config.health_on_dashboard

    # ---- lifecycle ----
    async def start(self) -> None:
        app = web.Application(middlewares=[self._security_headers, self._require_auth])
        app.router.add_get("/", self._root)
        app.router.add_get("/metrics", self._metrics)
        app.router.add_get(r"/transcripts/{channel_id:\d+}", self._transcript)
        if self.serves_health:
            app.router.add_get("/health", self.bot.health.handle)
        # access_log=None: aiohttp would otherwise log full URLs, including ?token=.
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.host, self.port).start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ---- middleware ----
    @web.middleware
    async def _security_headers(self, request: web.Request, handler):
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = exc
        response.headers.update(_DASHBOARD_HEADERS)
        if isinstance(response, web.HTTPException):
            raise response
        return response

    def _authorized(self, request: web.Request) -> bool:
        # HTTP Basic (any username, token as password) or ?token=. Constant-time compares.
        header = request.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                password = base64.b64decode(header[6:], validate=True).decode("utf-8").partition(":")[2]
            except (ValueError, UnicodeDecodeError):
                password = ""
            if password and hmac.compare_digest(password.encode(), self._token):
                return True
        query = request.query.get("token", "")
        return bool(query) and hmac.compare_digest(query.encode(), self._token)

    @web.middleware
    async def _require_auth(self, request: web.Request, handler):
        # /health reports status flags only, and supervisors probe it without credentials.
        if self.serves_health and request.path == "/health":
            return await handler(request)
        if not self._authorized(request):
            raise web.HTTPUnauthorized(
                text="Authentication required.",
                headers={"WWW-Authenticate": 'Basic realm="Ticket Dashboard", charset="UTF-8"'},
            )
        return await handler(request)

    @staticmethod
    def _token_query(request: web.Request) -> str:
        # If the viewer authenticated with ?token=, links must carry it; with Basic auth
        # the browser resends credentials itself, so links stay clean.
        token = request.query.get("token")
        return urllib.parse.urlencode({"token": token}) if token else ""

    # ---- handlers ----
    async def _root(self, request: web.Request) -> web.StreamResponse:
        query = self._token_query(request)
        raise web.HTTPFound("/metrics" + (f"?{query}" if query else ""))

    def _name(self, user_id: int | None) -> str:
        if user_id is None:
            return "—"
        user = self.bot.get_user(user_id)
        return user.name if user else f"User {user_id}"

    async def _metrics(self, request: web.Request) -> web.Response:
        range_key = request.query.get("range", "30")
        if range_key not in DASHBOARD_RANGES:
            range_key = "30"
        days = DASHBOARD_RANGES[range_key][1]
        since = 0 if days is None else int(time.time() - days * 86400)
        bot = self.bot
        # One server at a time: ?guild=<id>, else GUILD_ID, else the first server.
        guilds = sorted(bot.managed_guilds(), key=lambda g: g.name.lower())
        wanted = request.query.get("guild", "")
        guild = (next((g for g in guilds if str(g.id) == wanted), None)
                 or next((g for g in guilds if g.id == bot.config.guild_id), None)
                 or (guilds[0] if guilds else None))
        gid = guild.id if guild else None   # None (not connected yet) = every server's records

        # Live queue straight from the channel cache: no API calls.
        now = discord.utils.utcnow()
        category = bot.tickets_category(guild) if guild else None
        open_tickets = [
            ch for ch in (category.text_channels if category else [])
            if ticket_owner_id(ch) is not None and bot.tickets.delete_at(ch) is None
        ]
        ages = [(now - ch.created_at).total_seconds() for ch in open_tickets]
        avg_age = format_avg_response(sum(ages) / len(ages)) if ages else "—"

        db = bot.db
        if db is not None:
            sla = await db.sla_compliance(since, guild_id=gid)
            summary = await db.period_summary(since, guild_id=gid)
            board = await db.leaderboard(since, limit=10, guild_id=gid)
            recent = await db.recent_tickets(limit=25, guild_id=gid)
        else:
            sla, summary, board, recent = (0, 0), StaffStats(0, 0, None), [], []

        token_query = self._token_query(request)
        staff_rows = [
            (self._name(s.staff_id), s.tickets, format_avg_response(s.avg_response_seconds),
             format_avg_response(s.avg_resolution_seconds), _pct(s.sla_met, s.sla_eligible))
            for s in board
        ]
        recent_rows = []
        for r in recent:
            path = await asyncio.to_thread(find_transcript, bot.config.transcripts_dir, r["channel_id"])
            url = None
            if path is not None:
                url = f"/transcripts/{r['channel_id']}" + (f"?{token_query}" if token_query else "")
            created = dt.datetime.fromtimestamp(r["created_at"], dt.timezone.utc)
            closed = dt.datetime.fromtimestamp(r["closed_at"], dt.timezone.utc)
            first = r["first_response_at"]
            recent_rows.append({
                "closed": closed.strftime("%Y-%m-%d %H:%M"),
                "ticket": f"#{path.stem.split('__', 1)[1]}" if path else f"#{r['channel_id']}",
                "opener": self._name(r["opener_id"]),
                "staff": self._name(r["staff_id"]),
                "response": format_duration(dt.timedelta(seconds=first - r["created_at"])) if first else "—",
                "resolution": format_duration(closed - created),
                "transcript_url": url,
            })

        page = render_dashboard(
            guild_name=guild.name if guild else "Discord server",
            range_key=range_key,
            token_query=token_query,
            open_count=len(open_tickets),
            avg_open_age=avg_age,
            sla=sla,
            avg_response=format_avg_response(summary.avg_response_seconds),
            avg_resolution=format_avg_response(summary.avg_resolution_seconds) if db is not None else "Database unavailable",
            handled=summary.tickets,
            staff_rows=staff_rows,
            recent_rows=recent_rows,
            servers=[(g.id, g.name) for g in guilds],
            guild_id=gid,
        )
        return web.Response(text=page, content_type="text/html", charset="utf-8")

    async def _transcript(self, request: web.Request) -> web.StreamResponse:
        # The route regex only admits digits, so there is no path to traverse.
        path = await asyncio.to_thread(
            find_transcript, self.bot.config.transcripts_dir, int(request.match_info["channel_id"])
        )
        if path is None:
            raise web.HTTPNotFound(text="Transcript not found.")
        disposition = "attachment" if request.query.get("download") else "inline"
        return web.FileResponse(
            path,
            headers={
                "Content-Type": "text/html; charset=utf-8",
                "Content-Disposition": f'{disposition}; filename="transcript-{path.stem.split("__", 1)[-1]}.html"',
            },
        )


class DashboardCog(commands.Cog, name="Dashboard"):
    """Starts the web server when the cog loads and stops it on unload/shutdown."""

    def __init__(self, bot: FAQBot) -> None:
        self.bot = bot
        self.server: Dashboard | None = None

    async def cog_load(self) -> None:
        await self.start()

    async def cog_unload(self) -> None:
        await self.stop()

    async def start(self) -> None:
        cfg = self.bot.config
        if not cfg.dashboard_token:
            log.info("DASHBOARD_AUTH_TOKEN not set; metrics dashboard disabled")
            return
        if len(cfg.dashboard_token) < 16:
            log.warning("DASHBOARD_AUTH_TOKEN is short; use at least 16 random characters")
        dashboard = Dashboard(self.bot, host=cfg.dashboard_host, port=cfg.dashboard_port, token=cfg.dashboard_token)
        try:
            await dashboard.start()
        except OSError:
            # Port in use / bad host: the bot itself must still come up.
            log.exception("Could not start dashboard on %s:%s; continuing without it", cfg.dashboard_host, cfg.dashboard_port)
            await dashboard.stop()
            return
        self.server = dashboard
        log.info("Metrics dashboard at http://%s:%s/metrics", cfg.dashboard_host, cfg.dashboard_port)

    async def stop(self) -> None:
        if self.server is not None:
            await self.server.stop()
            self.server = None
