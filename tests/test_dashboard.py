"""Metrics dashboard over real HTTP on a loopback port: auth, security headers, content, transcripts."""

from __future__ import annotations

import base64
import socket
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import aiohttp
import discord
import pytest

import helpers as h
from caudal_bot.cogs.dashboard import _DASHBOARD_HEADERS, render_dashboard
from caudal_bot.transcripts.cleanup import save_html_transcript

TOKEN = "s3cret-token-0123456789"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def site(db_bot, make_config):
    b = db_bot
    b.config = make_config(dashboard_token=TOKEN, dashboard_port=free_port())
    await b.db.upsert_metrics(channel_id=100, opener_id=1, staff_id=2, created_at=h.ago(hours=2),
                              first_response_at=h.ago(hours=2, minutes=-5), closed_at=h.ago(hours=1), guild_id=h.GUILD_ID)
    await b.db.upsert_metrics(channel_id=103, opener_id=4, staff_id=None, created_at=h.ago(hours=5),
                              first_response_at=None, closed_at=h.ago(hours=4), guild_id=h.GUILD_ID)
    save_html_transcript(b.config.transcripts_dir, 100, "ticket-bob", "<!DOCTYPE html><title>t</title>ok")
    open_ticket = MagicMock(spec=discord.TextChannel)
    open_ticket.topic, open_ticket.created_at = "ticket-owner:1", h.ago(minutes=30)
    b.tickets_category = lambda _g: h.category(h.TICKETS_CAT, channels=[open_ticket])
    b.managed_guilds = lambda: [NS(id=h.GUILD_ID, name="Caudal <Support>")]
    await b.dashboard.start()
    assert b.dashboard.server is not None
    async with aiohttp.ClientSession() as http:
        yield NS(bot=b, http=http, base=f"http://127.0.0.1:{b.config.dashboard_port}")
    await b.dashboard.stop()


def basic(password: str, user: str = "admin") -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


async def get(site, path, **kw):
    async with site.http.get(site.base + path, allow_redirects=False, **kw) as r:
        return r.status, r.headers, await r.text()


@pytest.mark.parametrize("headers, query", [
    ({}, ""),
    (basic("wrong"), ""),
    ({"Authorization": "Basic !!!not-base64"}, ""),
    ({"Authorization": f"Bearer {TOKEN}"}, ""),
    ({}, "?token=nope"),
    (basic(TOKEN[:-1]), ""),
])
async def test_rejects_bad_credentials(site, headers, query):
    status, hdrs, _ = await get(site, "/metrics" + query, headers=headers)
    assert status == 401 and hdrs["WWW-Authenticate"].startswith("Basic")


async def test_transcripts_are_protected_too(site):
    status, _, _ = await get(site, "/transcripts/100")
    assert status == 401


@pytest.mark.parametrize("auth", ["basic", "query"])
async def test_accepts_basic_auth_or_query_token(site, auth):
    headers, query = (basic(TOKEN, user="anyone"), "") if auth == "basic" else ({}, f"?token={TOKEN}")
    status, _, _ = await get(site, "/metrics" + query, headers=headers)
    assert status == 200


async def test_security_headers_on_every_response(site):
    for path, headers in (("/metrics", basic(TOKEN)), ("/metrics", {}), ("/transcripts/100", basic(TOKEN))):
        _, hdrs, _ = await get(site, path, headers=headers)
        for name, value in _DASHBOARD_HEADERS.items():
            assert hdrs[name] == value, (path, name)
    assert "script-src" not in _DASHBOARD_HEADERS["Content-Security-Policy"]   # no scripts, ever
    assert _DASHBOARD_HEADERS["Referrer-Policy"] == "no-referrer"


async def test_page_shows_live_and_historical_metrics(site):
    _, _, body = await get(site, "/metrics", headers=basic(TOKEN))
    assert "Caudal &lt;Support&gt;" in body                 # escaped guild name
    assert "50%" in body and "1/2 answered within 15 min" in body
    assert "<td>mod</td>" in body and "#ticket-bob" in body
    # Staff table: tickets, avg first response, avg resolution, SLA rate. No CSAT anywhere.
    assert ('<td>mod</td><td class="num">1</td><td class="num">5m 0s</td>'
            '<td class="num">1h 0m</td><td class="num">100%</td>') in body
    assert "Avg resolution" in body and "SLA met" in body
    assert "CSAT" not in body and "Rating" not in body and "⭐" not in body
    assert body.count('href="/transcripts/100') == 2        # view + download, only where a file exists


