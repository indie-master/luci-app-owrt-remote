#!/usr/bin/env sh
# Run from a reviewed, checked-out Git commit. Never pipe a moving branch to sh.
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$SCRIPT_DIR/manager.py" install "$@"
