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


#: Inno Setup's own constants, plus the ones this installer is allowed to use.
#: An environment variable (``{%NAME}``) is always acceptable and is handled
#: separately, because it is a different mechanism with different syntax.
VALID_INNO_CONSTANTS = frozenset(
    {
        "app",
        "tmp",
        "sys",
        "sysnative",
        "win",
        "src",
        "sd",
        "localappdata",
        "userappdata",
        "userdocs",
        "userdesktop",
        "userprograms",
        "userstartmenu",
        "usercf",
        "userpf",
        "commonappdata",
        "commonprograms",
        "commondesktop",
        "commonpf",
        "commoncf",
        "autodesktop",
        "autoprograms",
        "group",
        "uninstallexe",
        "fonts",
        "dao",
    }
)


def test_only_real_inno_constants_are_used(iss_text: str) -> None:
    """``{userprofile}`` is not a constant, and only fails at runtime.

    ``ExpandConstant('{userprofile}')`` aborts the install with "unknown
    constant" *after* the files are copied and the prerequisites installed. The
    compile is perfectly clean, so nothing catches it before a user does. Any
    braced name that is not a documented constant must be an environment
    variable (``{%NAME}``), which this regex deliberately does not match.
    """
    used = set(re.findall(r"\{([a-z][a-z0-9_]*)\}", iss_text))
    unknown = sorted(name for name in used if name not in VALID_INNO_CONSTANTS)
    assert not unknown, (
        f"not Inno Setup constants: {unknown} - the user profile is "
        "{%USERPROFILE} and the temp folder is {%TEMP} or {tmp}"
    )


def test_no_inno_comment_swallows_itself(iss_text: str) -> None:
    """Inno's ``{ }`` comments do not nest, so an inner ``{`` ends one early.

    The rest of the line is then parsed as Pascal and the compiler reports a
    column number pointing at prose, which is a slow way to learn this. Writing
    the temp-folder constant inside a comment that explains the temp-folder
    constant is the natural thing to do, so it is worth a guard.
    """
    offenders = [
        (number, line.strip())
        for number, line in enumerate(iss_text.splitlines(), start=1)
        if line.lstrip().startswith("{") and line.count("{") > 1
    ]
    assert not offenders, f"a brace inside an Inno comment ends it early: {offenders}"


def test_the_finish_page_opens_the_setup_page(iss_text: str) -> None:
    """The [Run] entry's label promises the setup page, so it must ask for it.

    Without `--setup` the launcher opens the plain WebUI, and a new user meets an
    avatar scene with nothing telling them what to do next.
    """
    run_section = iss_text.split("[Run]", 1)[1].split("[UninstallRun]", 1)[0]
    assert "--setup" in run_section


def test_both_provisioning_steps_leave_a_log(iss_text: str) -> None:
    """Both steps run with their window hidden, so each must write a log.

    Otherwise a failure reaches the user as a bare exit code, which is exactly
    what the installer's own error dialog has to explain.
    """
    assert "synth_prereqs.log" in iss_text
    assert "synth_bootstrap.log" in iss_text
    assert "--log-file" in iss_text, "bootstrap is invoked without a log file"
    assert "{%TEMP}" in iss_text, "the reported log path must be the real TEMP folder"


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
