"""Guards on what the Windows installer is allowed to ship.

The exclude list in ``installer/synth-installer.iss`` is a hand-written string,
and getting it wrong is not a cosmetic bug: the repository root holds a real
``.env`` (gitignored, so it exists only on a developer's machine), ``data/``
holds the encrypted-endpoint secret, and the WebUI's TTS cache holds the
avatar's actual speech. All three were shipped by a rewrite of that list, which
is why these assertions exist.

These tests read the .iss as text. They do not compile it; the release workflow
does that, and the compiler's log is the authoritative payload record.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ISS = REPO_ROOT / "installer" / "synth-installer.iss"


@pytest.fixture(scope="module")
def iss_text() -> str:
    return ISS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def exclude_patterns(iss_text: str) -> set[str]:
    """The Excludes list of the application-tree entry, split into patterns."""
    match = re.search(r'Source: "\.\.\\\*";[^\n]*Excludes: "([^"]+)"', iss_text)
    assert match, "the application-tree [Files] entry is missing or has no Excludes"
    raw = match.group(1)
    # {#ExampleSkinsExclude} is an ISPP define; treat it as a wildcard fragment
    # so the split below stays meaningful.
    raw = re.sub(r"\{#[A-Za-z]+\}", "skins\\__define__\\*,", raw)
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


@pytest.mark.parametrize(
    "pattern",
    [
        ".env",
        ".venv*",
        ".git\\*",
        "data\\*",
        "logs\\*",
        "node_modules\\*",
        "mcp_servers\\*",
        "installer\\output\\*",
    ],
)
def test_private_and_generated_paths_are_excluded(
    exclude_patterns: set[str], pattern: str
) -> None:
    assert pattern.lower() in exclude_patterns, (
        f"{pattern} is not excluded: it is local state or generated content and "
        "must never be installed on someone else's machine"
    )


def test_the_env_template_still_ships(exclude_patterns: set[str]) -> None:
    """Excluding .env must not exclude .env.example, which users need."""
    assert ".env.example" not in exclude_patterns
    # A bare ".env*" would swallow the template as well.
    assert ".env*" not in exclude_patterns


def test_the_tts_cache_is_not_shipped(exclude_patterns: set[str]) -> None:
    """The WebUI's generated speech is private and was ~35 MB of the payload."""
    assert any("audio\\tts" in pattern for pattern in exclude_patterns)


def test_untracked_working_notes_are_not_shipped(exclude_patterns: set[str]) -> None:
    assert "one-click.md" in exclude_patterns
    assert "venice_no_response_report.md" in exclude_patterns


def test_the_installer_is_user_scoped(iss_text: str) -> None:
    """No admin, no UAC: that was the main complaint about the old installer."""
    assert "PrivilegesRequired=lowest" in iss_text
    assert "PrivilegesRequired=admin" not in iss_text


def test_there_is_exactly_one_choice_to_make(iss_text: str) -> None:
    """One option, no component picker: the whole point of the rewrite."""
    assert "[Components]" not in iss_text
    assert "[Types]" not in iss_text
    assert "DisableDirPage=yes" in iss_text


def test_nothing_opens_a_console_window(iss_text: str) -> None:
    """Every helper runs through pythonw.exe or hidden PowerShell."""
    assert "start_synth.bat" not in iss_text
    assert "pythonw.exe" in iss_text
    run_entries = re.findall(r"^Filename:.*$", iss_text, flags=re.MULTILINE)
    assert run_entries, "expected [Run]/[UninstallRun] entries"
    for entry in run_entries:
        assert ".bat" not in entry and ".cmd" not in entry, entry


def test_the_version_comes_from_the_build_not_a_file(iss_text: str) -> None:
    """GitVersion is the source of truth; there is no version file to read."""
    assert "version.txt" not in iss_text
    assert "#ifndef AppVersion" in iss_text


def test_every_file_the_installer_references_exists() -> None:
    """A missing icon or template would only fail at compile time on a release."""
    for relative in (
        "installer/synth.ico",
        "installer/wizard-large.bmp",
        "installer/wizard-small.bmp",
        "installer/build_installer.ps1",
        "installer/vendor/README.md",
        "core/webui_templates/setup.html",
        "scripts/start_synth.py",
        "scripts/bootstrap.py",
        "scripts/install_prereqs.ps1",
    ):
        assert (REPO_ROOT / relative).is_file(), f"{relative} is referenced but missing"


def test_pgvector_is_vendored_by_the_release_build(iss_text: str) -> None:
    """Without it the install still works, but SOUL memory search is memory-only."""
    assert "vendor\\pgvector" in iss_text
    assert "skipifsourcedoesntexist" in iss_text


def test_the_uninstaller_stops_the_database_first(iss_text: str) -> None:
    """Windows will not delete a directory a running process holds open."""
    assert "--stop-cluster" in iss_text
    assert "start_synth.py" in iss_text and "--stop" in iss_text
