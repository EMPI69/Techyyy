"""The /health liveness endpoint used by Docker's HEALTHCHECK, the systemd timer and
``Invoke-RestMethod http://127.0.0.1:8080/health``."""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock

import aiohttp
import pytest

from caudal_bot.cogs.health import health_snapshot
from caudal_bot.config import Config
from caudal_bot.main import FAQBot


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fake_state(bot, monkeypatch, *, ready=True, latency=0.042, db=True, loops=True):
    monkeypatch.setattr(type(bot), "latency", property(lambda self: latency))
    monkeypatch.setattr(bot, "is_ready", lambda: ready)
    monkeypatch.setattr(bot, "is_closed", lambda: False)
    bot.db = object() if db else None
    for loop in (bot.tickets._cleanup_expired, bot.tickets._sla_check, bot.maintenance._backup_database,
                 bot.transcript_cleanup._purge_loop):
        monkeypatch.setattr(loop, "is_running", lambda running=loops: running)


def test_healthy_when_connected_with_database_and_loops(bot, monkeypatch):
    fake_state(bot, monkeypatch)
    status, body = health_snapshot(bot, started_at=0)
    assert status == 200 and body["latency_ms"] == 42 and all(body["loops"].values())
    assert {k: body[k] for k in ("status", "database", "gateway")} == {
        "status": "ok", "database": "connected", "gateway": "connected"}


@pytest.mark.parametrize("state, expected", [
    (dict(ready=False, latency=float("nan")), ("starting", "connected", "connecting")),
    (dict(latency=float("inf")), ("degraded", "connected", "disconnected")),  # the websocket is gone
    (dict(db=False), ("degraded", "unavailable", "connected")),
    (dict(loops=False), ("degraded", "connected", "connected")),              # a background loop died
])
def test_unhealthy_states_return_503(bot, monkeypatch, state, expected):
    fake_state(bot, monkeypatch, **state)
    status, body = health_snapshot(bot, started_at=0)
    assert status == 503 and (body["status"], body["database"], body["gateway"]) == expected


def test_body_contains_no_ticket_data(bot, monkeypatch):
    fake_state(bot, monkeypatch)
    _, body = health_snapshot(bot, started_at=0)
    assert set(body) == {"status", "gateway", "latency_ms", "database", "loops", "uptime_s"}


def test_on_by_default_at_loopback_8080():
    cfg = Config(bot_token="x", guild_id=1, staff_role_id=2, tickets_category_id=3)
    assert (cfg.health_host, cfg.health_port) == ("127.0.0.1", 8080)


async def test_health_port_off_disables_it(bot):
    assert bot.config.health_port is None   # conftest's config, as with HEALTH_PORT=off
    await bot.health.start()
    assert not bot.health.running


async def test_shares_the_dashboard_server_on_the_same_port(make_config, monkeypatch):
    """Both default to 8080: the dashboard's server answers /health, without its token."""
    port = free_port()
    bot = FAQBot(make_config(dashboard_token="t" * 20, dashboard_port=port, health_port=port))
    fake_state(bot, monkeypatch)
    await bot.dashboard.start()
    await bot.health.start()
    try:
        assert bot.dashboard.server is not None and not bot.health.running   # no second server
        async with aiohttp.ClientSession() as http:
            async with http.get(f"http://127.0.0.1:{port}/health") as r:
                assert r.status == 200 and (await r.json())["status"] == "ok"
            async with http.get(f"http://127.0.0.1:{port}/metrics") as r:
                assert r.status == 401                                         # the rest still needs the token
    finally:
        await bot.health.stop()
        await bot.dashboard.stop()


async def test_dashboard_on_another_port_does_not_serve_health(make_config):
    port = free_port()
    bot = FAQBot(make_config(dashboard_token="t" * 20, dashboard_port=port, health_port=None))
    await bot.dashboard.start()
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(f"http://127.0.0.1:{port}/health") as r:
                assert r.status == 401                                         # not an unauthenticated route
    finally:
        await bot.dashboard.stop()


async def test_serves_http_without_auth(make_config, monkeypatch):
    port = free_port()
    bot = FAQBot(make_config(health_port=port))
    fake_state(bot, monkeypatch, ready=False, latency=float("nan"))
    await bot.health.start()
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(f"http://127.0.0.1:{port}/health") as r:
                assert r.status == 503 and (await r.json())["status"] == "starting"
                assert r.headers["Cache-Control"] == "no-store"
            fake_state(bot, monkeypatch)
            async with http.get(f"http://127.0.0.1:{port}/health") as r:
                assert r.status == 200 and (await r.json())["status"] == "ok"
            for path in ("/metrics", "/transcripts/1", "/"):
                async with http.get(f"http://127.0.0.1:{port}{path}") as r:
                    assert r.status == 404          # nothing else is served on this port
            async with http.post(f"http://127.0.0.1:{port}/health") as r:
                assert r.status == 405
    finally:
        await bot.health.stop()
    assert not bot.health.running


async def test_busy_port_does_not_stop_the_bot(make_config):
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        bot = FAQBot(make_config(health_port=blocker.getsockname()[1]))
        await bot.health.start()
    assert not bot.health.running


def test_docker_healthcheck_one_liner_matches_the_endpoint():
    """The probe baked into the Dockerfile and compose file must hit this route."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    for name in ("Dockerfile", "docker-compose.yml"):
        text = (root / name).read_text(encoding="utf-8")
        assert "http://127.0.0.1:8081/health" in text, name
