"""The tray icon: it has to run, and it has to be able to say why it did not.

Two failures cost a round trip to a real machine, and both were silence:

* ``DETACHED_PROCESS`` was used to start PowerShell. A console program given no
  console at all starts, exits with 0 and does nothing, so the icon never appeared
  and nothing anywhere recorded it.
* the script crashed a second later on ``$matches`` being overwritten by a later
  ``-notmatch`` — an error that would have been invisible without its log.

These tests pin the launch flags, the fact that the script's output is kept, and that
the script really does reach its message loop when run the way the launcher runs it.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPO_ROOT / "scripts" / "start_synth.py"
TRAY_SCRIPT = REPO_ROOT / "scripts" / "synth_tray.ps1"
POWERSHELL = shutil.which("powershell") or shutil.which("powershell.exe")

IS_WINDOWS = os.name == "nt"


def _load_launcher():
    spec = importlib.util.spec_from_file_location("start_synth_tray_test", LAUNCHER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["start_synth_tray_test"] = module
    spec.loader.exec_module(module)
    return module


def _spawn_tray_source() -> str:
    """The body of spawn_tray, which is where the launch flags live."""
    source = LAUNCHER.read_text(encoding="utf-8")
    start = source.index("def spawn_tray(")
    end = source.index("def wants_tray(", start)
    return source[start:end]


def test_the_tray_is_not_started_with_detached_process() -> None:
    """DETACHED_PROCESS makes a console program a no-op, which is how the icon vanished.

    Measured on Windows: ``powershell -Command '... | Set-Content marker'`` writes the
    marker with no flags and with CREATE_NO_WINDOW, and writes nothing at all with
    DETACHED_PROCESS. The tray is a PowerShell script, so using it there means the icon
    silently never appears.
    """
    body = _spawn_tray_source()
    # Comments are excluded on purpose: the code explains why it does not use it.
    code = "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("#")
    )
    assert "DETACHED_PROCESS" not in code, (
        "spawn_tray must not start PowerShell detached: it exits without running"
    )
    assert "CREATE_NO_WINDOW" in code


def test_the_trays_output_is_kept_rather_than_discarded() -> None:
    """When an icon does not appear, its own output is the only witness.

    It was sent to DEVNULL once, and a silent tray then needed a trip to the user's
    machine to explain. Both the launcher's decision log and the script's output have
    to land somewhere readable.
    """
    body = _spawn_tray_source()
    assert "tray.out.log" in body
    assert "_launch_log(" in body
    # stdin may be closed, but stdout and stderr must not go to nowhere.
    assert 'stdout": subprocess.DEVNULL' not in body
    assert 'stderr": subprocess.DEVNULL' not in body


def test_the_tray_is_given_a_log_file() -> None:
    """The script is told where to write, so its log sits with the application's."""
    launcher_source = LAUNCHER.read_text(encoding="utf-8")
    assert '"-LogFile"' in launcher_source
    assert '"tray.log"' in launcher_source
    tray_source = TRAY_SCRIPT.read_text(encoding="utf-8")
    assert "Add-Content -LiteralPath $LogFile" in tray_source
    # The whole point: an error inside the script is recorded, not swallowed.
    assert "FATAL" in tray_source


