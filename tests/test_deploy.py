"""Deployment files must agree with the code: paths, ports, the --run guard, shutdown
timings, and ignore rules. Docker, systemd and NSSM aren't needed; files are parsed."""

from __future__ import annotations

import fnmatch
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath

import pytest
import yaml

from caudal_bot.config import Config

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
HEALTH_URL = "http://127.0.0.1:8081/health"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def container_config(env: dict) -> Config:
    """The Config the bot would build inside the image (code at /app)."""
    return Config(bot_token="x", guild_id=1, staff_role_id=2, tickets_category_id=3,
                  database_path=env["DATABASE_PATH"], data_dir=Path("/app"))


# ---- Dockerfile ----------------------------------------------------------------------

def dockerfile_env() -> dict:
    text = read("Dockerfile").split("FROM ${PYTHON_IMAGE} AS runtime", 1)[1]
    block = re.search(r"^ENV (.+?)(?=\n\n|\n#)", text, flags=re.S | re.M).group(1)
    return dict(re.findall(r'(\w+)=("[^"]*"|\S+)', block.replace("\\\n", " ")))


def test_dockerfile_stages_and_base():
    text = read("Dockerfile")
    assert re.findall(r"^FROM (\S+) AS (\w+)", text, flags=re.M) == [
        ("${PYTHON_IMAGE}", "deps"), ("deps", "test"), ("deps", "venv"), ("${PYTHON_IMAGE}", "runtime")]
    assert "ARG PYTHON_IMAGE=python:3.11-slim" in text
    assert text.rstrip().endswith('CMD ["--run"]')                        # the final stage is what ships
    assert 'ENTRYPOINT ["python", "-m", "caudal_bot.main"]' in text


def test_dockerfile_runs_as_a_fixed_non_root_user():
    runtime = read("Dockerfile").split("AS runtime", 1)[1]
    assert "ARG UID=10001" in runtime and "ARG GID=10001" in runtime
    assert '--uid "${UID}"' in runtime and '--gid "${GID}"' in runtime
    assert re.search(r"^USER caudal:caudal$", runtime, flags=re.M)
    # USER comes after every RUN, so nothing executes as root in the final image at runtime
    assert runtime.rfind("\nRUN ") < runtime.find("\nUSER caudal")


def test_dockerfile_strips_packaging_tools_and_secrets():
    text = read("Dockerfile")
    assert "--only-binary=:all:" in text                   # no compiler ever needed
    assert "pip uninstall -y setuptools pip" in text       # runtime venv without pip
    assert "chmod a-s" in text                              # setuid/setgid bits removed
    assert "COPY . ." not in text.split("AS runtime", 1)[1]  # runtime copies only the code
    assert ".env" not in text.split("AS runtime", 1)[1]


def test_dockerfile_layout_matches_the_volumes():
    env = dockerfile_env()
    cfg = container_config(env)
    assert cfg.database_file == Path("/app/data/tickets.db")
    assert (cfg.transcripts_dir, cfg.backups_dir) == (Path("/app/transcripts"), Path("/app/backups"))
    assert env["HEALTH_PORT"] == "8081" and env["HEALTH_HOST"] == "127.0.0.1"
    assert HEALTH_URL in read("Dockerfile") and "STOPSIGNAL SIGTERM" in read("Dockerfile")


# ---- docker-compose.yml ----------------------------------------------------------------

@pytest.fixture(scope="module")
def service() -> dict:
    return yaml.safe_load(read("docker-compose.yml"))["services"]["bot"]


def test_compose_persistence_volumes(service):
    mounts = dict(v.split(":", 1) for v in service["volumes"])
    assert mounts == {"./data": "/app/data", "./backups": "/app/backups", "./transcripts": "/app/transcripts"}
    cfg = container_config(service["environment"])
    in_container = {cfg.database_file.parent.as_posix(), cfg.backups_dir.as_posix(), cfg.transcripts_dir.as_posix()}
    assert in_container == set(mounts.values())


def test_compose_runtime_contract(service):
    assert service["command"] == ["--run"] and service["restart"] == "unless-stopped"
    assert service["init"] is True and service["env_file"] == [".env"]
    assert service["ports"] == ["127.0.0.1:8080:8080"]
    assert service["environment"]["DASHBOARD_HOST"] == "0.0.0.0"         # else the mapping can't reach it
    assert service["build"]["target"] == "runtime"


def test_compose_resource_limits_and_hardening(service):
    limits = service["deploy"]["resources"]["limits"]
    assert float(limits["cpus"]) <= 1 and limits["memory"].endswith("M")
    assert service["read_only"] is True and service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]


