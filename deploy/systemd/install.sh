#!/usr/bin/env bash
# Install or update caudal-bot as a systemd service.
#
#   sudo ./deploy/systemd/install.sh            (run from a checkout of the repo)
#
# Idempotent: re-run it to deploy a new version. It installs code, a virtualenv and the
# units, and runs an offline --dry-run. It does NOT start the bot (that connects to
# Discord): the last lines it prints tell you how.
set -euo pipefail

PREFIX=/opt/caudal-bot
SERVICE_USER=caudal
PYTHON="${PYTHON:-python3.11}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

die() { echo "error: $*" >&2; exit 1; }
step() { printf '\n==> %s\n' "$*"; }

[[ $EUID -eq 0 ]] || die "run as root: sudo $0"
[[ -f "$SRC/caudal_bot/main.py" ]] || die "run this from a caudal_bot checkout (looked in $SRC)"
command -v "$PYTHON" >/dev/null || die "$PYTHON not found (set PYTHON=/path/to/python3.11+)"
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "$PYTHON is older than 3.11"
"$PYTHON" -c 'import venv' 2>/dev/null || die "the venv module is missing (Debian/Ubuntu: apt install python3.11-venv)"

step "Service user"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --user-group --home-dir "$PREFIX" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    echo "created system user $SERVICE_USER"
else
    echo "$SERVICE_USER exists"
fi

was_active=no
if systemctl is-active --quiet caudal-bot.service; then
    was_active=yes
    step "Stopping the running bot for the update (graceful, up to 30s)"
    systemctl stop caudal-bot.service
fi

step "Code -> $PREFIX (root-owned, read-only to the service)"
install -d -o root -g root -m 0755 "$PREFIX"
rm -rf "$PREFIX/caudal_bot" "$PREFIX/deploy"      # drop files deleted upstream
cp -r "$SRC/caudal_bot" "$SRC/deploy" "$PREFIX/"
install -m 0644 "$SRC/bot.py" "$PREFIX/bot.py"
if [[ -f "$SRC/README.md" ]]; then install -m 0644 "$SRC/README.md" "$PREFIX/README.md"; fi
if [[ -d "$SRC/docs" ]]; then rm -rf "$PREFIX/docs"; cp -r "$SRC/docs" "$PREFIX/"; fi
find "$PREFIX/caudal_bot" -name "__pycache__" -type d -prune -exec rm -rf {} +
chown -R root:root "$PREFIX/caudal_bot" "$PREFIX/deploy" "$PREFIX/bot.py"
chmod -R u=rwX,go=rX "$PREFIX/caudal_bot" "$PREFIX/deploy"
chmod 0755 "$PREFIX/deploy/systemd/healthcheck.sh"

step "Virtualenv"
[[ -x "$PREFIX/.venv/bin/python" ]] || "$PYTHON" -m venv "$PREFIX/.venv"
"$PREFIX/.venv/bin/pip" install --disable-pip-version-check --quiet --upgrade -r "$PREFIX/caudal_bot/requirements.txt"
# Bytecode is compiled now because the service can't write it (ProtectSystem=strict).
"$PREFIX/.venv/bin/python" -m compileall -q "$PREFIX/caudal_bot" "$PREFIX/bot.py"

step "Data folders (the only paths the service may write)"
for dir in data backups transcripts; do
    install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$PREFIX/$dir"
done
# Older installs kept tickets.db next to bot.py; move it (and any WAL files) into data/.
if [[ -f "$PREFIX/tickets.db" && ! -e "$PREFIX/data/tickets.db" ]]; then
    for f in tickets.db tickets.db-wal tickets.db-shm; do
        if [[ -f "$PREFIX/$f" ]]; then mv "$PREFIX/$f" "$PREFIX/data/$f"; fi
    done
    chown "$SERVICE_USER:$SERVICE_USER" "$PREFIX"/data/tickets.db*
    echo "moved existing tickets.db into data/"
fi

step "Secrets (.env: root-owned, readable by $SERVICE_USER only)"
if [[ ! -f "$PREFIX/.env" ]]; then
    [[ -f "$SRC/.env" ]] || die "no .env in $SRC or $PREFIX; create one (at least BOT_TOKEN) and re-run"
    install -o root -g "$SERVICE_USER" -m 0640 "$SRC/.env" "$PREFIX/.env"
    echo "copied $SRC/.env"
else
    echo "keeping existing $PREFIX/.env"
fi
chown root:"$SERVICE_USER" "$PREFIX/.env"
chmod 0640 "$PREFIX/.env"

step "systemd units"
install -m 0644 "$PREFIX/deploy/systemd/caudal-bot.service" /etc/systemd/system/caudal-bot.service
install -m 0644 "$PREFIX/deploy/systemd/caudal-bot-healthcheck.service" /etc/systemd/system/caudal-bot-healthcheck.service
install -m 0644 "$PREFIX/deploy/systemd/caudal-bot-healthcheck.timer" /etc/systemd/system/caudal-bot-healthcheck.timer
systemctl daemon-reload
if command -v systemd-analyze >/dev/null; then
    systemd-analyze verify /etc/systemd/system/caudal-bot.service \
        /etc/systemd/system/caudal-bot-healthcheck.service /etc/systemd/system/caudal-bot-healthcheck.timer
fi

step "Offline dry run as $SERVICE_USER (never connects to Discord)"
(
    cd "$PREFIX"
    set -a; . "$PREFIX/deploy/systemd/caudal-bot.env"; set +a   # same layout the unit uses
    runuser -u "$SERVICE_USER" -- "$PREFIX/.venv/bin/python" "$PREFIX/bot.py" --dry-run
) || die "dry run failed; fix the problems above, then re-run this script"

if [[ $was_active == yes ]]; then
    step "Restarting the bot (it was running before the update)"
    systemctl start caudal-bot.service
    systemctl --no-pager --lines=0 status caudal-bot.service || true
else
    cat <<EOF

Installed. Nothing has connected to Discord yet. To start the bot and the health watchdog:

    sudo systemctl enable --now caudal-bot.service caudal-bot-healthcheck.timer
    journalctl -u caudal-bot -f
EOF
fi
