"""The Linux uninstall path: what it removes, what it keeps, and how it says so.

install.sh is the one script, so it is what these run. It refuses to run on anything but
Linux, so `uname` is shimmed onto PATH rather than skipping the tests on the machines most
likely to run them. Everything here is --dry-run: the point is the decisions the script
makes, and a test must never delete a real install.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install.sh"
UNINSTALL_SH = REPO_ROOT / "uninstall.sh"


@pytest.fixture
def fake_uname(tmp_path: Path) -> str:
    """A PATH entry whose `uname` says Linux, so the installer will run here."""
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "uname"
    shim.write_text("#!/bin/sh\necho Linux\n", encoding="utf-8")
    shim.chmod(0o755)
    return str(shim_dir)


def _run_uninstall(
    fake_uname: str, install_dir: Path, *extra: str
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = fake_uname + os.pathsep + env.get("PATH", "")
    return subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--uninstall",
            "--dir",
            str(install_dir),
            "--dry-run",
            *extra,
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=120,
    )


def test_a_plain_uninstall_removes_the_app_and_says_the_database_stays(
    fake_uname: str, tmp_path: Path
) -> None:
    """The database is where the persona lives, so it is the thing worth keeping.

    Removing the application folder takes data/ and .env with it, which is not
    recoverable; the database is a separate system service and survives. The output has
    to say both, because that is the difference between "I lost my Synth" and "I
    reinstalled and she was there".
    """
    install_dir = tmp_path / "SyntH"
    install_dir.mkdir(parents=True)
    result = _run_uninstall(fake_uname, install_dir)

    assert result.returncode == 0, result.stderr
    assert f"rm -rf {install_dir}" in result.stdout
    assert "left alone" in result.stdout
    assert "Deleting the database" not in result.stdout, (
        "a plain uninstall must not touch the database: that is the user's Synth, and "
        "nothing asked for it to go"
    )
    assert "run this again with --purge" in result.stdout
    # Not recoverable, so it is stated whether or not --purge was asked for.
    assert ".env" in result.stdout
    assert "data/" in result.stdout


def test_purge_drops_the_database_it_read_from_env(
    fake_uname: str, tmp_path: Path
) -> None:
    """--purge is the "delete everything", and the identity comes from .env.

    It has to be read before the folder is removed, which is the ordering this pins: a
    purge that runs after the delete would guess the database name.
    """
    install_dir = tmp_path / "SyntH"
    install_dir.mkdir(parents=True)
    (install_dir / ".env").write_text(
        "DB_NAME=my_synth\nDB_USER=my_synth\nDB_HOST=127.0.0.1\nDB_PORT=5433\n",
        encoding="utf-8",
    )
    result = _run_uninstall(fake_uname, install_dir, "--purge")

    assert result.returncode == 0, result.stderr
    assert "dropdb --if-exists my_synth" in result.stdout
    assert "dropuser --if-exists my_synth" in result.stdout
    assert "nothing" in result.stdout and "left" in result.stdout


def test_purge_does_not_drop_a_database_on_another_machine(
    fake_uname: str, tmp_path: Path
) -> None:
    """A remote database is not this machine's to drop, and guessing would be worse."""
    install_dir = tmp_path / "SyntH"
    install_dir.mkdir(parents=True)
    (install_dir / ".env").write_text(
        "DB_NAME=soul2\nDB_USER=soul2\nDB_HOST=192.168.1.13\n", encoding="utf-8"
    )
    result = _run_uninstall(fake_uname, install_dir, "--purge")

    assert result.returncode == 0, result.stderr
    # warn() writes to stderr, which is where a warning belongs.
    combined = result.stdout + result.stderr
    assert "not this machine" in combined
    # The output still tells the user how to do it on that host, so the check is what the
    # script would actually run: --dry-run marks each of those lines with "would run:".
    executed = [line for line in result.stdout.splitlines() if "would run:" in line]
    assert not [line for line in executed if "dropdb" in line or "dropuser" in line], (
        f"a remote database must not be dropped from here: {executed}"
    )


def test_uninstall_sh_is_the_command_it_looks_like() -> None:
    """The file people go looking for exists, is runnable, and is not a second copy.

    Its whole job is to be discoverable: the logic stays in install.sh's --uninstall path
    so there is one place where an uninstall is defined. The executable bit is asserted
    through git's index rather than the filesystem, because on Windows the working tree
    does not carry it and the index is what ships.
    """
    assert UNINSTALL_SH.is_file(), "there is no uninstall.sh to find"
    indexed = subprocess.run(
        ["git", "ls-files", "-s", "uninstall.sh"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
    ).stdout.strip()
    assert indexed.startswith("100755"), (
        f"uninstall.sh is not committed as executable: {indexed!r}"
    )
    text = UNINSTALL_SH.read_text(encoding="utf-8")
    assert 'exec "$here/install.sh" --uninstall "$@"' in text
    assert 'set -- --dir "$here" "$@"' in text, (
        "run from the install folder, it must uninstall that folder rather than the "
        "default location"
    )


def test_the_installer_starts_synth_on_the_setup_page() -> None:
    """The installer starts SyntH at the setup page, rather than telling the user to.

    Measured on a fresh Debian VM: install.sh installed everything correctly, opened a
    browser at a port nothing was listening on, and left nothing running. The launcher
    path that already does start-itself-and-open-/setup is --setup.
    """
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "$LAUNCHER --setup" in text, (
        "install.sh does not start SyntH on the setup page, so the browser it opens has "
        "nothing to connect to"
    )
    assert "--no-start" in text, "there must be a way to install without starting it"
