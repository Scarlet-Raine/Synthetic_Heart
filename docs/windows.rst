Windows (Native) — Running Synthetic Heart on Microsoft Windows
===============================================================

This page documents how to run Synthetic Heart natively on Windows (non-Docker).


Installing it
-------------

Run ``SyntH-Setup-<version>.exe`` from the releases page. It needs no
administrator rights and asks you nothing: there is one option, and it installs
into ``%LOCALAPPDATA%\Programs\SyntH``.

What it does, in order:

1. ``scripts/install_prereqs.ps1`` provisions ``uv`` (which brings its own
   Python, so no Python needs to be installed), unpacks the official
   EnterpriseDB PostgreSQL binaries into ``pgsql\`` inside the install folder,
   and copies pgvector into place if the release shipped it.
2. ``scripts/bootstrap.py --portable`` creates a private PostgreSQL cluster in
   ``data\pgsql`` on a free port, creates the role and the database, writes a
   technical ``.env``, and runs ``uv sync``.
3. Shortcuts are created (Start Menu, and Desktop if you asked for one), then
   SyntH starts and your browser opens the setup page.

Both helper steps run hidden, through ``pythonw.exe`` where possible, so no
console window ever flashes. If a step fails, the installer says which one and
where its log is rather than failing silently.

The setup page is where the personal answers go: the persona's name and
character, your name, timezone, language, location, how you physically talk to
it, and which AI provider to use with its API key. Nothing about you is asked
during installation. It is reachable afterwards at ``/setup``, and all of it is
in Settings.

Files that matter after installation:

===================  =========================================================
``%LOCALAPPDATA%``   ``\Programs\SyntH`` — the application, its Python, its
                     PostgreSQL binaries and its database
``data\``            the database cluster, attachments, and your encrypted
                     API keys
``.env``             the technical configuration bootstrap wrote
``logs\``            ``synth.log`` and rotated logs
===================  =========================================================

Uninstalling removes the application, its Python and its PostgreSQL, and keeps
``data\`` and ``.env`` so that reinstalling resumes with the same persona,
history and keys. Delete those two folders by hand for a clean slate.

.. note::

   Windows SmartScreen will warn about an unrecognised publisher. That is
   expected for an unsigned installer; choose "More info" then "Run anyway".
   Code signing is not currently in place.


Manual installation
-------------------

Prerequisites, all of which the installer above handles for you:

- ``uv`` — https://docs.astral.sh/uv/ (it also provides Python; the project
  pins its version in ``.python-version``)
- A PostgreSQL server, or the EnterpriseDB binaries for ``--portable``
- **ffmpeg** on PATH for multimodal video/audio processing and Discord voice.
  Without it those features are simply unavailable.
- Node.js only if you want the Minecraft vessel bridge.

Steps:

.. code-block:: powershell

   # 1. Dependencies
   uv sync

   # 2. Database, .env and free ports (add --portable to unpack a private server)
   uv run --no-project python scripts\bootstrap.py

   # 3. Start it, then open the WebUI
   uv run --no-project python scripts\start_synth.py

``scripts\start_synth.py`` runs SyntH through ``pythonw.exe`` so no console
window appears, waits until the WebUI answers, then opens your browser. It also
takes ``--status``, ``--stop``, ``--setup`` and ``--no-browser``.

To check a running instance:

.. code-block:: powershell

   uv run --no-project python scripts\healthcheck.py


Notes and caveats
-----------------

- The ``webtop/`` folder contains container-oriented scripts and an embedded
  Linux desktop (PulseAudio, X server). Those are Docker-only conveniences and
  are irrelevant to a native install; the Windows installer does not ship them
  as anything runnable.
- PostgreSQL is the only supported database. MariaDB deployments are migrated
  automatically on first start, or deliberately with
  ``scripts\migrate_main_db_to_postgres.py``; see :doc:`installation`.
- Older builds wrote state to the container paths ``/app`` and ``/config``.
  On Windows those are drive-relative and became real folders (``D:\config``,
  ``D:\app``), which is where an upgraded install's ``.synth_secret`` and
  attachments still are. SyntH adopts that state into ``data\`` on startup,
  once, without deleting the originals. Inspect it with
  ``uv run --no-project python -m core.legacy_state --dry-run``.
- Selenium-based engines are being replaced by other engines; if you still use
  them, install Chrome and ``undetected-chromedriver``.
- Some tests assume a database or other services; point them at local ones with
  environment variables or let them skip.


Maintenance guidance for maintainers
------------------------------------

- Runtime behaviour is controlled by environment variables and the config
  registry; keep it that way.
- Anything that assumes a container path must go through ``core/app_paths.py``.
  ``scripts/check_dockerisms.py`` fails the build on un-allowlisted ``/app``,
  ``/config`` and ``host.docker.internal`` occurrences; it runs in CI.
- When adding container-specific scripts, document them as such and keep the
  native path working.


Building the installer
----------------------

Inno Setup 6 is required (``winget install JRSoftware.InnoSetup``).

.. code-block:: powershell

   # Builds installer\Output\SyntH-Setup-<version>.exe
   .\installer\build_installer.ps1

The version comes from GitVersion (``GitVersion.yml``); the script falls back to
the newest git tag, then to ``0.0.0-dev``. CI passes the release version and
also builds the pgvector DLLs the installer vendors — see
``installer/vendor/README.md``. Add ``/DWithExampleSkins=1`` to include the
example personas (about 80 MB of VRM models that the installer leaves out by
default):

.. code-block:: powershell

   iscc "/DAppVersion=1.2.3" "/DWithExampleSkins=1" installer\synth-installer.iss


Testing branches & automation
-----------------------------

A convenient helper script to prepare a testing branch and optionally create a Draft PR is available at `tools/push_windows_branch.ps1`.

Example usage (PowerShell):

.. code-block:: powershell

   # Create a branch, commit the current changes, push, and attempt to open a Draft PR via the GitHub CLI
   pwsh .\tools\push_windows_branch.ps1 -CreatePR

   # Provide an explicit branch name and title
   pwsh .\tools\push_windows_branch.ps1 -BranchName "windows-automation/add-windows-ci" -CreatePR -Title "WIP: Windows support"

Notes:
- The script will ensure a branch named `windows-automation/*` is created and pushed to `origin`. The repository is configured to auto-open a Draft PR for branches under `windows-automation/**`.
- If you use automation agents to commit/push, point them at the same script or replicate its commands. Keep tokens / credentials in GitHub Secrets and avoid hardcoding personal tokens.

If you prefer, run the script locally and it will stage, commit, and push the prepared files (it also removes `docs/windows.md` in favor of `docs/windows.rst`). The `-CreatePR` flag attempts to create a Draft PR using `gh` if available.
