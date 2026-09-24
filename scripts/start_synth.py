#!/usr/bin/env python3
"""Start SyntH the way a desktop user expects: one click, no console window.

This is what the installers' shortcuts and the Linux desktop entry invoke.  It:

* starts ``main.py`` in the background with the project virtual environment;
* on Windows uses ``pythonw.exe`` so no console window appears or lingers;
* redirects output into the normal log file;
* records a pid file so the same shortcut can stop it;
* shows a notification-area icon on Windows, so a windowless launch is visible;
* waits until the WebUI actually answers, then opens the browser on it.

Only the standard library is used, so it also works before ``uv sync``.

Usage::

    python scripts/start_synth.py              # start (or focus) and open the WebUI
    python scripts/start_synth.py --foreground # run in this terminal
    python scripts/start_synth.py --status     # is it up?
    python scripts/start_synth.py --stop       # stop the background instance
    python scripts/start_synth.py --console    # keep a console window (debugging)
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

#: Sibling script, stdlib-only. Imported by path rather than as a package
#: because ``scripts/`` is a folder of standalone tools, not an importable
#: package.
from healthcheck import check  # type: ignore[unresolved-import]  # noqa: E402


# ---------------------------------------------------------------------------
# Paths and interpreter selection
# ---------------------------------------------------------------------------


def _load_app_paths() -> object:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_synth_app_paths", REPO_ROOT / "core" / "app_paths.py"
    )
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError("cannot load core/app_paths.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_APP_PATHS = _load_app_paths()


def data_root() -> Path:
    return _APP_PATHS.data_root()  # type: ignore[attr-defined]


def log_dir() -> Path:
    return _APP_PATHS.log_dir()  # type: ignore[attr-defined]


def pid_path() -> Path:
    return data_root() / "synth.pid"


def venv_python(*, windowed: bool) -> Path | None:
    """Return the interpreter inside the project virtual environment.

    ``windowed=True`` prefers ``pythonw.exe``, which is what keeps a Windows
    desktop launch from flashing a console window.
    """
    candidates: list[Path] = []
    if os.name == "nt":
        base = REPO_ROOT / ".venv" / "Scripts"
        if windowed:
            candidates.append(base / "pythonw.exe")
        candidates.extend([base / "python.exe", base / "pythonw.exe"])
    else:
        candidates.append(REPO_ROOT / ".venv" / "bin" / "python")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# pid handling
# ---------------------------------------------------------------------------


def read_pid() -> int | None:
    path = pid_path()
    if not path.is_file():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def pid_alive(pid: int) -> bool:
    """Return True when a process with ``pid`` exists."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:
        # Windows raises a PermissionError-shaped error for a live system
        # process we do not own; treat anything non-OSError as "exists".
        return True
    return True


def write_pid(pid: int) -> None:
    path = pid_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pid), encoding="utf-8")


def clear_pid() -> None:
    try:
        pid_path().unlink()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# start / stop / status
# ---------------------------------------------------------------------------


def build_command(interpreter: Path | str, *, console: bool) -> list[str]:
    """Return the command line that runs the application."""
    return [str(interpreter), str(REPO_ROOT / "main.py")]


