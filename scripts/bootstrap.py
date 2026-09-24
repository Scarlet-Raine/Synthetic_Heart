#!/usr/bin/env python3
"""One-shot native bootstrap for Synthetic Heart.

This is the single place that prepares a *native* (non-Docker) install of SyntH.
The platform installers only install operating-system packages and ``uv``; they
then hand over to this script, so Linux, Windows and any future package format
share one implementation of "make SyntH runnable here".

It performs, in order:

1. resolve a PostgreSQL server: an existing one, or a private cluster it creates
   and manages under the data directory (``--portable``);
2. create the application role, the database and the ``vector`` / ``pg_trgm``
   extensions (the app creates its own tables afterwards);
3. choose free ports for the WebUI and the OpenAI-compatible API;
4. write a minimal **technical** ``.env`` (database, ports, host bindings). The
   persona, location, timezone and engine keys are deliberately NOT written
   here: those come from the WebUI setup page, which stores them in the config
   registry;
5. sync the Python environment with ``uv`` (optionally with the ``local-voice``
   extra);
6. smoke-test the database and print the WebUI URL.

Usage::

    uv run --no-project python scripts/bootstrap.py            # normal install
    python3 scripts/bootstrap.py --dry-run                     # show the plan
    python3 scripts/bootstrap.py --portable                    # private PG cluster
    python3 scripts/bootstrap.py --extra local-voice           # add offline TTS/STT
    python3 scripts/bootstrap.py --non-interactive --json      # CI

Everything is stdlib-only on purpose: it has to run before ``uv sync`` has
installed a single dependency.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DB_NAME = "synth"
DEFAULT_DB_USER = "synth"
DEFAULT_DB_PORT = 5432
DEFAULT_WEBUI_PORT = 8080
DEFAULT_API_PORT = 11435

#: Ports the bootstrap will try in order when the preferred one is taken.
PORT_SEARCH_SPAN = 40

STATE_FILE_NAME = "bootstrap-state.json"
SUPERUSER_PWFILE_NAME = "pgsql-superuser.pw"


# ---------------------------------------------------------------------------
# Shared path resolution (loaded from core/app_paths.py without importing the
# ``core`` package, so this works before any dependency exists).
# ---------------------------------------------------------------------------


def _load_app_paths() -> object:
    module_path = REPO_ROOT / "core" / "app_paths.py"
    spec = importlib.util.spec_from_file_location("_synth_app_paths", module_path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_APP_PATHS = _load_app_paths()
app_root = _APP_PATHS.app_root  # type: ignore[attr-defined]
data_root = _APP_PATHS.data_root  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


class Reporter:
    """Small progress reporter: four meaningful steps, no scrolling log."""

    def __init__(
        self,
        total: int,
        *,
        quiet: bool = False,
        json_mode: bool = False,
        log_file: str | None = None,
    ):
        self.total = max(total, 1)
        self.index = 0
        self.quiet = quiet
        self.json_mode = json_mode
        self.log_file = log_file
        self.warnings: list[str] = []
        self.messages: list[str] = []

    def to_log(self, text: str) -> None:
        """Append a line to the log file, when one was asked for.

        The Windows installer runs this script with its window hidden, so
        without a log a failure reaches the user as a bare exit code and
        nothing else. Fail-safe on purpose: a log that cannot be written must
        never be the reason an install fails.
        """
        if not self.log_file:
            return
        try:
            with open(self.log_file, "a", encoding="utf-8") as handle:
                handle.write(text + "\n")
        except OSError:
            pass

    def _emit(self, text: str, *, stream: TextIO = sys.stdout) -> None:
        self.messages.append(text)
        self.to_log(text)
        if self.json_mode or self.quiet:
            return
        print(text, file=stream, flush=True)

    def step(self, text: str) -> None:
        self.index += 1
        self._emit(f"[{self.index}/{self.total}] {text}")

    def detail(self, text: str) -> None:
        self._emit(f"      {text}")

    def ok(self, text: str) -> None:
        self._emit(f"      ok: {text}")

    def warn(self, text: str) -> None:
        self.warnings.append(text)
        self._emit(f"      warning: {text}", stream=sys.stderr)

    def fail(self, text: str) -> None:
        self._emit(f"      ERROR: {text}", stream=sys.stderr)


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested in tests/test_bootstrap.py)
# ---------------------------------------------------------------------------


def generate_password(length: int = 32) -> str:
    """Return a URL-safe random password with no shell- or DSN-hostile chars."""
    return secrets.token_urlsafe(length)


def parse_env_file(text: str) -> dict[str, str]:
    """Parse a ``.env`` file's ``KEY=VALUE`` lines.

    Comments, blank lines and lines without ``=`` are ignored, and surrounding
    quotes are stripped, matching how the application loads its own ``.env``.
    """
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def render_env_file(values: dict[str, str], *, header: str) -> str:
    """Render ``values`` as a deterministic ``.env`` body."""
    lines = [header.rstrip(), ""]
    for key in sorted(values):
        lines.append(f"{key}={values[key]}")
    lines.append("")
    return "\n".join(lines)


def merge_env_values(
    existing: dict[str, str], generated: dict[str, str]
) -> dict[str, str]:
    """Overlay ``generated`` on ``existing`` without discarding unknown keys.

    Re-running the bootstrap must never throw away settings the user added by
    hand, so only the keys the bootstrap owns are replaced.
    """
    merged = dict(existing)
    merged.update(generated)
    return merged


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    """Return True when nothing is listening on ``host:port``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((host, port)) != 0


