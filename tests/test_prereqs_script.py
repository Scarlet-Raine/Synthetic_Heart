"""Invariants for the Windows prerequisite script.

These are text-level checks, not a substitute for running the script on Windows
(which the release workflow does). They exist because the script's failure modes
are about *what it reports*, and a silent failure is the one thing a hidden
installer cannot afford: a real run died with exit code 1 and a log that stopped
mid-sentence, and there was nothing on disk to say why.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PREREQS = REPO_ROOT / "scripts" / "install_prereqs.ps1"


def script_text() -> str:
    # utf-8-sig: the file carries a BOM, as PowerShell scripts written on Windows do.
    return PREREQS.read_text(encoding="utf-8-sig")


def test_unexpected_failures_are_logged_rather_than_swallowed() -> None:
    """`$ErrorActionPreference = 'Stop'` plus no trap is a silent exit code 1.

    That is exactly what a live run produced: the installer reported exit code 1
    and the log ended at the last progress line, because an exception terminated
    the script before any handler could record it.
    """
    text = script_text()
    assert "$ErrorActionPreference = 'Stop'" in text, "the premise of this test changed"

    trap = re.search(r"^trap \{.*?^\}", text, re.DOTALL | re.MULTILINE)
    assert trap, "an unhandled exception must not be allowed to end the script silently"
    assert "Write-Log" in trap.group(0), "the trap must record the reason in the log"
    assert "exit 1" in trap.group(0)


def test_the_cached_archive_is_checked_before_it_is_unpacked() -> None:
    """A download killed part-way leaves a truncated zip that only fails later."""
    text = script_text()
    check = text.index("Test-UsableArchive $zipPath")
    unpack = text.index("Expand-Zip $zipPath $staging")
    assert check < unpack, "the cached archive must be validated before unpacking it"
    assert "Remove-Item $zipPath" in text, (
        "an unusable archive must be discarded, not reused"
    )


def test_the_slow_steps_say_what_they_are_doing() -> None:
    """Unpacking a 320 MB archive is slow, and silence reads as a hang."""
    text = script_text()
    assert "unpacking PostgreSQL into a temporary folder" in text
    assert "unpacked in" in text
    assert "installing into" in text
    assert "installed in" in text


def test_the_archive_extractor_is_not_expand_archive() -> None:
    """PS 5.1's Expand-Archive pushes every entry through PowerShell objects.

    On a 300 MB archive that is minutes of work and a lot of memory; the .NET
    extractor streams to disk and reports a real error when the file is bad.
    """
    text = script_text()
    assert "Expand-Archive -" not in text, "use Expand-Zip (the .NET extractor) instead"
    assert "System.IO.Compression.ZipFile" in text


def test_the_log_path_is_one_the_installer_can_name() -> None:
    """The installer prints the log path in its error dialog, so it must be TEMP."""
    text = script_text()
    assert "Join-Path $env:TEMP 'synth_prereqs.log'" in text