def spawn_background(interpreter: Path | str, log_path: Path, *, console: bool) -> int:
    """Start the application detached and return its pid."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("a", encoding="utf-8", errors="replace")
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    kwargs: dict[str, Any] = {
        "cwd": str(REPO_ROOT),
        "stdout": handle,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "env": env,
    }
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP
        if not console:
            flags |= subprocess.DETACHED_PROCESS
            flags |= subprocess.CREATE_NO_WINDOW
        kwargs["creationflags"] = flags
        # Keep the log file handle alive for the child's lifetime by holding a
        # reference; the child owns the descriptor after Popen returns.
        kwargs["close_fds"] = True
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(build_command(interpreter, console=console), **kwargs)  # noqa: S603
    write_pid(process.pid)
    return process.pid


def tray_command(env_file: Path) -> list[str] | None:
    """Return the command that shows the notification-area icon, if it can run.

    Windows only: elsewhere SyntH is a service or a foreground process, and there is
    no notification area to put an icon in. It runs on the system PowerShell so the
    tray needs no dependency of its own.
    """
    if os.name != "nt":
        return None
    script = REPO_ROOT / "scripts" / "synth_tray.ps1"
    if not script.is_file():
        return None
    powershell = shutil.which("powershell") or shutil.which("powershell.exe")
    if not powershell:
        return None
    return [
        powershell,
        "-NoProfile",
        "-WindowStyle",
        "Hidden",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-AppRoot",
        str(REPO_ROOT),
        "-EnvFile",
        str(env_file),
    ]


def spawn_tray(env_file: Path) -> bool:
    """Show the tray icon, reporting whether it started.

    Called before waiting for the WebUI, so the icon and its "starting" balloon are
    already on screen while SyntH boots. On a native install the launcher's own
    window closes immediately, and without this the machine looks like it did
    nothing at all. Best-effort: a machine without PowerShell still starts SyntH.
    """
    command = tray_command(env_file)
    if not command:
        return False
    kwargs: dict[str, Any] = {
        "cwd": str(REPO_ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(command, **kwargs)  # noqa: S603
    except Exception:
        return False
    return True


def wants_tray(args: argparse.Namespace) -> bool:
    """Whether this launch should show the tray icon.

    On by default for a desktop launch on Windows, because that is the launch the
    installer creates; a foreground run has its console instead, and ``--no-tray``
    turns it off for anything scripting the launcher.
    """
    if args.no_tray:
        return False
    if args.tray:
        return True
    return os.name == "nt" and not args.foreground


def stop(quiet: bool = False) -> int:
    """Stop the background instance recorded in the pid file."""
    pid = read_pid()
    if not pid or not pid_alive(pid):
        clear_pid()
        if not quiet:
            print("SyntH is not running.")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception as exc:  # noqa: BLE001 - reported verbatim
        if not quiet:
            print(f"could not stop process {pid}: {exc}")
        return 1
    for _ in range(30):
        if not pid_alive(pid):
            break
        time.sleep(0.5)
    clear_pid()
    if not quiet:
        print("SyntH stopped.")
    return 0


def wait_for_webui(env_file: Path, *, timeout: float, quiet: bool) -> tuple[bool, str]:
    """Poll until the WebUI answers; return ``(ok, url)``."""
    deadline = time.time() + timeout
    last_url = ""
    while time.time() < deadline:
        report = check(env_file)
        last_url = str(report.get("url") or "")
        if report.get("ok"):
            return True, last_url
        if not quiet:
            print(f"      starting... ({int(deadline - time.time())}s left)")
        time.sleep(2)
    return False, last_url


def _open_browser(url: str) -> None:
    try:
        import webbrowser

        webbrowser.open(url)
    except Exception:
        pass


#: Where the first-run page lives. The WebUI serves it, and it is a plain page
#: over the existing config and endpoint APIs, so it needs nothing extra.
SETUP_PATH = "/setup"


def setup_url(base: str) -> str:
    """The setup page on *base*, tolerating a missing or trailing-slash URL."""
    base = (base or "").rstrip("/")
    if not base:
        return SETUP_PATH
    return base + SETUP_PATH


def _tail(path: Path, lines: int = 15) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    return "\n".join(content[-lines:])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start, stop or inspect SyntH.")
    parser.add_argument(
        "--foreground", action="store_true", help="run in this terminal"
    )
    parser.add_argument(
        "--stop", action="store_true", help="stop the background instance"
    )
    parser.add_argument(
        "--status", action="store_true", help="report whether SyntH is up"
    )
    parser.add_argument("--console", action="store_true", help="allow a console window")
    parser.add_argument(
        "--no-browser", action="store_true", help="do not open the WebUI"
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="open the setup page instead of the WebUI (starts SyntH if needed)",
    )
    parser.add_argument(
        "--tray",
        action="store_true",
        help="show the notification-area icon (default on for a Windows desktop launch)",
    )
    parser.add_argument(
        "--no-tray",
        action="store_true",
        help="do not show the notification-area icon",
    )
    parser.add_argument(
        "--timeout", type=float, default=240.0, help="startup wait in seconds"
    )
    parser.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    args = parser.parse_args(argv)

    env_file = Path(args.env_file).expanduser()

    if args.status:
        return 0 if check(env_file).get("ok") else 1

    if args.stop:
        return stop()

    # Already running? Then this invocation is just "open my Synth".
    report = check(env_file)
    if report.get("ok"):
        url = str(report.get("url") or "")
        if args.setup:
            url = setup_url(url)
        print(f"SyntH is already running: {url}")
        # Also a launch, from the user's point of view: if the icon is missing
        # (a fresh session, or the icon was hidden) it comes back, and the tray's
        # own single-instance guard keeps a second one from appearing.
        if wants_tray(args):
            spawn_tray(env_file)
        if args.setup or not args.no_browser:
            _open_browser(url)
        return 0

    interpreter = (
        sys.executable if args.foreground else venv_python(windowed=not args.console)
    )
    if not interpreter:
        interpreter = venv_python(windowed=False)
    if not interpreter:
        print(
            "SyntH is not installed yet: no virtual environment found.\n"
            "Run the installer, or: uv sync",
            file=sys.stderr,
        )
        return 2

    if args.foreground:
        os.execv(str(interpreter), build_command(interpreter, console=True))
        return 0  # pragma: no cover - execv does not return

    log_path = log_dir() / "synth.log"
    print("Starting SyntH...")
    print(f"  interpreter: {interpreter}")
    print(f"  log:         {log_path}")
    pid = spawn_background(interpreter, log_path, console=args.console)
    print(f"  pid:         {pid}")

    # Before the wait, not after: this is what tells the user something is happening
    # while the WebUI is still coming up.
    if wants_tray(args) and spawn_tray(env_file):
        print("  tray icon:   shown")

    ok, url = wait_for_webui(env_file, timeout=args.timeout, quiet=False)
    if ok:
        if args.setup:
            url = setup_url(url)
        print(f"\nSyntH is running: {url}")
        if args.setup or not args.no_browser:
            _open_browser(url)
        return 0

    print("\nSyntH did not come up in time.", file=sys.stderr)
    tail = _tail(log_path)
    if tail:
        print("\nLast log lines:\n" + tail, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