def test_a_refused_tray_records_its_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No icon is a decision with a reason, and the reason is written down.

    Here the reason is a missing script: the root is pointed at an empty directory, so
    the launcher has to decline. Whatever the reason is, it has to reach the log.
    """
    module = _load_launcher()
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    assert module.tray_command(tmp_path / ".env") is None
    launch_log = tmp_path / "logs" / "synth_launch.log"
    assert launch_log.is_file(), "a refused tray left no trace"
    text = launch_log.read_text(encoding="utf-8")
    assert "tray:" in text
    if IS_WINDOWS:
        assert "is missing" in text


@pytest.mark.skipif(
    not (IS_WINDOWS and POWERSHELL),
    reason="needs a Windows desktop with PowerShell",
)
def test_the_tray_script_is_syntactically_valid() -> None:
    """A parse error in the tray is an icon that never appears, with no other symptom."""
    if not TRAY_SCRIPT.is_file() or not POWERSHELL:
        pytest.skip("no tray script or no PowerShell in this checkout")
    parser = (
        "$errors = $null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{TRAY_SCRIPT}', [ref]$null, [ref]$errors) | Out-Null; "
        "if ($errors) { $errors | ForEach-Object { $_.Message }; exit 1 } else { 'ok' }"
    )
    completed = subprocess.run(  # noqa: S603
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", parser],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.skipif(
    not (IS_WINDOWS and POWERSHELL),
    reason="needs a Windows desktop with PowerShell",
)
def test_every_helper_the_tray_calls_is_defined_in_it() -> None:
    """A missing helper is a menu entry that fails only when someone clicks it.

    The Shut down and Restart entries called ``Update-State`` for a while without it
    existing anywhere: PowerShell reports that at click time, in a window that is not
    open, which is exactly the kind of silence this file exists to prevent. PowerShell
    itself is the judge of which commands are its own.
    """
    if not TRAY_SCRIPT.is_file() or not POWERSHELL:
        pytest.skip("no tray script or no PowerShell in this checkout")
    body = TRAY_SCRIPT.read_text(encoding="utf-8")
    # Comments are prose, and prose reads like a command name: the help block's
    # "Notification-area (tray) icon" is not a call to anything.
    code = re.sub(r"<#.*?#>", "", body, flags=re.DOTALL)
    code = "\n".join(
        line.split(" #", 1)[0]
        for line in code.splitlines()
        if not line.lstrip().startswith("#")
    )
    defined = set(re.findall(r"function\s+([A-Za-z][\w-]*)", code))
    # Command position only: a statement start, straight after an opening brace, or a
    # pipeline stage.
    candidates = set(
        re.findall(r"(?:^\s*|\{\s*)([A-Z][A-Za-z]*-[A-Za-z][\w]*)", code, re.MULTILINE)
    ) | set(re.findall(r"\|\s*([A-Z][A-Za-z]*-[A-Za-z][\w]*)", code))
    unknown = sorted(candidates - defined)
    if not unknown:
        return
    probe = "; ".join(
        f"if (Get-Command -Name '{name}' -ErrorAction SilentlyContinue) {{ '{name}' }}"
        for name in unknown
    )
    known = subprocess.run(  # noqa: S603
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", probe],
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout
    builtin = {name for name in unknown if name in known}
    missing = sorted(set(unknown) - builtin)
    assert not missing, (
        f"the tray calls {missing}, which it does not define and PowerShell does not "
        "provide"
    )


@pytest.mark.skipif(
    not (IS_WINDOWS and POWERSHELL),
    reason="needs a Windows desktop with PowerShell",
)
def test_the_tray_script_reaches_its_message_loop(tmp_path: Path) -> None:
    """Run the real script the way scripts/start_synth.py runs it.

    This is the test that would have caught both silent failures: a crash shows up as
    FATAL in the log, and a process that never ran shows up as no log at all.
    """
    if not TRAY_SCRIPT.is_file():
        pytest.skip("synth_tray.ps1 is not in this checkout")
    icon = REPO_ROOT / "installer" / "synth-tray.ico"
    if not icon.is_file():
        pytest.skip("the tray icon is not in this checkout")

    # Its own AppRoot, so this cannot collide with an icon that is really running.
    (tmp_path / "installer").mkdir(exist_ok=True)
    shutil.copy(icon, tmp_path / "installer" / "synth-tray.ico")
    log = tmp_path / "logs" / "tray.log"

    command = [
        POWERSHELL,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(TRAY_SCRIPT),
        "-AppRoot",
        str(tmp_path),
        "-EnvFile",
        str(tmp_path / "missing.env"),
        "-LogFile",
        str(log),
        # No balloon: this runs during the test suite, on the tester's desktop.
        "-NoBalloon",
    ]
    process = subprocess.Popen(  # noqa: S603
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        text = ""
        deadline = time.time() + 60.0
        while time.time() < deadline:
            if log.is_file():
                text = log.read_text(encoding="utf-8", errors="replace")
                if "entering the message loop" in text or "FATAL" in text:
                    break
            time.sleep(0.25)

        assert "FATAL" not in text, f"the tray failed:\n{text}"
        assert "notification icon created and made visible" in text, (
            f"the tray never made its icon visible:\n{text}"
        )
        assert "entering the message loop" in text, (
            f"the tray never reached its message loop:\n{text}"
        )
        assert process.poll() is None, "the tray exited instead of staying up"
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=15)
