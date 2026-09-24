"""Unit tests for the native bootstrap helper logic.

``scripts/bootstrap.py`` must run before any dependency exists, so it is
stdlib-only and its decision-making is separated into pure functions. Those are
what these tests cover: no database, no network, no uv sync.
"""

from __future__ import annotations

import importlib.util
import string
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_bootstrap() -> object:
    import sys

    name = "_synth_bootstrap_under_test"
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "scripts" / "bootstrap.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # ``@dataclass`` resolves annotations through ``sys.modules``, so the module
    # has to be registered before it is executed.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


bootstrap = _load_bootstrap()


# ---------------------------------------------------------------------------
# passwords and DSNs
# ---------------------------------------------------------------------------


def test_generated_password_is_url_safe_and_long() -> None:
    password = bootstrap.generate_password(32)
    assert len(password) >= 32
    assert set(password) <= set(string.ascii_letters + string.digits + "-_")


def test_generated_passwords_do_not_repeat() -> None:
    assert len({bootstrap.generate_password(24) for _ in range(50)}) == 50


def test_dsn_encodes_hostile_password_characters() -> None:
    dsn = bootstrap.build_dsn(
        "127.0.0.1", 5433, "synth", "p@ss word/with#chars", "synth"
    )
    assert dsn.startswith("postgresql://synth:")
    assert " " not in dsn
    assert "#" not in dsn
    assert dsn.endswith("@127.0.0.1:5433/synth")


# ---------------------------------------------------------------------------
# .env handling
# ---------------------------------------------------------------------------


def test_parse_env_file_ignores_comments_and_handles_quotes() -> None:
    text = (
        "# a comment\n"
        "\n"
        "DB_HOST=127.0.0.1\n"
        "DB_PASS='quoted value'\n"
        'DB_NAME="synth"\n'
        "NOT_AN_ASSIGNMENT\n"
        "EMPTY=\n"
    )
    values = bootstrap.parse_env_file(text)
    assert values["DB_HOST"] == "127.0.0.1"
    assert values["DB_PASS"] == "quoted value"
    assert values["DB_NAME"] == "synth"
    assert values["EMPTY"] == ""
    assert "NOT_AN_ASSIGNMENT" not in values
    assert "a comment" not in values


def test_merge_env_preserves_hand_written_keys() -> None:
    existing = {"DB_HOST": "old-host", "MY_HAND_EDITED_KEY": "keep-me"}
    generated = {"DB_HOST": "127.0.0.1", "DB_PORT": "5433"}
    merged = bootstrap.merge_env_values(existing, generated)
    assert merged["DB_HOST"] == "127.0.0.1"
    assert merged["MY_HAND_EDITED_KEY"] == "keep-me"
    assert merged["DB_PORT"] == "5433"


def test_render_env_file_is_sorted_and_ends_with_newline() -> None:
    body = bootstrap.render_env_file({"B": "2", "A": "1"}, header="# header")
    assert body.startswith("# header")
    assert body.index("A=1") < body.index("B=2")
    assert body.endswith("\n")


def test_round_trip_parse_render_parse() -> None:
    values = {"DB_HOST": "127.0.0.1", "DB_PASS": "a-b_c", "DB_NAME": "synth"}
    body = bootstrap.render_env_file(values, header="# x")
    assert bootstrap.parse_env_file(body) == values


# ---------------------------------------------------------------------------
# ports
# ---------------------------------------------------------------------------


def test_choose_port_prefers_the_default_when_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bootstrap, "port_is_free", lambda port, host="127.0.0.1": True)
    assert bootstrap.choose_port(8080) == 8080


def test_choose_port_skips_busy_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    busy = {8080, 8081, 8082}
    monkeypatch.setattr(
        bootstrap, "port_is_free", lambda port, host="127.0.0.1": port not in busy
    )
    assert bootstrap.choose_port(8080) == 8083


def test_choose_port_honours_exclusions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "port_is_free", lambda port, host="127.0.0.1": True)
    assert bootstrap.choose_port(8080, excluded={8080, 8081}) == 8082


def test_choose_port_raises_when_the_range_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bootstrap, "port_is_free", lambda port, host="127.0.0.1": False)
    with pytest.raises(RuntimeError):
        bootstrap.choose_port(8080)


# ---------------------------------------------------------------------------
# PostgreSQL binary discovery
# ---------------------------------------------------------------------------