async def test_query_token_is_carried_on_links(site):
    _, _, body = await get(site, f"/metrics?token={TOKEN}&range=7", headers={})
    assert f"/transcripts/100?token={TOKEN}" in body and "Last 7 Days" in body
    status, hdrs, _ = await get(site, f"/?token={TOKEN}")
    assert status == 302 and hdrs["Location"] == f"/metrics?token={TOKEN}"


async def test_transcript_view_and_download(site):
    status, hdrs, body = await get(site, "/transcripts/100", headers=basic(TOKEN))
    assert status == 200 and body.endswith("ok") and hdrs["Content-Disposition"].startswith("inline")
    _, hdrs, _ = await get(site, "/transcripts/100?download=1", headers=basic(TOKEN))
    assert hdrs["Content-Disposition"] == 'attachment; filename="transcript-ticket-bob.html"'


@pytest.mark.parametrize("path", ["/transcripts/999", "/transcripts/..%2f..%2fbot.py", "/transcripts/../bot.py",
                                  "/transcripts/abc", "/transcripts/100.html"])
async def test_unknown_or_traversal_paths_are_404(site, path):
    status, _, _ = await get(site, path, headers=basic(TOKEN))
    assert status == 404


async def test_no_token_means_no_server(bot):
    await bot.dashboard.start()
    assert bot.dashboard.server is None


async def test_port_in_use_does_not_stop_the_bot(bot, make_config):
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        bot.config = make_config(dashboard_token=TOKEN, dashboard_port=blocker.getsockname()[1])
        await bot.dashboard.start()
    assert bot.dashboard.server is None


def test_render_escapes_everything():
    page = render_dashboard(guild_name="<script>", range_key="30", token_query="", open_count=0, avg_open_age="—",
                            sla=(0, 0), avg_response="—", avg_resolution="—", handled=0,
                            staff_rows=[("<img src=x onerror=1>", 1, "1s", "2m 0s", "<i>")],
                            recent_rows=[{"closed": "c", "ticket": "<b>", "opener": "o", "staff": "s", "response": "r",
                                          "resolution": "r", "transcript_url": None}])
    assert "<script>" not in page and "<img src=x" not in page and "&lt;img src=x onerror=1&gt;" in page


def test_server_switcher_keeps_the_selection_in_every_link():
    page = render_dashboard(guild_name="B", range_key="7", token_query="token=t", open_count=0, avg_open_age="—",
                            sla=(0, 0), avg_response="—", avg_resolution="—", handled=0, staff_rows=[],
                            recent_rows=[], servers=[(1, "A <x>"), (2, "B")], guild_id=2)
    assert 'href="/metrics?token=t&amp;guild=2&amp;range=30"' in page          # range links keep the server
    assert 'class="on" href="/metrics?token=t&amp;guild=2&amp;range=7">B</a>' in page
    assert 'guild=1&amp;range=7">A &lt;x&gt;</a>' in page                     # escaped server names
    single = render_dashboard(guild_name="A", range_key="7", token_query="", open_count=0, avg_open_age="—",
                              sla=(0, 0), avg_response="—", avg_resolution="—", handled=0, staff_rows=[],
                              recent_rows=[], servers=[(1, "A")], guild_id=1)
    assert "<br>" not in single                                               # one server: no switcher


async def test_metrics_only_show_the_selected_server(site):
    await site.bot.db.upsert_metrics(channel_id=900, opener_id=1, staff_id=50, created_at=h.ago(hours=2),
                                     first_response_at=None, closed_at=h.ago(hours=1), guild_id=500)
    _, _, body = await get(site, "/metrics", headers=basic(TOKEN))
    assert "#900" not in body and "User 50" not in body                          # another server's ticket