def test_compose_graceful_stop_and_healthcheck(service):
    assert service["stop_signal"] == "SIGTERM"
    assert int(service["stop_grace_period"].rstrip("s")) >= 30
    hc = service["healthcheck"]
    assert hc["test"][:2] == ["CMD", "python"] and HEALTH_URL in hc["test"][-1]
    assert service["environment"]["HEALTH_PORT"] == "8081"


# ---- systemd ----------------------------------------------------------------------------

def unit(name: str) -> dict[str, list[str]]:
    """Directive -> every value (EnvironmentFile appears twice, so no configparser)."""
    out: dict[str, list[str]] = {}
    for line in (DEPLOY / "systemd" / name).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "[")) and "=" in line:
            key, value = line.split("=", 1)
            out.setdefault(key, []).append(value)
    return out


def test_service_unit_required_directives():
    u = unit("caudal-bot.service")
    one = lambda k: u[k][0]
    assert (one("Restart"), one("RestartSec"), one("KillSignal"), one("TimeoutStopSec")) == ("always", "10", "SIGTERM", "30")
    assert one("User") == "caudal" and one("WorkingDirectory") == "/opt/caudal-bot"
    assert one("ExecStart").endswith("bot.py --run") and one("ExecStartPre").endswith("bot.py --dry-run")
    assert one("WantedBy") == "multi-user.target"


def test_service_env_files_load_in_the_right_order():
    files = unit("caudal-bot.service")["EnvironmentFile"]
    assert files == ["/opt/caudal-bot/.env", "/opt/caudal-bot/deploy/systemd/caudal-bot.env"]  # later wins


def test_service_can_write_exactly_the_data_folders():
    layout = dict(line.split("=", 1) for line in read("deploy/systemd/caudal-bot.env").splitlines()
                  if line and not line.startswith("#"))
    cfg = Config(bot_token="x", guild_id=1, staff_role_id=2, tickets_category_id=3,
                 database_path=layout["DATABASE_PATH"], data_dir=Path("/opt/caudal-bot"))
    writable = unit("caudal-bot.service")["ReadWritePaths"][0].split()
    as_posix = lambda p: PurePosixPath(p.as_posix())
    assert set(writable) == {str(as_posix(cfg.database_file.parent)), str(as_posix(cfg.backups_dir)),
                             str(as_posix(cfg.transcripts_dir))}
    assert unit("caudal-bot.service")["ProtectSystem"] == ["strict"]
    assert layout["HEALTH_PORT"] == "8081" and HEALTH_URL in read("deploy/systemd/healthcheck.sh")


def test_healthcheck_timer_targets_its_service():
    assert unit("caudal-bot-healthcheck.timer")["Unit"] == ["caudal-bot-healthcheck.service"]
    svc = unit("caudal-bot-healthcheck.service")
    assert svc["Requisite"] == ["caudal-bot.service"]            # never revives a deliberately stopped bot
    assert svc["ExecStart"][0].endswith("deploy/systemd/healthcheck.sh")


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
@pytest.mark.parametrize("script", ["install.sh", "healthcheck.sh"])
def test_shell_scripts_parse(script):
    path = (DEPLOY / "systemd" / script).as_posix()
    result = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "\r" not in (DEPLOY / "systemd" / script).read_text(encoding="utf-8")   # LF only, or bash breaks


def test_install_script_never_starts_the_bot_on_first_install():
    """Starting connects to Discord, so the installer only ever restarts a bot that was
    already running before an update; a first install just prints how to start it."""
    lines = read("deploy/systemd/install.sh").splitlines()
    branch = lines.index("if [[ $was_active == yes ]]; then")
    otherwise = lines.index("else", branch)
    heredoc = lines.index("    cat <<EOF", otherwise)
    heredoc_end = lines.index("EOF", heredoc)
    for i, line in enumerate(lines):
        if re.search(r"systemctl start caudal-bot", line):
            assert branch < i < otherwise, f"line {i + 1} starts the bot outside the update branch"
        if re.search(r"systemctl enable --now", line):
            assert heredoc < i < heredoc_end, f"line {i + 1} enables the bot instead of printing how to"


# ---- NSSM ----------------------------------------------------------------------------------

