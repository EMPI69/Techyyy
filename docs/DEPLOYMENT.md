# Deploying the Caudal bot

Three supported ways to run it in production. All of them:

- start the bot only with `--run` (and validate offline with `--dry-run` first),
- use the same layout: `data/tickets.db`, `backups/`, `transcripts/`,
- stop it gracefully (SIGTERM, or Ctrl+C on Windows): background loops are awaited,
  the dashboard stops, SQLite is flushed, and the Discord connection is closed,
- expose `GET /health` on `127.0.0.1:8081` for supervision. (Run directly, the bot defaults
  to `127.0.0.1:8080`; these setups pin 8081 so it never shares the dashboard's port.)
  It returns `200` when the
  gateway is connected, the database is open and every background loop is running,
  otherwise `503`, with status flags only (no ticket data).

> **Run one copy only.** Two processes with the same token both answer every message and
> race on every button. Stop any `python bot.py` running in a terminal first.

---

## 1. Docker (recommended)

Requires Docker Engine 24+ with Compose v2.

```sh
git clone <repo> caudal-bot && cd caudal-bot
cp .env.example .env && chmod 600 .env        # fill in BOT_TOKEN (then run /setup in Discord)

# The container runs as UID/GID 10001 and must own its data folders.
mkdir -p data backups transcripts
sudo chown 10001:10001 data backups transcripts
#   (or build with your own IDs: CAUDAL_UID=$(id -u) CAUDAL_GID=$(id -g) docker compose build)

docker compose build
docker build --target test .                  # optional: run the test suite in the image
docker compose run --rm bot --dry-run         # validate config offline (never connects)
docker compose up -d                          # start
docker compose ps                             # STATUS shows (healthy) after ~1 minute
docker compose logs -f bot
```

| Task | Command |
| --- | --- |
| Stop gracefully | `docker compose stop` (SIGTERM, up to 30s) |
| Update | `git pull && docker compose build && docker compose up -d` |
| Check health | `docker inspect --format '{{json .State.Health}}' caudal-bot` |
| Version | `docker compose run --rm bot --version` |

What the image does:

- **Build:** multi-stage. Dependencies install from prebuilt wheels only, so no compiler
  is ever installed; pip and setuptools are removed from the runtime image.
- **User:** runs as the non-root user `caudal` (UID/GID 10001), with setuid/setgid bits
  stripped.
- **Filesystem:** the code in `/app` is root-owned and read-only (`read_only: true`), and
  only the three volumes are writable.
- **Privileges:** all Linux capabilities are dropped and `no-new-privileges` is set.
- **Limits:** 0.5 CPU and 256 MB of memory. Raise `deploy.resources.limits` in
  `docker-compose.yml` for large servers.
- **Dashboard:** published on `127.0.0.1:8080` only, because it's plain HTTP. Put an
  HTTPS reverse proxy in front before exposing it beyond the host. It serves anything
  only if `DASHBOARD_AUTH_TOKEN` is set.

**Automatic restarts:** `restart: unless-stopped` restarts the container when the process
exits. Plain Docker does **not** restart a container that's merely *unhealthy*. If you
want that, run a watcher such as `willfarrell/autoheal`, or use the systemd timer approach
below.

**Existing data:** move an existing `tickets.db` into `./data/` before the first start.
The container looks for `data/tickets.db`.

---

## 2. Linux with systemd (bare metal / VPS)

Requires Python 3.11+ with the `venv` module (Debian/Ubuntu: `apt install python3.11-venv`).

```sh
git clone <repo> caudal-bot && cd caudal-bot
cp .env.example .env                          # fill it in; the installer copies it
sudo bash deploy/systemd/install.sh           # user, code, venv, units, offline dry run
sudo systemctl enable --now caudal-bot.service caudal-bot-healthcheck.timer
journalctl -u caudal-bot -f
```

`install.sh` is idempotent. **To update, re-run it from a newer checkout.** It stops the
running bot, installs the new version, dry-runs it, and starts it again.

The installer sets up:

| Path | Owner / mode | Purpose |
| --- | --- | --- |
| `/opt/caudal-bot/caudal_bot`, `bot.py` | root, read-only to the service | code |
| `/opt/caudal-bot/.venv` | root | virtualenv |
| `/opt/caudal-bot/.env` | `root:caudal 0640` | secrets |
| `/opt/caudal-bot/data`, `backups`, `transcripts` | `caudal 0750` | the only writable paths |

Unit highlights (`deploy/systemd/caudal-bot.service`):

- **Restart and stop:** `Restart=always`, `RestartSec=10`, `KillSignal=SIGTERM`,
  `KillMode=mixed` and `TimeoutStopSec=30`. SIGTERM reaches the bot only; the whole
  process group is killed after 30s.
- **Pre-start check:** `ExecStartPre=... --dry-run` validates config and schema before
  every start.
- **Start limit:** 10 failed starts in 10 minutes stops further retries, so a revoked
  token doesn't hammer Discord's login. Clear it with
  `sudo systemctl reset-failed caudal-bot` after fixing the config.
- **Sandboxing:** `ProtectSystem=strict` with `ReadWritePaths=` limited to the data
  folders, no capabilities, `NoNewPrivileges`, a private `/tmp`, and a restricted set of
  address families.
- **Resource limits:** 512 MB of memory and 50% CPU.

**Health watchdog:** `caudal-bot-healthcheck.timer` probes `/health` every 2 minutes. It
restarts the bot only after 3 consecutive failures 10s apart. It never starts a bot you
stopped yourself (`Requisite=`).

| Task | Command |
| --- | --- |
| Stop gracefully | `sudo systemctl stop caudal-bot` |
| Status / health | `systemctl status caudal-bot`; `curl -s 127.0.0.1:8081/health` |
| Offline check | `cd /opt/caudal-bot && sudo -u caudal env DATABASE_PATH=data/tickets.db .venv/bin/python bot.py --dry-run` |
| Uninstall | `sudo systemctl disable --now caudal-bot caudal-bot-healthcheck.timer`, then remove the three units from `/etc/systemd/system` and `/opt/caudal-bot` |

---

## 3. Windows service (NSSM)

Requires [NSSM](https://nssm.cc) (`winget install NSSM.NSSM` or `choco install nssm`) and
Python 3.11+. From an **elevated** PowerShell in the project folder:

```powershell
.\deploy\windows\install-service.ps1          # venv, dry run, service, permissions (doesn't start)
nssm start CaudalBot                          # or: .\deploy\windows\install-service.ps1 -Start
Get-Content .\logs\caudal-bot.log -Wait
```

Re-run the script to update settings. `uninstall-service.ps1` removes the service and
leaves your data alone.

What it configures, as raw NSSM commands, if you'd rather run them by hand:

```powershell
$svc = "CaudalBot"; $dir = "C:\path\to\caudal-bot"
nssm install $svc "$dir\.venv\Scripts\python.exe"
nssm set $svc AppDirectory $dir
nssm set $svc AppParameters "bot.py --run"
nssm set $svc Start SERVICE_DELAYED_AUTO_START
nssm set $svc AppEnvironmentExtra PYTHONUNBUFFERED=1 PYTHONUTF8=1 "DATABASE_PATH=data\tickets.db" HEALTH_HOST=127.0.0.1 HEALTH_PORT=8081
# restart on crash, back off if it keeps dying within 60s
nssm set $svc AppExit Default Restart
nssm set $svc AppRestartDelay 10000
nssm set $svc AppThrottle 60000
# graceful stop: Ctrl+C (the bot's SIGINT handler), wait 30s
nssm set $svc AppNoConsole 0
nssm set $svc AppStopMethodConsole 30000
nssm set $svc AppStopMethodWindow 5000
nssm set $svc AppStopMethodThreads 5000
nssm set $svc AppKillProcessTree 1
# one appended log, rotated daily or at 10 MB, also while running
nssm set $svc AppStdout "$dir\logs\caudal-bot.log"
nssm set $svc AppStderr "$dir\logs\caudal-bot.log"
nssm set $svc AppStdoutCreationDisposition 4
nssm set $svc AppStderrCreationDisposition 4
nssm set $svc AppRotateFiles 1
nssm set $svc AppRotateOnline 1
nssm set $svc AppRotateSeconds 86400
nssm set $svc AppRotateBytes 10485760
# least-privilege account: read the project, write only data/backups/transcripts/logs
nssm set $svc ObjectName "NT AUTHORITY\LocalService"
icacls $dir /grant "*S-1-5-19:(OI)(CI)RX"
foreach ($d in "data","backups","transcripts","logs") { icacls "$dir\$d" /grant "*S-1-5-19:(OI)(CI)M" }
```

Windows-specific notes:

- **Python installed "for me only":** a Python under `C:\Users\...` can't be read by
  service accounts. The script detects this and grants read access to that Python folder.
  Installing Python "for all users" avoids it.
- **Existing data:** the script moves an existing root-level `tickets.db` into `data\`,
  unless a running bot has it open. In that case it warns and leaves it.
- **Health:** check `Invoke-WebRequest http://127.0.0.1:8081/health`. NSSM restarts the
  bot on crashes. It has no health-based restart, but the `-Start` switch waits for
  `/health` to report healthy.

---

## Backups & data

- `backups/` holds a verified SQLite snapshot every 24h, kept for 14 days. `transcripts/`
  holds HTML transcripts, also kept for 14 days. Both sit on the same disk as the
  database, so copy `backups/` off the machine for real disaster recovery.
- To restore, stop the bot and copy a backup over `data/tickets.db` (delete any
  `tickets.db-wal` / `-shm` next to it). Then run `--dry-run`, which validates the schema,
  and start.
