# syntax=docker/dockerfile:1
#
# Caudal support bot. Build:   docker build -t caudal-bot:1.1.0 .
#                     Test:    docker build --target test .
#
# Stages: deps (virtualenv with runtime deps) -> test (optional, runs pytest)
#         -> venv (deps with pip/setuptools stripped) -> runtime (what ships).

ARG PYTHON_IMAGE=python:3.11-slim

# ---- deps: resolve runtime dependencies into an isolated virtualenv -----------------
FROM ${PYTHON_IMAGE} AS deps
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
COPY caudal_bot/requirements.txt /tmp/requirements.txt
# Every dependency ships prebuilt wheels for CPython 3.11 (amd64 and arm64), so no
# compiler is ever installed. --only-binary makes a missing wheel fail loudly instead.
RUN pip install --only-binary=:all: -r /tmp/requirements.txt

# ---- test: run the offline test suite inside the image (docker build --target test .) --
FROM deps AS test
COPY caudal_bot/requirements-dev.txt /tmp/requirements-dev.txt
RUN sed -i 's#^-r requirements.txt#-r /tmp/requirements.txt#' /tmp/requirements-dev.txt \
 && pip install --only-binary=:all: -r /tmp/requirements-dev.txt
WORKDIR /src
COPY . .
RUN python -m pytest -q -p no:cacheprovider

# ---- venv: the runtime virtualenv without packaging tools ---------------------------
FROM deps AS venv
RUN pip uninstall -y setuptools pip \
 && find /opt/venv -name "__pycache__" -type d -prune -exec rm -rf {} +

# ---- runtime -------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

ARG UID=10001
ARG GID=10001

LABEL org.opencontainers.image.title="caudal-bot" \
      org.opencontainers.image.description="Discord FAQ auto-responder with ticket escalation" \
      org.opencontainers.image.version="1.1.0"

# Same on-disk layout as a bare-metal install: code in /app (read-only for the bot),
# writable data in /app/data (tickets.db), /app/backups and /app/transcripts.
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    DATABASE_PATH=data/tickets.db \
    DASHBOARD_HOST=0.0.0.0 \
    HEALTH_HOST=127.0.0.1 \
    HEALTH_PORT=8081

# Non-root user; drop the base image's packaging tools and setuid/setgid bits.
RUN set -eux; \
    groupadd --system --gid "${GID}" caudal; \
    useradd --system --uid "${UID}" --gid caudal --no-create-home --home-dir /nonexistent \
            --shell /usr/sbin/nologin caudal; \
    python -m pip uninstall -y pip setuptools wheel || true; \
    rm -rf /usr/local/lib/python3.11/ensurepip /root/.cache /var/cache/debconf/* /var/log/* /tmp/*; \
    find / -xdev -type f -perm /6000 -exec chmod a-s {} + || true

COPY --from=venv /opt/venv /opt/venv
WORKDIR /app
COPY bot.py ./
COPY caudal_bot/ ./caudal_bot/
# Code stays root-owned (the bot can't modify itself); bytecode is compiled at build time
# because nothing can write it at runtime. Only the three data folders belong to caudal.
RUN python -m compileall -q bot.py caudal_bot \
 && mkdir -p data backups transcripts \
 && chown caudal:caudal data backups transcripts \
 && chmod 0750 data backups transcripts

USER caudal:caudal

# Metrics dashboard (only served when DASHBOARD_AUTH_TOKEN is set). The /health endpoint
# on 8081 stays on the container's loopback and is not exposed.
EXPOSE 8080

# SIGTERM starts the graceful shutdown (loops awaited, SQLite flushed, gateway closed).
STOPSIGNAL SIGTERM

# Zero-dependency probe: urllib raises (non-zero exit) on 503 or no answer. Proxies are
# bypassed so an HTTP_PROXY in the environment can't intercept a loopback request.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD ["python", "-c", "import urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:8081/health', timeout=4)"]

ENTRYPOINT ["python", "-m", "caudal_bot.main"]
CMD ["--run"]