def test_pg_bin_candidates_put_explicit_and_bundled_first() -> None:
    candidates = bootstrap.pg_bin_candidates("C:/custom/pg/bin")
    assert Path("C:/custom/pg/bin") == candidates[0]
    assert bootstrap.app_root() / "pgsql" / "bin" in candidates


def test_pg_bin_candidates_include_environment_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SYNTH_PG_BIN", "/opt/pg/bin")
    candidates = bootstrap.pg_bin_candidates()
    assert Path("/opt/pg/bin") in candidates


def test_version_key_orders_16_above_9_6() -> None:
    assert bootstrap._version_key("16") > bootstrap._version_key("9.6")
    assert bootstrap._version_key("not-a-version") == (0,)


# ---------------------------------------------------------------------------
# the generated .env is technical-only
# ---------------------------------------------------------------------------

#: Keys that must never be written by the bootstrap: they are persona, location,
#: timezone or engine credentials and belong to the WebUI setup page.
PERSONAL_KEYS = {
    "SYNTH_NAME",
    "SYNTH_PROFILE",
    "SYNTH_ALIASES",
    "TRAINER_NAME",
    "TZ",
    "PROJECT_DEFAULT_LANGUAGE",
    "BASE_CORTEX",
    "ACTIVE_VOX_ENGINE",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
}


def _env_values() -> dict[str, str]:
    target = bootstrap.PostgresTarget(
        host="127.0.0.1",
        port=5433,
        superuser="postgres",
        superuser_password="secret",
        psql="/usr/bin/psql",
    )
    return bootstrap.build_env_values(
        target=target,
        db_name="synth",
        db_user="synth",
        db_password="generated",
        webui_port=8080,
        api_port=11435,
    )


def test_generated_env_carries_the_database_settings() -> None:
    values = _env_values()
    assert values["DB_HOST"] == "127.0.0.1"
    assert values["DB_PORT"] == "5433"
    assert values["DB_NAME"] == "synth"
    assert values["DB_USER"] == "synth"
    assert values["SOUL_REPOSITORY_BACKEND"] == "postgres"
    assert (
        values["SOUL_POSTGRES_DSN"]
        == "postgresql://synth:generated@127.0.0.1:5433/synth"
    )


def test_generated_env_binds_loopback_and_disables_tls() -> None:
    values = _env_values()
    assert values["SYNTH_WEBUI_HOST"] == "127.0.0.1"
    assert values["OLLAMA_HOST"] == "127.0.0.1"
    assert values["SYNTH_WEBUI_TLS"] == "0"
    assert values["SYNTH_IN_CONTAINER"] == "0"


def test_generated_env_never_contains_personal_or_engine_keys() -> None:
    values = _env_values()
    leaked = PERSONAL_KEYS & set(values)
    assert not leaked, f"the bootstrap must not decide these: {sorted(leaked)}"


def test_no_mariadb_defaults_survive_in_the_generated_env() -> None:
    values = _env_values()
    assert values["DB_PORT"] != "3306"
    assert "DB_ROOT_PASS" not in values
    assert values["SYNTH_DB_TYPE"] == "postgres"


def test_env_keeps_soul_in_memory_without_pgvector() -> None:
    """No pgvector -> no VECTOR(768) column -> SOUL must not use Postgres."""
    target = bootstrap.PostgresTarget(
        host="127.0.0.1",
        port=5432,
        superuser="postgres",
        superuser_password=None,
        psql="psql",
    )
    values = bootstrap.build_env_values(
        target=target,
        db_name="synth",
        db_user="synth",
        db_password="pw",
        webui_port=8080,
        api_port=11435,
        soul_backend="memory",
    )
    assert values["SOUL_REPOSITORY_BACKEND"] == "memory"