def choose_port(preferred: int, *, excluded: set[int] | None = None) -> int:
    """Return ``preferred`` when free, else the next free port above it."""
    taken = set(excluded or set())
    for candidate in range(preferred, preferred + PORT_SEARCH_SPAN):
        if candidate in taken:
            continue
        if port_is_free(candidate):
            return candidate
    raise RuntimeError(f"no free port in [{preferred}, {preferred + PORT_SEARCH_SPAN})")


def build_dsn(host: str, port: int, user: str, password: str, database: str) -> str:
    """Build a PostgreSQL DSN with the password percent-encoded."""
    return (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{quote(database, safe='')}"
    )


def pg_bin_candidates(explicit: str | None = None) -> list[Path]:
    """Return candidate ``bin`` directories holding psql/initdb/pg_ctl.

    Highest priority first: an explicit ``--pg-bin``, ``SYNTH_PG_BIN``, the
    bundled portable tree the installer unpacks under the application root, the
    private cluster directory, then the usual system locations.
    """
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env_bin = os.environ.get("SYNTH_PG_BIN", "").strip()
    if env_bin:
        candidates.append(Path(env_bin).expanduser())
    candidates.append(app_root() / "pgsql" / "bin")
    candidates.append(data_root() / "pgsql" / "bin")

    system = platform.system()
    if system == "Windows":
        program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        postgres_root = program_files / "PostgreSQL"
        if postgres_root.is_dir():
            found = sorted(
                (entry for entry in postgres_root.iterdir() if entry.is_dir()),
                key=lambda entry: _version_key(entry.name),
                reverse=True,
            )
            candidates.extend(entry / "bin" for entry in found)
    else:
        for root in (Path("/usr/lib/postgresql"), Path("/usr/pgsql")):
            if root.is_dir():
                found = sorted(
                    (entry for entry in root.iterdir() if entry.is_dir()),
                    key=lambda entry: _version_key(entry.name),
                    reverse=True,
                )
                candidates.extend(entry / "bin" for entry in found)
        candidates.extend(
            [Path("/usr/local/bin"), Path("/usr/bin"), Path("/opt/homebrew/bin")]
        )

    return candidates


def _version_key(name: str) -> tuple[int, ...]:
    """Sort key that orders ``16`` above ``9.6`` and tolerates odd names."""
    parts = re.findall(r"\d+", name)
    return tuple(int(part) for part in parts) if parts else (0,)


def find_pg_tool(tool: str, explicit_bin: str | None = None) -> str | None:
    """Return the path to a PostgreSQL tool (``psql``, ``initdb``, ``pg_ctl``)."""
    for directory in pg_bin_candidates(explicit_bin):
        for name in (tool, f"{tool}.exe"):
            candidate = directory / name
            if candidate.is_file():
                return str(candidate)
    found = shutil.which(tool)
    return found


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        return (self.stdout + "\n" + self.stderr).strip()


def run_command(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    timeout: int = 300,
    capture: bool = True,
) -> CommandResult:
    """Run a subprocess, capturing output, and never raise on a nonzero exit.

    stdin is closed: an unattended install must fail on a prompt, never wait for
    one. (psql asking for a password with nowhere to type it is the classic hang.)
    """
    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)
    try:
        completed = subprocess.run(
            command,
            input=input_text,
            capture_output=capture,
            text=True,
            env=merged_env,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL if input_text is None else None,
        )
    except FileNotFoundError as exc:
        return CommandResult(127, "", f"{command[0]}: {exc}")
    except subprocess.TimeoutExpired:
        return CommandResult(
            124, "", f"timed out after {timeout}s: {' '.join(command)}"
        )
    return CommandResult(
        completed.returncode,
        completed.stdout or "",
        completed.stderr or "",
    )


