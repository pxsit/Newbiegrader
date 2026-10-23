#!/bin/sh
# Switch the project venv to the Python pinned in bootstrap.sh, keeping the old
# venv for rollback. Run as root with the API and every worker stopped.
#   upgrade_python.sh /opt/newbiegrader/releases/<release>
#   upgrade_python.sh --rollback
# Services are never started here; verify first (docs/PYTHON_UPGRADE.md).
set -eu
test "$(id -u)" = 0
PREFIX=/usr/local/lib/newbiegrader
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VENV="$PREFIX/venv"
PREVIOUS="$PREFIX/venv.previous"

active=''
for unit in newbiegrader-api.service newbiegrader-worker.service \
        newbiegrader-worker@1.service newbiegrader-worker@2.service \
        newbiegrader-worker@3.service newbiegrader-worker@4.service \
        newbiegrader-canary.service newbiegrader-discord-presence.service; do
    if systemctl is-active --quiet "$unit"; then active="$active $unit"; fi
done
if test -n "$active"; then
    echo "Stop these units and drain jobs first:$active" >&2
    exit 1
fi

if test "${1:-}" = '--rollback'; then
    test -d "$PREVIOUS" || { echo "No $PREVIOUS to restore" >&2; exit 1; }
    rm -rf "$PREFIX/venv.failed"
    mv "$VENV" "$PREFIX/venv.failed"
    mv "$PREVIOUS" "$VENV"
    "$VENV/bin/python" -V
    echo "Rolled back. The upgraded venv is kept at $PREFIX/venv.failed."
    exit 0
fi

RELEASE=${1:?usage: upgrade_python.sh /opt/newbiegrader/releases/<release> | --rollback}
RELEASE=$(readlink -f "$RELEASE")
test -f "$RELEASE/deploy/host/requirements.lock"
# bootstrap.sh is the single source of truth for both pins.
PYTHON=$(sed -n "s/.*uv python install \([0-9][0-9.]*\)\$/\1/p" "$HERE/bootstrap.sh")
UV=$(sed -n "s/.*'uv==\([0-9][0-9.]*\)'.*/\1/p" "$HERE/bootstrap.sh")
test -n "$PYTHON" && test -n "$UV"

current=$("$VENV/bin/python" -c 'import platform; print(platform.python_version())' 2>/dev/null || true)
if test "$current" = "$PYTHON"; then
    echo "Already on Python $PYTHON; nothing to do."
    exit 0
fi
if test -e "$PREVIOUS"; then
    echo "$PREVIOUS exists; remove it (or roll back) before upgrading again." >&2
    exit 1
fi

"$PREFIX/bootstrap/bin/pip" install --disable-pip-version-check "uv==$UV"
export UV_PYTHON_INSTALL_DIR="$PREFIX/python"
"$PREFIX/bootstrap/bin/uv" python install "$PYTHON"

mv "$VENV" "$PREVIOUS"
# Any failure past this point restores the old venv automatically.
restore() {
    status=$?
    if test "$status" -ne 0; then
        rm -rf "$VENV"
        mv "$PREVIOUS" "$VENV"
        echo "Upgrade failed; previous venv restored." >&2
    fi
    exit "$status"
}
trap restore EXIT
umask 0022
"$PREFIX/bootstrap/bin/uv" venv --python "$PYTHON" "$VENV"
"$PREFIX/bootstrap/bin/uv" pip install --python "$VENV/bin/python" \
    -r "$RELEASE/deploy/host/requirements.lock"
"$VENV/bin/python" "$RELEASE/deploy/host/precompile_runtime.py" --release "$RELEASE"
runuser -u newbiegrader-api -- "$VENV/bin/python" -c \
    'import fastapi, supabase, certifi, ssl; ssl.create_default_context(cafile=certifi.where())'
trap - EXIT
"$VENV/bin/python" -V
echo "Python $PYTHON is ready. Previous venv kept at $PREVIOUS."
echo "Next: verify-workers.sh, start the API and workers, then run the canaries."
