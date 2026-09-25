#!/usr/bin/env bash
# Synthetic Heart — one-click installer for Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/XargonWan/Synthetic_Heart/develop/install.sh | bash
#
# It installs the system packages SyntH needs, installs uv (which brings its own
# Python, so no Python prerequisite), fetches the source, then hands over to
# scripts/bootstrap.py, which creates the database, writes .env, picks free ports
# and installs the Python environment. Finally it adds a launcher and a desktop
# entry, so the user never has to touch a terminal again.
#
# Nothing about your Synth is decided here: their name, your name, your location,
# timezone and the engine + API key are collected by the WebUI setup page on the
# first launch.
#
# Usage:
#   ./install.sh                     install for the current user
#   ./install.sh --dir ~/SyntH       choose the install directory
#   ./install.sh --portable          no sudo: private PostgreSQL cluster
#   ./install.sh --extra local-voice add offline TTS/STT (large, ~2 GB with torch)
#   ./install.sh --uninstall         remove the app (the database is left alone)
#   ./install.sh --dry-run           print what would happen
#
set -euo pipefail

APP_NAME="SyntH"
DEFAULT_REPO="https://github.com/XargonWan/Synthetic_Heart.git"
DEFAULT_BRANCH="develop"
INSTALL_DIR="${SYNTH_INSTALL_DIR:-$HOME/.local/share/SyntH}"
REPO_URL="${SYNTH_REPO_URL:-$DEFAULT_REPO}"
BRANCH="${SYNTH_BRANCH:-$DEFAULT_BRANCH}"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"
EXTRAS=()
DO_DESKTOP=1
PORTABLE=0
DRY_RUN=0
UNINSTALL=0
SKIP_PACKAGES=0

# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
else
    BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; RESET=""
fi

say()  { printf '%s\n' "$*"; }
step() { printf '%s\n' "${BOLD}$*${RESET}"; }
ok()   { printf '  %s %s\n' "${GREEN}ok${RESET}" "$*"; }
note() { printf '  %s\n' "${DIM}$*${RESET}"; }
warn() { printf '  %s %s\n' "${YELLOW}warning:${RESET}" "$*" >&2; }
die()  { printf '%s %s\n' "${RED}error:${RESET}" "$*" >&2; exit 1; }

run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  %s %s\n' "${DIM}would run:${RESET}" "$*"
        return 0
    fi
    "$@"
}

# ---------------------------------------------------------------------------
# arguments
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --dir) INSTALL_DIR="$2"; shift 2 ;;
        --repo) REPO_URL="$2"; shift 2 ;;
        --branch) BRANCH="$2"; shift 2 ;;
        --extra) EXTRAS+=("$2"); shift 2 ;;
        --portable) PORTABLE=1; shift ;;
        --no-desktop) DO_DESKTOP=0; shift ;;
        --skip-packages) SKIP_PACKAGES=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help) sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
done

case "$(uname -s)" in
    Linux) ;;
    Darwin) die "this installer is for Linux; on macOS use the Docker or manual path" ;;
    *) die "unsupported platform: $(uname -s)" ;;
esac

# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------
uninstall() {
    step "Removing $APP_NAME"
    if [ -x "$INSTALL_DIR/.venv/bin/python" ]; then
        run "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/scripts/start_synth.py" --stop || true
    fi
    run rm -f "$BIN_DIR/synth"
    run rm -f "$DESKTOP_DIR/synth.desktop"
    run rm -rf "$INSTALL_DIR"
    ok "application files removed"
    say ""
    say "Your database and your Synth's memory were left untouched."
    say "The .env in the install directory went with it, and it held the generated"
    say "database credentials and any API keys you added. Keep a copy beforehand if"
    say "you set your own; the Windows uninstaller asks about this instead."
    say "To remove the database too:  sudo -u postgres dropdb synth && sudo -u postgres dropuser synth"
    exit 0
}
[ "$UNINSTALL" -eq 1 ] && uninstall

# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------
step "Preparing to install $APP_NAME"
note "install directory: $INSTALL_DIR"
note "source:            $REPO_URL ($BRANCH)"

PKG=""
if command -v apt-get >/dev/null 2>&1; then PKG="apt"
elif command -v dnf >/dev/null 2>&1; then PKG="dnf"
elif command -v pacman >/dev/null 2>&1; then PKG="pacman"
elif command -v zypper >/dev/null 2>&1; then PKG="zypper"
fi

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi
fi

have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------
# 1. system packages
# ---------------------------------------------------------------------------
if [ "$SKIP_PACKAGES" -eq 0 ] && [ "$PORTABLE" -eq 0 ]; then
    step "Installing system packages (PostgreSQL + pgvector, ffmpeg)"
    if [ -z "$PKG" ]; then
        warn "unknown distribution: install PostgreSQL 14+ with pgvector and ffmpeg yourself"
    else
        case "$PKG" in
            apt)
                run $SUDO apt-get update -qq
                run $SUDO apt-get install -y postgresql postgresql-contrib ffmpeg git curl ca-certificates
                PG_MAJOR="$( (psql --version 2>/dev/null || true) | grep -oE '[0-9]+' | head -1)"
                if [ -n "$PG_MAJOR" ]; then
                    run $SUDO apt-get install -y "postgresql-${PG_MAJOR}-pgvector" \
                        || warn "package postgresql-${PG_MAJOR}-pgvector not available; memory search needs pgvector"
                fi
                ;;
            dnf)
                run $SUDO dnf install -y postgresql-server postgresql-contrib ffmpeg git curl
                run $SUDO dnf install -y pgvector || warn "pgvector package not available on this distribution"
                if ! [ -d /var/lib/pgsql/data ]; then
                    run $SUDO postgresql-setup --initdb || true
                fi
                run $SUDO systemctl enable --now postgresql || warn "could not start PostgreSQL automatically"
                ;;
            pacman)
                run $SUDO pacman -S --noconfirm postgresql pgvector ffmpeg git curl
                if ! [ -d /var/lib/postgres/data ]; then
                    run $SUDO -u postgres initdb -D /var/lib/postgres/data || true
                fi
                run $SUDO systemctl enable --now postgresql || warn "could not start PostgreSQL automatically"
                ;;
            zypper)
                run $SUDO zypper --non-interactive install postgresql-server postgresql-contrib ffmpeg git curl
                run $SUDO zypper --non-interactive install postgresql16-pgvector \
                    || warn "pgvector package not available; memory search needs pgvector"
                ;;
        esac
        ok "system packages present"
    fi
else
    step "Skipping system packages"
    if [ "$PORTABLE" -eq 1 ]; then
        note "--portable: scripts/bootstrap.py creates a private PostgreSQL cluster"
    fi
fi

# ---------------------------------------------------------------------------
# 2. uv (brings its own Python, so Python is not a prerequisite)
# ---------------------------------------------------------------------------
step "Installing uv"
if have uv; then
    ok "uv already installed ($(uv --version 2>/dev/null | head -1))"
elif [ "$DRY_RUN" -eq 1 ]; then
    note "would install uv from https://astral.sh/uv/install.sh"
else
    curl -LsSf https://astral.sh/uv/install.sh | sh || die "uv installation failed"
    export PATH="$HOME/.local/bin:$PATH"
    have uv || die "uv was installed but is not on PATH; open a new terminal and re-run"
    ok "uv installed"
fi

# ---------------------------------------------------------------------------
# 3. source
# ---------------------------------------------------------------------------
step "Fetching $APP_NAME"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "")"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/pyproject.toml" ] && [ -f "$SCRIPT_DIR/main.py" ]; then
    INSTALL_DIR="$SCRIPT_DIR"
    ok "running from an existing checkout: $INSTALL_DIR"
elif [ -f "$INSTALL_DIR/main.py" ] && [ -f "$INSTALL_DIR/scripts/bootstrap.py" ]; then
    ok "existing installation found, updating"
    run git -C "$INSTALL_DIR" pull --ff-only || warn "could not update; continuing with the current version"