# ---------------------------------------------------------------------------
# PostgreSQL server resolution
# ---------------------------------------------------------------------------


@dataclass
class PostgresTarget:
    """Where the application database lives."""

    host: str
    port: int
    superuser: str
    superuser_password: str | None
    psql: str
    managed_cluster: Path | None = None
    pg_ctl: str | None = None
    #: True when administrative statements must go through ``sudo -u postgres``
    #: (peer authentication, the Debian/Ubuntu default) instead of a TCP
    #: connection with a password.
    via_sudo: bool = False


@dataclass
class BootstrapPlan:
    """Everything the bootstrap decided, for ``--dry-run`` and ``--json``."""

    target: PostgresTarget | None = None
    db_name: str = DEFAULT_DB_NAME
    db_user: str = DEFAULT_DB_USER
    db_password: str = ""
    webui_port: int = DEFAULT_WEBUI_PORT
    api_port: int = DEFAULT_API_PORT
    env_values: dict[str, str] = field(default_factory=dict)
    sync_command: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _cluster_dir() -> Path:
    return data_root() / "pgsql"


def _superuser_pwfile() -> Path:
    return _cluster_dir() / SUPERUSER_PWFILE_NAME


def _read_superuser_password() -> str | None:
    path = _superuser_pwfile()
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except Exception:
        return None


