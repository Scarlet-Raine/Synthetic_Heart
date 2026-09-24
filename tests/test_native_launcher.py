"""Tests for the native health probe and launcher helpers.

``scripts/healthcheck.py`` and ``scripts/start_synth.py`` are what the installers'
shortcuts call, so their decision logic is pinned here: URL derivation, scheme
fallback, pid bookkeeping and interpreter selection. No process is started.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, relative: str) -> object:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


healthcheck = _load("_synth_healthcheck_under_test", "scripts/healthcheck.py")
start_synth = _load("_synth_start_under_test", "scripts/start_synth.py")


# ---------------------------------------------------------------------------
# healthcheck: configuration parsing
# ---------------------------------------------------------------------------


def test_parse_env_file_reads_values_and_skips_comments(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\nDB_PORT=5432\nSYNTH_WEBUI_TLS='1'\nEMPTY=\n", encoding="utf-8"
    )
    values = healthcheck.parse_env_file(env)
    assert values == {"DB_PORT": "5432", "SYNTH_WEBUI_TLS": "1", "EMPTY": ""}


def test_parse_env_file_on_a_missing_file_is_empty(tmp_path: Path) -> None:
    assert healthcheck.parse_env_file(tmp_path / "absent") == {}


def test_effective_env_lets_the_real_environment_win(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text("DB_PORT=5432\n", encoding="utf-8")
    monkeypatch.setenv("DB_PORT", "6543")
    assert healthcheck.effective_env(env)["DB_PORT"] == "6543"


# ---------------------------------------------------------------------------
# healthcheck: WebUI URL derivation
# ---------------------------------------------------------------------------


def test_webui_url_uses_plain_http_for_a_loopback_native_install() -> None:
    assert (
        healthcheck._webui_url(
            {"SYNTH_WEBUI_TLS": "0", "SYNTH_WEBUI_HTTP_PORT": "8080"}
        )
        == "http://127.0.0.1:8080/"
    )


def test_webui_url_uses_https_when_configured() -> None:
    values = {"SYNTH_WEBUI_TLS": "1", "SYNTH_WEBUI_HTTPS_PORT": "8443"}
    assert healthcheck._webui_url(values) == "https://127.0.0.1:8443/"


def test_webui_url_falls_back_to_the_http_port_for_tls() -> None:
    """With TLS on and no HTTPS port, the app serves TLS on the HTTP port."""
    values = {"SYNTH_WEBUI_TLS": "1", "SYNTH_WEBUI_HTTP_PORT": "8080"}
    assert healthcheck._webui_url(values) == "https://127.0.0.1:8080/"


def test_webui_url_maps_the_all_interfaces_bind_to_loopback() -> None:
    values = {"SYNTH_WEBUI_TLS": "0", "SYNTH_WEBUI_HOST": "0.0.0.0"}
    assert healthcheck._webui_url(values) == "http://127.0.0.1:8080/"


def test_webui_candidates_probe_both_schemes() -> None:
    candidates = healthcheck._webui_candidates({"SYNTH_WEBUI_TLS": "1"})
    assert candidates[0][0].startswith("https://")
    assert candidates[0][1] is True
    assert any(url.startswith("http://") for url, _ in candidates)
    assert not any(is_configured for url, is_configured in candidates[1:])


def test_probe_webui_reports_a_scheme_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server up on the wrong scheme is up, with a note — not 'down'."""
    monkeypatch.setattr(
        healthcheck,
        "http_reachable",
        lambda url, timeout=5.0: (url.startswith("http://"), "200"),
    )
    result = healthcheck._probe_webui(
        {"SYNTH_WEBUI_TLS": "1", "SYNTH_WEBUI_HTTP_PORT": "8080"}
    )
    assert result["ok"] is True
    assert result["scheme_mismatch"] is True
    assert str(result["url"]).startswith("http://")


def test_probe_webui_reports_down_when_nothing_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        healthcheck, "http_reachable", lambda url, timeout=5.0: (False, "no")
    )
    result = healthcheck._probe_webui({"SYNTH_WEBUI_TLS": "0"})
    assert result["ok"] is False
    assert result["scheme_mismatch"] is False


# ---------------------------------------------------------------------------
# start_synth: launcher internals
# ---------------------------------------------------------------------------


def test_build_command_runs_main_py_with_the_given_interpreter() -> None:
    command = start_synth.build_command(Path("/python"), console=False)
    assert command[0] == str(Path("/python"))
    assert command[1].endswith("main.py")


def test_pid_file_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(start_synth, "pid_path", lambda: tmp_path / "synth.pid")
    assert start_synth.read_pid() is None
    start_synth.write_pid(4321)
    assert start_synth.read_pid() == 4321
    start_synth.clear_pid()
    assert start_synth.read_pid() is None


def test_pid_alive_for_this_process() -> None:
    assert start_synth.pid_alive(os.getpid()) is True


def test_pid_alive_for_an_impossible_pid() -> None:
    assert start_synth.pid_alive(999_999_999) is False