@pytest.mark.parametrize("setting", [
    'AppParameters "bot.py --run"', "AppExit Default Restart", "AppRestartDelay 10000",
    "AppStopMethodConsole 30000", "AppNoConsole 0", "AppRotateFiles 1", "AppRotateOnline 1",
    "AppRotateBytes 10485760", "AppRotateSeconds 86400", "AppStderr $LogFile", "AppStdout $LogFile",
    "Start SERVICE_DELAYED_AUTO_START",
])
def test_nssm_script_sets(setting):
    assert f"Invoke-Nssm set $ServiceName {setting}" in read("deploy/windows/install-service.ps1")


def test_nssm_script_uses_the_shared_layout_and_only_starts_on_request():
    text = read("deploy/windows/install-service.ps1")
    assert '"DATABASE_PATH=data\\tickets.db"' in text and '"HEALTH_PORT=8081"' in text
    assert re.search(r"if \(\$Start\) \{\s+Step .+\s+Invoke-Nssm start", text)
    assert text.count("Invoke-Nssm start") == 1


# ---- ignore rules --------------------------------------------------------------------------

MUST_IGNORE = [".env", ".env.local", ".env.production", "discord.token", "tickets.db", "tickets.db-wal",
               "tickets.db-shm", "data/tickets.db", "backups/tickets_backup_20260925_161841.db",
               "transcripts/100__ticket-bob.html", "logs/caudal-bot.log", "caudal_bot/__pycache__/main.cpython-311.pyc",
               ".pytest_cache/v/cache/nodeids", ".coverage", "htmlcov/index.html", "Thumbs.db", ".DS_Store",
               ".venv/pyvenv.cfg", "caudal_bot/transcripts/x.html.partial"]
MUST_KEEP = ["caudal_bot/transcripts/generator.py", "caudal_bot/transcripts/cleanup.py",
             "caudal_bot/transcripts/delivery.py", "caudal_bot/database.py", "deploy/systemd/caudal-bot.env",
             ".env.example", "Dockerfile", "docker-compose.yml", ".dockerignore", "tests/test_deploy.py",
             "caudal_bot/requirements.txt", "pyproject.toml", "bot.py"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_gitignore_with_real_git(tmp_path):
    shutil.copy(ROOT / ".gitignore", tmp_path / ".gitignore")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for rel in MUST_IGNORE + MUST_KEEP:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x")
    result = subprocess.run(["git", "check-ignore", "--no-index", *MUST_IGNORE, *MUST_KEEP],
                            cwd=tmp_path, capture_output=True, text=True)
    ignored = set(result.stdout.split())
    assert set(MUST_IGNORE) <= ignored, set(MUST_IGNORE) - ignored
    assert not (set(MUST_KEEP) & ignored), set(MUST_KEEP) & ignored


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_every_real_source_file_is_committable():
    sources = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*")
               if p.is_file() and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts
               and p.suffix in {".py", ".toml", ".yml", ".sh", ".ps1", ".service", ".timer", ".md", ".txt"}
               and not {"data", "backups", "transcripts", "logs", ".venv"} & {p.relative_to(ROOT).parts[0]}]
    assert "caudal_bot/transcripts/generator.py" in sources
    result = subprocess.run(["git", "-c", f"core.excludesFile={ROOT / '.gitignore'}", "check-ignore", "--no-index",
                             *sources], cwd=ROOT, capture_output=True, text=True)
    assert result.stdout.split() == []


def dockerignored(path: str) -> bool:
    """Docker's rule: root-anchored patterns; a match on any parent excludes the file.
    fnmatch's '*' also crosses '/', so this errs towards 'excluded' (conservative)."""
    patterns = [l.strip() for l in read(".dockerignore").splitlines() if l.strip() and not l.startswith("#")]
    parts = path.split("/")
    prefixes = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    return any(fnmatch.fnmatchcase(prefix, pat) for pat in patterns if not pat.startswith("!") for prefix in prefixes)


@pytest.mark.parametrize("path", [".env", ".env.local", "tickets.db", "data/tickets.db", "backups/x.db",
                                  "transcripts/1__a.html", ".git/config", "caudal_bot/__pycache__/x.pyc"])
def test_dockerignore_keeps_secrets_and_data_out_of_the_build(path):
    assert dockerignored(path)


@pytest.mark.parametrize("path", ["caudal_bot/transcripts/generator.py", "caudal_bot/main.py", "bot.py",
                                  "caudal_bot/requirements.txt", "caudal_bot/requirements-dev.txt",
                                  "tests/test_deploy.py", "pyproject.toml", "Dockerfile", "docker-compose.yml"])
def test_dockerignore_keeps_what_the_build_needs(path):
    assert not dockerignored(path)