else
    have git || die "git is required to fetch the source"
    run mkdir -p "$(dirname "$INSTALL_DIR")"
    run git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR" \
        || die "could not clone $REPO_URL (branch $BRANCH)"
    ok "source ready"
fi

cd "$INSTALL_DIR"

# ---------------------------------------------------------------------------
# 4. database, .env, ports, dependencies  (scripts/bootstrap.py)
# ---------------------------------------------------------------------------
BOOTSTRAP_ARGS=()
[ "$PORTABLE" -eq 1 ] && BOOTSTRAP_ARGS+=(--portable)
[ "$DRY_RUN" -eq 1 ] && BOOTSTRAP_ARGS+=(--dry-run)
[ "$DRY_RUN" -eq 1 ] && BOOTSTRAP_ARGS+=(--skip-sync)
for extra in "${EXTRAS[@]:-}"; do
    [ -n "$extra" ] && BOOTSTRAP_ARGS+=(--extra "$extra")
done

PY_FOR_BOOTSTRAP=""
for candidate in python3 python; do
    # Verify it actually runs: a Windows "App execution alias" stub, or a broken
    # interpreter, would otherwise be chosen and fail only later.
    if have "$candidate" && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
        PY_FOR_BOOTSTRAP="$candidate"
        break
    fi
done
if [ -z "$PY_FOR_BOOTSTRAP" ]; then
    # uv can run the script without any usable system Python at all.
    note "no usable system Python found; using the uv-managed interpreter"
    PY_FOR_BOOTSTRAP="uv run --no-project python"
fi

# shellcheck disable=SC2086
$PY_FOR_BOOTSTRAP scripts/bootstrap.py "${BOOTSTRAP_ARGS[@]}" || die "setup failed (see the messages above)"

# ---------------------------------------------------------------------------
# 5. launcher + desktop entry
# ---------------------------------------------------------------------------
step "Adding the launcher"
if [ "$DRY_RUN" -eq 1 ]; then
    note "would write $BIN_DIR/synth and $DESKTOP_DIR/synth.desktop"
else
    mkdir -p "$BIN_DIR" "$DESKTOP_DIR"
    cat > "$BIN_DIR/synth" <<EOF
#!/usr/bin/env bash
# Start SyntH (and open the WebUI). Use: synth [--status|--stop|--foreground]
exec "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/scripts/start_synth.py" "\$@"
EOF
    chmod +x "$BIN_DIR/synth"
    ok "command: synth"

    if [ "$DO_DESKTOP" -eq 1 ]; then
        ICON="$INSTALL_DIR/installer/synth-256.png"
        [ -f "$ICON" ] || ICON="$INSTALL_DIR/website/assets/synth_logo_bg.png"
        cat > "$DESKTOP_DIR/synth.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$APP_NAME
Comment=Talk to your SyntH
Exec=$INSTALL_DIR/.venv/bin/python $INSTALL_DIR/scripts/start_synth.py
Icon=$ICON
Terminal=false
Categories=Utility;
StartupNotify=false
EOF
        chmod +x "$DESKTOP_DIR/synth.desktop"
        ok "desktop entry installed (it appears in your applications menu)"
    fi
fi

# ---------------------------------------------------------------------------
# done
# ---------------------------------------------------------------------------
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$BIN_DIR is not on your PATH; add it to run 'synth' by name" ;;
esac

say ""
if [ "$DRY_RUN" -eq 1 ]; then
    say "${BOLD}Dry run complete.${RESET} Nothing was changed."
else
    say "${BOLD}$APP_NAME is installed.${RESET}"
    say "  Start it:   synth"
    say "  Status:     synth --status"
    say "  Stop it:    synth --stop"
    say ""
    say "The first page in your browser is the setup page: it asks for your SyntH's"
    say "name, your name, your location and timezone, and the engine + API key they"
    say "should think with. Nothing else is needed."
fi