def test_ensure_extensions_reports_what_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing vector extension is reported, never fatal."""

    def fake_psql(target: object, sql: str, *, database: str = "postgres") -> object:
        if "vector" in sql:
            return bootstrap.CommandResult(1, "", 'extension "vector" is not available')
        return bootstrap.CommandResult(0, "", "")

    monkeypatch.setattr(bootstrap, "psql_query", fake_psql)
    reporter = bootstrap.Reporter(3, quiet=True)
    target = bootstrap.PostgresTarget(
        host="127.0.0.1",
        port=5432,
        superuser="postgres",
        superuser_password=None,
        psql="psql",
    )
    available = bootstrap.ensure_extensions(
        reporter, target, db_name="synth", db_user="synth"
    )
    assert available == {"vector": False, "pg_trgm": True}
    assert any("vector" in warning for warning in reporter.warnings)


def test_log_file_receives_every_line_and_never_breaks_the_run(
    tmp_path: Path,
) -> None:
    """The installer runs bootstrap hidden; the log is the only trace of it."""
    log = tmp_path / "bootstrap.log"
    reporter = bootstrap.Reporter(2, quiet=True, log_file=str(log))
    reporter.step("Locating a database engine")
    reporter.ok("found one")
    reporter.warn("something worth knowing")

    text = log.read_text(encoding="utf-8")
    assert "Locating a database engine" in text
    assert "ok: found one" in text
    assert "warning: something worth knowing" in text, "warnings must be logged"

    # A log that cannot be written must never be the reason an install fails.
    broken = bootstrap.Reporter(
        1, quiet=True, log_file=str(tmp_path / "missing-dir" / "x.log")
    )
    broken.step("still runs")
    assert broken.messages


def test_without_a_log_file_nothing_is_written(tmp_path: Path) -> None:
    reporter = bootstrap.Reporter(1, quiet=True)
    reporter.step("no log configured")
    assert list(tmp_path.iterdir()) == []


def test_main_records_the_invocation_in_the_log(tmp_path: Path) -> None:
    """A log with no header cannot be told from the previous attempt's."""
    log = tmp_path / "run.log"
    exit_code = bootstrap.main(
        [
            "--dry-run",
            "--skip-sync",
            "--no-browser",
            "--log-file",
            str(log),
            "--env-file",
            str(tmp_path / "test.env"),
        ]
    )
    assert exit_code == 0
    text = log.read_text(encoding="utf-8")
    assert "bootstrap.py" in text, "the log must say what was run"
    assert "--log-file" in text, "the log must record the arguments"
    assert "===" in text, "entries must be separated so repeats are distinguishable"


def _stub_portable_cluster(monkeypatch: pytest.MonkeyPatch, cluster: Path) -> None:
    """Point the portable-cluster helpers at *cluster* and at fake binaries."""
    monkeypatch.setattr(bootstrap, "_cluster_dir", lambda: cluster)
    monkeypatch.setattr(bootstrap, "_read_superuser_password", lambda: None)
    monkeypatch.setattr(bootstrap, "find_pg_tool", lambda *a, **k: "fake-pg-tool")
    monkeypatch.setattr(bootstrap, "generate_password", lambda length: "x" * length)


def test_a_half_created_cluster_is_removed_before_retrying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """initdb refuses a non-empty directory, so a stopped attempt must not block.

    A previous run that hit the timeout (or was closed) leaves a partial cluster
    behind. Without this cleanup the retry fails with "directory exists but is
    not empty", which says nothing about what actually happened.
    """
    cluster = tmp_path / "pgsql"
    cluster.mkdir(parents=True)
    (cluster / "leftover-from-a-killed-initdb").write_text("x", encoding="utf-8")
    _stub_portable_cluster(monkeypatch, cluster)
    monkeypatch.setattr(
        bootstrap,
        "run_command",
        lambda *a, **k: bootstrap.CommandResult(1, "", "initdb says no"),
    )

    reporter = bootstrap.Reporter(1, quiet=True)
    assert bootstrap.ensure_portable_cluster(reporter, pg_bin=None, port=5433) is None
    assert not cluster.exists(), "the half-built cluster must be removed for the retry"
    assert any("half-created" in warning for warning in reporter.warnings)


