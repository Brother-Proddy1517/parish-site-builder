#!/usr/bin/env bash
# Installs site_build.py as a regular command: `site-build`.
# Run this once from inside the repo directory, with sudo.
#
#   cd parish-site-builder
#   sudo ./install.sh
#
# After this, use it like any other command:
#   sudo site-build create messiah --domain messiah.example.org
#
# It's a symlink, not a copy, so future `git pull`s keep it up to date
# automatically — no need to re-run this after every update.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$SCRIPT_DIR/site_build.py"
LINK="/usr/local/bin/site-build"

if [ "$EUID" -ne 0 ]; then
    echo "Run this with sudo: sudo ./install.sh" >&2
    exit 1
fi

chmod +x "$TARGET"
ln -sf "$TARGET" "$LINK"

echo "Installed: $LINK -> $TARGET"
echo "Try it: sudo site-build list"