def test_stop_is_a_no_op_without_a_pid_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The shortcut must never kill something it did not start."""
    monkeypatch.setattr(start_synth, "pid_path", lambda: tmp_path / "synth.pid")
    called: list[tuple[int, int]] = []
    monkeypatch.setattr(start_synth, "read_pid", lambda: None)
    monkeypatch.setattr(
        start_synth.os, "kill", lambda pid, sig: called.append((pid, sig))
    )
    assert start_synth.stop() == 0
    assert called == []
    assert "not running" in capsys.readouterr().out


def test_venv_python_prefers_the_windowed_interpreter_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(start_synth, "REPO_ROOT", tmp_path)
    scripts_dir = tmp_path / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    scripts_dir.mkdir(parents=True)
    plain = scripts_dir / ("python.exe" if os.name == "nt" else "python")
    plain.write_text("", encoding="utf-8")
    assert start_synth.venv_python(windowed=False) == plain
    if os.name == "nt":
        windowed = scripts_dir / "pythonw.exe"
        windowed.write_text("", encoding="utf-8")
        assert start_synth.venv_python(windowed=True) == windowed
    else:
        assert start_synth.venv_python(windowed=True) == plain


def test_venv_python_is_none_without_an_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(start_synth, "REPO_ROOT", tmp_path)
    assert start_synth.venv_python(windowed=False) is None
    assert start_synth.venv_python(windowed=True) is None


def test_venv_python_never_consults_an_absolute_posix_path_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard the drive-relative trap: no ``/app``-style assumption in the launcher."""
    source = (REPO_ROOT / "scripts" / "start_synth.py").read_text(encoding="utf-8")
    assert '"/app' not in source
    assert "'/app" not in source


# ---------------------------------------------------------------------------
# The tray icon: a windowless launch has to look like something happened
# ---------------------------------------------------------------------------


def _args(**overrides: object) -> object:
    base = {"tray": False, "no_tray": False, "foreground": False}
    base.update(overrides)
    return argparse.Namespace(**base)


def test_the_tray_is_shown_by_default_on_a_windows_desktop_launch() -> None:
    """The installer's shortcut and its finish action both take this path."""
    expected = os.name == "nt"
    assert start_synth.wants_tray(_args()) is expected


def test_a_foreground_run_uses_its_console_instead_of_the_tray() -> None:
    assert start_synth.wants_tray(_args(foreground=True)) is False


def test_no_tray_and_tray_win_over_the_default() -> None:
    assert start_synth.wants_tray(_args(no_tray=True, tray=True)) is False
    assert start_synth.wants_tray(_args(tray=True)) is True


def test_the_tray_command_points_at_the_script_and_the_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(start_synth.os, "name", "nt")
    monkeypatch.setattr(start_synth, "REPO_ROOT", tmp_path)
    script = tmp_path / "scripts" / "synth_tray.ps1"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        start_synth.shutil, "which", lambda name: "C:/ps/powershell.exe"
    )

    command = start_synth.tray_command(tmp_path / ".env")

    assert command is not None
    assert command[0] == "C:/ps/powershell.exe"
    assert str(script) in command
    # The tray reads the same .env, so it probes the port and scheme the app was
    # actually configured with rather than a hard-coded 8080.
    assert str(tmp_path / ".env") in command
    assert "-WindowStyle" in command and "Hidden" in command


def test_there_is_no_tray_outside_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(start_synth.os, "name", "posix")
    assert start_synth.tray_command(tmp_path / ".env") is None
    assert start_synth.spawn_tray(tmp_path / ".env") is False


def test_a_missing_tray_script_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source tree without the script still starts SyntH."""
    monkeypatch.setattr(start_synth.os, "name", "nt")
    monkeypatch.setattr(start_synth, "REPO_ROOT", tmp_path)
    assert start_synth.tray_command(tmp_path / ".env") is None


def test_spawn_tray_launches_it_detached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(start_synth.os, "name", "nt")
    monkeypatch.setattr(
        start_synth, "tray_command", lambda env_file: ["powershell", "-File", "tray"]
    )
    launched: list[tuple[list[str], dict]] = []

    class _Process:
        pid = 4242

    def fake_popen(command, **kwargs):
        launched.append((list(command), kwargs))
        return _Process()

    monkeypatch.setattr(start_synth.subprocess, "Popen", fake_popen)

    assert start_synth.spawn_tray(tmp_path / ".env") is True
    assert launched and launched[0][0] == ["powershell", "-File", "tray"]
    # Detached: the launcher exits after opening the browser and must not take the
    # icon down with it.
    assert launched[0][1].get("creationflags")


def test_the_tray_script_offers_the_actions_the_icon_promises() -> None:
    """The menu is the feature: pin its entries against a rename or a rewrite."""
    script = REPO_ROOT / "scripts" / "synth_tray.ps1"
    assert script.is_file(), "the launcher references this path"
    text = script.read_text(encoding="utf-8")

    for label in ("Open SyntH", "Restart", "Shut down", "Check for updates"):
        assert label in text, f"missing tray menu entry: {label}"
    # Reads the install's own .env instead of assuming a port.
    assert "SYNTH_WEBUI_HTTP_PORT" in text
    # Single instance, so repeated launches cannot stack icons.
    assert "Mutex" in text
    # The transparent artwork, not the black-tiled squircle.
    assert "synth-tray.ico" in text