def test_the_cluster_step_reports_where_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The log must not be silent across initdb; silence is a hang to a user."""
    _stub_portable_cluster(monkeypatch, tmp_path / "pgsql")
    monkeypatch.setattr(
        bootstrap, "run_command", lambda *a, **k: bootstrap.CommandResult(0, "", "")
    )
    monkeypatch.setattr(
        bootstrap, "psql_query", lambda *a, **k: bootstrap.CommandResult(0, "1", "")
    )

    reporter = bootstrap.Reporter(1, quiet=True)
    bootstrap.ensure_portable_cluster(reporter, pg_bin=None, port=5433)
    text = "\n".join(reporter.messages)
    assert "running initdb" in text, "the slow step must announce itself"
    assert "starting the server" in text
    assert "cluster created in" in text
    assert "private cluster is up" in text


def test_initdb_timing_out_names_the_likely_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexplained timeout sends the user to the wrong place."""
    _stub_portable_cluster(monkeypatch, tmp_path / "pgsql")
    monkeypatch.setattr(
        bootstrap,
        "run_command",
        lambda *a, **k: bootstrap.CommandResult(124, "", "timed out"),
    )

    reporter = bootstrap.Reporter(1, quiet=True)
    assert bootstrap.ensure_portable_cluster(reporter, pg_bin=None, port=5433) is None
    text = "\n".join(reporter.messages)
    assert str(bootstrap.INITDB_TIMEOUT_SEC) in text
    assert "antivirus" in text.lower(), "the usual cause must be named"
    assert "again" in text.lower(), "the user must know a retry is safe"


def test_ensure_extensions_reports_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        bootstrap, "psql_query", lambda *a, **k: bootstrap.CommandResult(0, "", "")
    )
    reporter = bootstrap.Reporter(3, quiet=True)
    target = bootstrap.PostgresTarget(
        host="127.0.0.1",
        port=5432,
        superuser="postgres",
        superuser_password=None,
        psql="psql",
    )
    assert bootstrap.ensure_extensions(
        reporter, target, db_name="synth", db_user="synth"
    ) == {"vector": True, "pg_trgm": True}


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_cli_defaults_are_native_friendly() -> None:
    args = bootstrap._parse_args([])
    assert args.portable is False
    assert args.skip_sync is False
    assert args.json is False
    assert args.extra == []


def test_cli_accepts_repeated_extras_and_flags() -> None:
    args = bootstrap._parse_args(
        [
            "--portable",
            "--extra",
            "local-voice",
            "--extra",
            "whisper",
            "--json",
            "--dry-run",
        ]
    )
    assert args.portable is True
    assert args.extra == ["local-voice", "whisper"]
    assert args.json is True
    assert args.dry_run is True


def test_reporter_collects_warnings_without_printing_them_as_errors(
    capsys: object,
) -> None:
    reporter = bootstrap.Reporter(2, quiet=True)
    reporter.warn("something to mention")
    assert reporter.warnings == ["something to mention"]


# ---------------------------------------------------------------------------
# PostgreSQL superuser transports
# ---------------------------------------------------------------------------


def _capture_command(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def fake_run(command: list[str], **kwargs: object) -> object:
        calls.append({"command": command, "kwargs": kwargs})
        return bootstrap.CommandResult(0, "1", "")

    monkeypatch.setattr(bootstrap, "run_command", fake_run)
    return calls


def test_psql_query_uses_a_tcp_password_connection_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_command(monkeypatch)
    target = bootstrap.PostgresTarget(
        host="127.0.0.1",
        port=5433,
        superuser="postgres",
        superuser_password="pw",
        psql="/usr/bin/psql",
    )
    bootstrap.psql_query(target, "SELECT 1")
    command = calls[0]["command"]
    assert command[0] == "/usr/bin/psql"
    assert "-h" in command and "-U" in command
    assert calls[0]["kwargs"]["env"] == {"PGPASSWORD": "pw"}


def test_psql_query_uses_peer_auth_when_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_command(monkeypatch)
    target = bootstrap.PostgresTarget(
        host="127.0.0.1",
        port=5432,
        superuser="postgres",
        superuser_password=None,
        psql="/usr/bin/psql",
        via_sudo=True,
    )
    bootstrap.psql_query(target, "SELECT 1")
    command = calls[0]["command"]
    assert command[:4] == ["sudo", "-n", "-u", "postgres"]
    assert "-h" not in command
    assert "PGPASSWORD" not in (calls[0]["kwargs"].get("env") or {})


def test_cli_pg_via_sudo_is_tri_state() -> None:
    assert bootstrap._parse_args([]).pg_via_sudo is None
    assert bootstrap._parse_args(["--pg-via-sudo"]).pg_via_sudo is True
    assert bootstrap._parse_args(["--no-pg-via-sudo"]).pg_via_sudo is False


def test_sudo_psql_is_never_attempted_on_windows() -> None:
    import os

    if os.name == "nt":
        assert bootstrap.sudo_psql_available("/usr/bin/psql") is False