def _write_superuser_password(password: str) -> None:
    path = _superuser_pwfile()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(password, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except Exception:
        # Windows ACLs are inherited from the user profile; a chmod failure here
        # is not fatal, the file lives inside the user's own data directory.
        pass


def resolve_existing_server(
    reporter: Reporter,
    *,
    host: str,
    port: int,
    superuser: str,
    password: str | None = None,
    via_sudo: bool | None = None,
) -> PostgresTarget | None:
    """Return a target for a reachable, already-running PostgreSQL server.

    ``via_sudo`` is tri-state: ``True`` forces the peer-auth transport, ``False``
    forbids it, ``None`` (the default) falls back to it when a direct connection
    cannot authenticate — the usual case for a distribution-provided PostgreSQL,
    where the ``postgres`` role has no password and only trusts local sockets.
    """
    psql = find_pg_tool("psql")
    if not psql:
        return None
    password = (
        password
        or os.environ.get("PGPASSWORD")
        or os.environ.get("SYNTH_PG_SUPERUSER_PASSWORD")
    )
    target = PostgresTarget(
        host=host,
        port=port,
        superuser=superuser,
        superuser_password=password,
        psql=psql,
    )
    probe = psql_query(target, "SELECT 1")
    if probe.ok:
        reporter.detail(f"found a PostgreSQL server on {host}:{port}")
        return target

    if via_sudo is not False and sudo_psql_available(psql, superuser):
        sudo_target = PostgresTarget(
            host=host,
            port=port,
            superuser=superuser,
            superuser_password=None,
            psql=psql,
            via_sudo=True,
        )
        if psql_query(sudo_target, "SELECT 1").ok:
            reporter.detail(
                f"using the local PostgreSQL server as '{superuser}' via sudo "
                "(peer authentication)"
            )
            if port_is_free(port, host):
                reporter.warn(
                    f"nothing is listening on {host}:{port} for TCP connections; "
                    "SyntH itself connects over TCP"
                )
            return sudo_target

    reporter.detail(f"no PostgreSQL answering on {host}:{port}")
    return None


def ensure_portable_cluster(
    reporter: Reporter,
    *,
    pg_bin: str | None,
    port: int,
) -> PostgresTarget | None:
    """Create (once) and start a private PostgreSQL cluster under the data dir."""
    initdb = find_pg_tool("initdb", pg_bin)
    pg_ctl = find_pg_tool("pg_ctl", pg_bin)
    psql = find_pg_tool("psql", pg_bin)
    if not initdb or not pg_ctl or not psql:
        reporter.warn(
            "portable PostgreSQL binaries not found (looked for initdb/pg_ctl/psql); "
            "pass --pg-bin <dir> or install PostgreSQL"
        )
        return None

    cluster = _cluster_dir()
    superuser_password = _read_superuser_password()
    if not (cluster / "PG_VERSION").is_file():
        reporter.detail(f"creating a private PostgreSQL cluster in {cluster}")
        # Only the parent is created: initdb insists on creating the cluster
        # directory itself and refuses a directory that is not empty.
        cluster.parent.mkdir(parents=True, exist_ok=True)
        superuser_password = generate_password(24)
        # The password file must live OUTSIDE the cluster directory, or initdb
        # rejects it with "exists but is not empty".
        pwfile = cluster.parent / ".initdb-pw"
        pwfile.write_text(superuser_password, encoding="utf-8")
        try:
            os.chmod(pwfile, 0o600)
        except Exception:
            pass
        try:
            result = run_command(
                [
                    initdb,
                    "-D",
                    str(cluster),
                    "-U",
                    "postgres",
                    "--auth-local=trust",
                    "--auth-host=scram-sha-256",
                    f"--pwfile={pwfile}",
                    "--encoding=UTF8",
                ],
                timeout=600,
            )
        finally:
            try:
                pwfile.unlink()
            except Exception:
                pass
        if not result.ok:
            reporter.fail(f"initdb failed: {result.output}")
            return None
        _write_superuser_password(superuser_password)
    elif not superuser_password:
        reporter.warn(
            f"cluster exists but {_superuser_pwfile()} is missing; "
            "re-run with --pg-superuser-password"
        )
        superuser_password = os.environ.get("SYNTH_PG_SUPERUSER_PASSWORD")
        if not superuser_password:
            return None

    log_file = cluster / "postgres.log"
    start = run_command(
        [
            pg_ctl,
            "-D",
            str(cluster),
            "-l",
            str(log_file),
            "-o",
            f"-p {port} -c listen_addresses=127.0.0.1",
            "-w",
            "start",
        ],
        timeout=120,
    )
    if not start.ok and "already running" not in start.output.lower():
        reporter.fail(f"pg_ctl start failed: {start.output}")
        return None

    target = PostgresTarget(
        host="127.0.0.1",
        port=port,
        superuser="postgres",
        superuser_password=superuser_password,
        psql=psql,
        managed_cluster=cluster,
        pg_ctl=pg_ctl,
    )
    for _ in range(30):
        if psql_query(target, "SELECT 1").ok:
            reporter.detail(f"private cluster is up on 127.0.0.1:{port}")
            return target
        time.sleep(1)
    reporter.fail("the private cluster did not become ready")
    return None


def stop_portable_cluster(reporter: Reporter, *, pg_bin: str | None = None) -> bool:
    """Stop the private cluster, if one exists. Used by the uninstaller.

    Returns True when there is nothing left running. The uninstaller deletes the
    PostgreSQL binaries, and Windows will not delete files a process holds open,
    so this has to happen before the directory is removed.
    """
    cluster = _cluster_dir()
    if not (cluster / "PG_VERSION").is_file():
        reporter.detail("no private cluster to stop")
        return True

    pg_ctl = find_pg_tool("pg_ctl", pg_bin)
    if not pg_ctl:
        reporter.warn(
            "pg_ctl was not found; the cluster cannot be stopped automatically"
        )
        return False

    # -m fast lets open connections finish their current statement and then
    # disconnects them, which is what we want for a shutdown we asked for.
    result = run_command(
        [pg_ctl, "-D", str(cluster), "-m", "fast", "-w", "stop"], timeout=120
    )
    if result.ok or "not running" in result.output.lower():
        reporter.detail("private cluster stopped")
        return True
    reporter.warn(f"pg_ctl stop reported: {result.output.strip()}")
    return False


def psql_query(
    target: PostgresTarget, sql: str, *, database: str = "postgres"
) -> CommandResult:
    """Run one SQL statement as the superuser on ``database``.

    Two transports: a TCP connection authenticated with the superuser password,
    or ``sudo -n -u <superuser> psql`` for a system PostgreSQL that trusts local
    peer connections (the Debian/Ubuntu default). ``-n`` makes sudo fail instead
    of prompting, so a missing sudo right is an error rather than a hang.
    """
    if target.via_sudo:
        return run_command(
            [
                "sudo",
                "-n",
                "-u",
                target.superuser,
                target.psql,
                "-d",
                database,
                "-v",
                "ON_ERROR_STOP=1",
                "-tAc",
                sql,
            ],
            timeout=60,
        )
    env = {"PGPASSWORD": target.superuser_password or ""}
    return run_command(
        [
            target.psql,
            # -w: never prompt for a password. Without it a rejected password
            # turns into a hang on an unattended machine.
            "-w",
            "-h",
            target.host,
            "-p",
            str(target.port),
            "-U",
            target.superuser,
            "-d",
            database,
            "-v",
            "ON_ERROR_STOP=1",
            "-tAc",
            sql,
        ],
        env=env,
        timeout=60,
    )


def sudo_psql_available(psql: str, superuser: str = "postgres") -> bool:
    """Return True when ``sudo -n -u <superuser> psql`` can run a query."""
    if os.name == "nt":
        return False
    result = run_command(
        ["sudo", "-n", "-u", superuser, psql, "-d", "postgres", "-tAc", "SELECT 1"],
        timeout=20,
    )
    return result.ok and result.stdout.strip() == "1"


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def ensure_database(
    reporter: Reporter,
    target: PostgresTarget,
    *,
    db_name: str,
    db_user: str,
    db_password: str,
) -> bool:
    """Create the role and the database, idempotently."""
    role_exists = psql_query(
        target, f"SELECT 1 FROM pg_roles WHERE rolname = '{db_user}'"
    )
    if not role_exists.ok:
        reporter.fail(f"cannot inspect roles: {role_exists.output}")
        return False
    if role_exists.stdout.strip() == "1":
        reset = psql_query(
            target, f"ALTER ROLE {db_user} WITH LOGIN PASSWORD '{db_password}'"
        )
        if not reset.ok:
            reporter.fail(f"cannot update the role password: {reset.output}")
            return False
        reporter.detail(f"role '{db_user}' already exists (password refreshed)")
    else:
        created = psql_query(
            target, f"CREATE ROLE {db_user} WITH LOGIN PASSWORD '{db_password}'"
        )
        if not created.ok:
            reporter.fail(f"cannot create role '{db_user}': {created.output}")
            return False
        reporter.detail(f"created role '{db_user}'")

    db_exists = psql_query(
        target, f"SELECT 1 FROM pg_database WHERE datname = '{db_name}'"
    )
    if not db_exists.ok:
        reporter.fail(f"cannot inspect databases: {db_exists.output}")
        return False
    if db_exists.stdout.strip() == "1":
        reporter.detail(f"database '{db_name}' already exists")
    else:
        created_db = psql_query(target, f'CREATE DATABASE "{db_name}" OWNER {db_user}')
        if not created_db.ok:
            reporter.fail(f"cannot create database '{db_name}': {created_db.output}")
            return False
        reporter.detail(f"created database '{db_name}'")

    granted = psql_query(
        target, f'GRANT ALL PRIVILEGES ON DATABASE "{db_name}" TO {db_user}'
    )
    if not granted.ok:
        reporter.warn(f"could not grant database privileges: {granted.output}")
    return True


def ensure_extensions(
    reporter: Reporter, target: PostgresTarget, *, db_name: str, db_user: str
) -> dict[str, bool]:
    """Install the extensions the application uses and report what is available.

    ``vector`` (pgvector) powers semantic memory search. It is a separate OS
    package on Linux and a separate DLL on Windows that the base PostgreSQL
    binaries do not include, so its absence is reported rather than fatal: the
    caller keeps the SOUL repository in memory instead and tells the user.
    """
    available: dict[str, bool] = {}
    for extension in ("vector", "pg_trgm"):
        result = psql_query(
            target, f"CREATE EXTENSION IF NOT EXISTS {extension}", database=db_name
        )
        available[extension] = result.ok
        if result.ok:
            reporter.detail(f"extension '{extension}' ensured")
        else:
            reason = (
                result.output.strip().splitlines()[-1] if result.output.strip() else ""
            )
            reporter.warn(f"extension '{extension}' unavailable: {reason}")

    schema_grant = psql_query(
        target,
        f"GRANT ALL ON SCHEMA public TO {db_user}; "
        f"ALTER SCHEMA public OWNER TO {db_user}",
        database=db_name,
    )
    if not schema_grant.ok:
        reporter.warn(
            f"could not hand the public schema to '{db_user}': {schema_grant.output}"
        )
    return available


def build_env_values(
    *,
    target: PostgresTarget,
    db_name: str,
    db_user: str,
    db_password: str,
    webui_port: int,
    api_port: int,
    soul_backend: str = "postgres",
) -> dict[str, str]:
    """Return the technical ``.env`` values a native install needs.

    Persona, location, timezone and engine credentials are intentionally absent:
    they belong to the config registry and are collected by the WebUI setup page.

    ``soul_backend`` is ``postgres`` only when pgvector is available, because the
    SOUL schema declares a ``VECTOR(768)`` column; otherwise the caller asks for
    ``memory`` so the app starts instead of failing on a missing extension.
    """
    return {
        "DB_HOST": target.host,
        "DB_PORT": str(target.port),
        "DB_USER": db_user,
        "DB_PASS": db_password,
        "DB_NAME": db_name,
        "SYNTH_DB_TYPE": "postgres",
        "SYNTH_PRIMARY_DB": "soul",
        "SOUL_REPOSITORY_BACKEND": soul_backend,
        "SOUL_POSTGRES_DSN": build_dsn(
            target.host, target.port, db_user, db_password, db_name
        ),
        "SYNTH_WEBUI_HOST": "127.0.0.1",
        "SYNTH_WEBUI_TLS": "0",
        "SYNTH_WEBUI_HTTP_PORT": str(webui_port),
        "OLLAMA_HOST": "127.0.0.1",
        "OPENAI_API_SERVER_PORT": str(api_port),
        "SYNTH_IN_CONTAINER": "0",
        "SYNTH_HOST_OS": "windows" if platform.system() == "Windows" else "linux",
    }


def write_env(reporter: Reporter, *, values: dict[str, str], env_path: Path) -> None:
    """Write the generated ``.env``, preserving unrelated hand-written keys."""
    existing: dict[str, str] = {}
    if env_path.is_file():
        try:
            existing = parse_env_file(env_path.read_text(encoding="utf-8"))
        except Exception as exc:
            reporter.warn(f"could not read the existing .env ({exc}); rewriting it")
    merged = merge_env_values(existing, values)
    header = (
        "# SyntH native configuration - written by scripts/bootstrap.py.\n"
        "# Technical values only: database, ports, host bindings.\n"
        "# Your Synth's name, profile, location, timezone and engine keys live in\n"
        "# the config registry and are set from the WebUI setup page.\n"
        "# Re-running the bootstrap refreshes only the keys it owns.\n"
        "# See .env.example for the full documented reference."
    )
    env_path.write_text(render_env_file(merged, header=header), encoding="utf-8")
    reporter.detail(f"wrote {env_path}")


def sync_environment(reporter: Reporter, *, extras: list[str], dry_run: bool) -> bool:
    """Install the Python environment with uv."""
    uv = shutil.which("uv")
    if not uv:
        reporter.fail(
            "uv was not found on PATH. Install it first: "
            "https://docs.astral.sh/uv/getting-started/installation/"
        )
        return False
    command = [uv, "sync"]
    for extra in extras:
        command += ["--extra", extra]
    reporter.detail(" ".join(command))
    if dry_run:
        return True
    result = run_command(command, timeout=3600, capture=False)
    if not result.ok:
        reporter.fail(
            "dependency sync failed. If the network is flaky, re-run; the "
            "'kittentts' wheel is fetched from GitHub and is the usual culprit."
        )
        return False
    return True


def smoke_test(reporter: Reporter, *, env_path: Path) -> bool:
    """Connect to the database with the generated credentials."""
    values = parse_env_file(env_path.read_text(encoding="utf-8"))
    psql = find_pg_tool("psql")
    if not psql:
        reporter.warn("psql not found; skipping the database smoke test")
        return True
    result = run_command(
        [
            psql,
            "-h",
            values.get("DB_HOST", "127.0.0.1"),
            "-p",
            values.get("DB_PORT", str(DEFAULT_DB_PORT)),
            "-U",
            values.get("DB_USER", DEFAULT_DB_USER),
            "-d",
            values.get("DB_NAME", DEFAULT_DB_NAME),
            "-tAc",
            "SELECT extname FROM pg_extension ORDER BY extname",
        ],
        env={"PGPASSWORD": values.get("DB_PASS", "")},
        timeout=30,
    )
    if not result.ok:
        reporter.fail(f"database smoke test failed: {result.output}")
        return False
    installed = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    reporter.detail(f"connected; extensions: {', '.join(installed) or 'none'}")
    return True


def state_path() -> Path:
    return data_root() / STATE_FILE_NAME


def write_state(plan: BootstrapPlan, *, env_path: Path) -> None:
    """Record what was created so updates and uninstalls can find it again."""
    payload = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "app_root": str(app_root()),
        "data_root": str(data_root()),
        "env_path": str(env_path),
        "database": {
            "host": plan.target.host if plan.target else "",
            "port": plan.target.port if plan.target else 0,
            "name": plan.db_name,
            "user": plan.db_user,
        },
        "managed_cluster": (
            str(plan.target.managed_cluster)
            if plan.target and plan.target.managed_cluster
            else None
        ),
        "ports": {"webui": plan.webui_port, "api": plan.api_port},
    }
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare a native Synthetic Heart install (database, .env, deps).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="show the plan, change nothing"
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="never prompt; fail instead of asking for credentials",
    )
    parser.add_argument(
        "--portable",
        action="store_true",
        help="create and manage a private PostgreSQL cluster under the data directory",
    )
    parser.add_argument(
        "--pg-bin", default=None, help="directory holding psql/initdb/pg_ctl"
    )
    parser.add_argument("--pg-host", default=os.environ.get("PGHOST", "127.0.0.1"))
    parser.add_argument(
        "--pg-port", type=int, default=int(os.environ.get("PGPORT", DEFAULT_DB_PORT))
    )
    parser.add_argument("--pg-superuser", default=os.environ.get("PGUSER", "postgres"))
    parser.add_argument(
        "--pg-superuser-password",
        default=os.environ.get("SYNTH_PG_SUPERUSER_PASSWORD"),
        help="password for the PostgreSQL superuser (or set SYNTH_PG_SUPERUSER_PASSWORD)",
    )
    parser.add_argument(
        "--pg-via-sudo",
        dest="pg_via_sudo",
        action="store_true",
        default=None,
        help="administer a system PostgreSQL through 'sudo -n -u postgres psql' (peer auth)",
    )
    parser.add_argument(
        "--no-pg-via-sudo",
        dest="pg_via_sudo",
        action="store_false",
        help="never use sudo; require a password-authenticated superuser connection",
    )
    parser.add_argument("--db-name", default=DEFAULT_DB_NAME)
    parser.add_argument("--db-user", default=DEFAULT_DB_USER)
    parser.add_argument(
        "--db-password",
        default=None,
        help="reuse this password instead of generating one",
    )
    parser.add_argument("--webui-port", type=int, default=DEFAULT_WEBUI_PORT)
    parser.add_argument("--api-port", type=int, default=DEFAULT_API_PORT)
    parser.add_argument(
        "--extra",
        action="append",
        default=[],
        help="uv extra to install (repeatable), e.g. --extra local-voice",
    )
    parser.add_argument("--skip-sync", action="store_true", help="do not run uv sync")
    parser.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    parser.add_argument(
        "--no-browser", action="store_true", help="never open a browser"
    )
    parser.add_argument(
        "--json", action="store_true", help="machine-readable summary on stdout"
    )
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--stop-cluster",
        action="store_true",
        help="stop the private PostgreSQL cluster and exit (used by the uninstaller)",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help=(
            "append progress and warnings to this file (the Windows installer "
            "passes one so a hidden run is still diagnosable)"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    reporter = Reporter(
        5, quiet=args.quiet, json_mode=args.json, log_file=args.log_file
    )
    if args.log_file:
        # Log the arguments this run was actually given: when the script is
        # invoked as a CLI that is sys.argv, but a programmatic caller (a test,
        # or another script) passes its own list and sys.argv would be wrong.
        invoked = argv if argv is not None else sys.argv[1:]
        reporter.to_log(
            f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} "
            f"bootstrap.py {' '.join(invoked)} ==="
        )
    if args.stop_cluster:
        # Always exit 0: the uninstaller cannot act on a failure code, and a
        # cluster that will not stop must not block the rest of the uninstall.
        stop_portable_cluster(reporter, pg_bin=args.pg_bin)
        return 0
    env_path = Path(args.env_file).expanduser()
    plan = BootstrapPlan(db_name=args.db_name, db_user=args.db_user)

    if not reporter.json_mode and not args.quiet:
        print("Synthetic Heart - native setup")
        print(f"  application: {app_root()}")
        print(f"  data:        {data_root()}")
        print("")

    # --- 1. database server -------------------------------------------------
    reporter.step("Locating a database engine")
    target: PostgresTarget | None = None
    if args.dry_run:
        # A dry run probes but never creates: the portable cluster is only
        # reported, never initialised.
        target = resolve_existing_server(
            reporter,
            host=args.pg_host,
            port=args.pg_port,
            superuser=args.pg_superuser,
            password=args.pg_superuser_password,
            via_sudo=args.pg_via_sudo,
        )
        if target is None:
            reporter.detail(
                f"no server answering; a real run would create a private cluster "
                f"in {_cluster_dir()} when --portable is given"
            )
    elif args.portable:
        target = ensure_portable_cluster(
            reporter, pg_bin=args.pg_bin, port=args.pg_port
        )
    else:
        target = resolve_existing_server(
            reporter,
            host=args.pg_host,
            port=args.pg_port,
            superuser=args.pg_superuser,
            password=args.pg_superuser_password,
            via_sudo=args.pg_via_sudo,
        )
        if target is None:
            reporter.detail("falling back to a private cluster")
            target = ensure_portable_cluster(
                reporter, pg_bin=args.pg_bin, port=args.pg_port
            )
    if target is None and args.dry_run:
        target = PostgresTarget(
            host=args.pg_host,
            port=args.pg_port,
            superuser=args.pg_superuser,
            superuser_password=None,
            psql=find_pg_tool("psql") or "psql",
        )
    if target is None:
        reporter.fail(
            "no usable PostgreSQL server. Install PostgreSQL + pgvector, or re-run "
            "with --portable and --pg-bin <postgres bin dir>."
        )
        return 2
    plan.target = target
    reporter.ok(f"database engine at {target.host}:{target.port}")

    # --- 2. role, database, extensions -------------------------------------
    reporter.step("Preparing the SyntH database")
    plan.db_password = args.db_password or generate_password(32)
    extensions: dict[str, bool] = {}
    if args.dry_run:
        reporter.detail(
            f"would create role '{args.db_user}' and database '{args.db_name}'"
        )
    else:
        if not ensure_database(
            reporter,
            target,
            db_name=args.db_name,
            db_user=args.db_user,
            db_password=plan.db_password,
        ):
            return 3
        extensions = ensure_extensions(
            reporter, target, db_name=args.db_name, db_user=args.db_user
        )
    vector_available = extensions.get("vector", True)
    if not vector_available:
        reporter.warn(
            "semantic memory search is off: the 'vector' (pgvector) extension is "
            "missing. On Linux install postgresql-<version>-pgvector and re-run; "
            "on Windows the installer bundles it."
        )
    reporter.ok("database ready")

    # --- 3. ports -----------------------------------------------------------
    reporter.step("Choosing local ports")
    used: set[int] = set()
    plan.webui_port = choose_port(args.webui_port, excluded=used)
    used.add(plan.webui_port)
    plan.api_port = choose_port(args.api_port, excluded=used)
    reporter.detail(
        f"WebUI: {plan.webui_port}   OpenAI-compatible API: {plan.api_port}"
    )
    if plan.webui_port != args.webui_port:
        reporter.warn(
            f"port {args.webui_port} was busy; using {plan.webui_port} for the WebUI"
        )

    # --- 4. .env ------------------------------------------------------------
    reporter.step("Writing the configuration")
    plan.env_values = build_env_values(
        target=target,
        db_name=args.db_name,
        db_user=args.db_user,
        db_password=plan.db_password,
        webui_port=plan.webui_port,
        api_port=plan.api_port,
        soul_backend="postgres" if vector_available else "memory",
    )
    if args.dry_run:
        reporter.detail(f"would write {env_path}")
    else:
        try:
            write_env(reporter, values=plan.env_values, env_path=env_path)
        except Exception as exc:
            reporter.fail(f"could not write {env_path}: {exc}")
            return 5
    reporter.ok("configuration written")

    # --- 5. dependencies + smoke test ---------------------------------------
    reporter.step("Installing components")
    if args.skip_sync:
        reporter.detail("skipped (--skip-sync)")
    elif not sync_environment(reporter, extras=args.extra, dry_run=args.dry_run):
        return 6
    if not args.dry_run:
        if not smoke_test(reporter, env_path=env_path):
            return 7
        try:
            write_state(plan, env_path=env_path)
        except Exception as exc:
            reporter.warn(f"could not write the bootstrap state file: {exc}")
    reporter.ok("components installed")

    url = f"http://127.0.0.1:{plan.webui_port}"
    if args.json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "dry_run": args.dry_run,
                    "app_root": str(app_root()),
                    "data_root": str(data_root()),
                    "env_path": str(env_path),
                    "database": {
                        "host": target.host,
                        "port": target.port,
                        "name": plan.db_name,
                        "user": plan.db_user,
                        "managed": bool(target.managed_cluster),
                    },
                    "ports": {"webui": plan.webui_port, "api": plan.api_port},
                    "url": url,
                    "warnings": reporter.warnings,
                },
                indent=2,
            )
        )
    elif not args.quiet:
        print("")
        print(f"  Your Synth is ready: {url}")
        print("  The first page you see sets up their name, your name, your location")
        print("  and the engine + API key they should think with.")
        print("")
        print("  Start:   uv run main.py       (native install)")
        launcher = Path.home() / ".local" / "bin" / "synth"
        if os.name != "nt" and launcher.is_file():
            print("           synth                 (launcher, opens the WebUI)")

    if not args.no_browser and not args.dry_run and not args.quiet:
        _open_browser(url)
    return 0


def _open_browser(url: str) -> None:
    """Best-effort: open the WebUI so the user lands on the setup page."""
    try:
        import webbrowser

        webbrowser.open(url)
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
