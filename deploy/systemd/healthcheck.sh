#!/bin/sh
# Run by caudal-bot-healthcheck.service. Restarts the bot only after three failed probes
# 10s apart, so a brief Discord reconnect never triggers a restart.
set -u

URL="${HEALTH_URL:-http://127.0.0.1:8081/health}"
PY=/opt/caudal-bot/.venv/bin/python

attempt=1
while [ "$attempt" -le 3 ]; do
    if "$PY" -c 'import sys, urllib.request as u; u.build_opener(u.ProxyHandler({})).open(sys.argv[1], timeout=5)' "$URL" 2>/dev/null; then
        exit 0
    fi
    if [ "$attempt" -lt 3 ]; then sleep 10; fi
    attempt=$((attempt + 1))
done

echo "caudal-bot: $URL unhealthy on 3 consecutive probes; restarting caudal-bot.service" >&2
exec systemctl restart caudal-bot.service
