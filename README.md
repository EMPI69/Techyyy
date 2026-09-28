Discord FAQ auto-replies with ticket escalation, transcripts, metrics and a dashboard.
Configuration lives in `.env` (see the comments in that file).

## Running

```sh
pip install -r caudal_bot/requirements.txt

python bot.py --dry-run   # validate config, imports and the database schema; never connects
python bot.py --run       # connect to Discord
python bot.py --version
```

`python -m caudal_bot` accepts the same flags. **Nothing connects to Discord without
`--run`**: a bare `python bot.py`, `--help` or a mistyped flag exits without logging in.
`--test` is an alias for `--dry-run`. It is read-only: an existing `tickets.db` is opened
read-only, and a missing one is not created.

Stop with Ctrl+C or SIGTERM. The bot shuts down gracefully: background loops are cancelled
and awaited, the dashboard's web server is stopped, the database is flushed and closed,
and the Discord connection is closed. Press Ctrl+C a second time to force an immediate exit.

## Deploying

Docker Compose, a hardened systemd unit and an NSSM Windows service are ready to use;
see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). All of them run `--run` under supervision,
keep data in `data/`, `backups/` and `transcripts/`, and probe the `/health` endpoint.

## Health check

`GET http://127.0.0.1:8080/health` is on by default and needs no token. It returns `200`
with `{"status": "ok", "database": "connected", "gateway": "connected", ...}` when the bot
is fully up, and `503` otherwise, with status flags only. Configure it with `HEALTH_HOST` and
`HEALTH_PORT`, or set `HEALTH_PORT=off` to disable it. When the dashboard also runs on
port 8080, the dashboard's server answers `/health` too.

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8080/health"
```

## FAQ answers

When a message matches an FAQ topic, the bot replies with one short line and a
**📘 Show answer** button, and deletes that line after 60 seconds. The answer and its
**Open a Ticket** button appear only to whoever clicks, as an ephemeral message. Discord
only allows those in response to an interaction, not to a chat message. Members can also
run `/faq` (with autocomplete) for a private answer at any time.

## Adding the bot to a server

Only `BOT_TOKEN` is required. Invite the bot with the **Manage Channels** and **Manage Roles**
permissions, then have an administrator run `/setup staff_role:@Support` in that server.
It finds or creates:

- the **🎫 TICKETS** category;
- the private **📦 ARCHIVED TICKETS** category;
- **#ticket-transcripts** and **#ticket-sla-alerts** inside the archive category;

and saves them for that server. Running it again only adds what's missing. Each server has
its own settings, staff role and analytics, and `/set-faq-channels` limits FAQ answers to
chosen channels.

**Before `/setup`:** tickets still work. The tickets category is created the first time
someone opens a ticket, closed tickets are archived in place, and SLA warnings go inside
the ticket.

**Existing `.env` IDs:** `GUILD_ID` with `STAFF_ROLE_ID`, `TICKETS_CATEGORY_ID` and so on
still configure that one server until it runs `/setup`, which keeps the categories they
point at.

## Staff commands

`/admin-help` lists them all. The staff role comes from `/setup` or `/set-staff-role` (or
`STAFF_ROLE_ID` for `GUILD_ID`), and each server has its own. Closing and
re-opening a ticket only move it between categories and toggle the owner's permission to
send; the channel is never renamed. Discord allows only 2 renames or topic edits per
channel every 10 minutes, so this flow can't hit that limit.

## Tests

```sh
pip install -r caudal_bot/requirements-dev.txt
python -m pytest                        # everything (~370 tests, ~15 s)
python -m pytest tests/test_lifecycle.py -k archive   # one area
```

The suite is fully offline. An autouse fixture in `tests/conftest.py` disables `.env`
loading and makes any Discord login, gateway connection or REST call fail the test, so
no test can reach Discord or see the real token. All files a test writes go to pytest's
temporary directories. The one exception is `tests/test_dashboard.py`, which starts the
real web server on a free `127.0.0.1` port.

| Module | Covers |
| --- | --- |
| `test_config.py` | `.env` validation (IDs, cooldown, port range), defaults, path resolution |
| `test_faq.py` | the 5 onboarding topics, most-specific-keyword-wins matching, word boundaries, cooldowns, where the bot listens, the public prompt and private (ephemeral) answers, `/faq` |
| `test_lifecycle.py` | topic format, permission overwrites, archive / re-open / delete with no renames or topic edits, tickets closed by older versions, stored deadlines, claiming, 48 h cleanup, SLA alerts |
| `test_transcripts.py` | markdown, XSS sanitisation, message grouping, `.txt` format, storage and the 14-day purge, log channel (`.txt` only) and the closing DM (no files) |
| `test_database.py` | schema creation and in-place migration, read-only schema check, durability, stats / leaderboard / SLA-compliance queries, legacy `csat_score` column left intact, backups and retention |
| `test_setup.py` | per-server settings (cache, `.env` fallback, legacy migration), `/setup`, `/set-faq-channels`, creating the tickets category on the fly, fallbacks |
| `test_commands.py` | `/staff-stats` and `/staff-leaderboard` output, `/set-staff-role` and the dynamic staff checks, `/admin-help`, `/ticket-purge` |
| `test_dashboard.py` | auth, security headers, page content, transcript downloads, path traversal |
| `test_main.py` | startup wiring, the slash-command contract, message routing, CLI guards, dry run, signals, graceful shutdown |
| `test_health.py` | the `/health` endpoint: on by default, healthy/starting/degraded states, no auth, sharing the dashboard's port |
| `test_deploy.py` | Dockerfile, compose, systemd and NSSM files agree with the code (paths, ports, `--run`, timeouts); `.gitignore` checked with real git |
