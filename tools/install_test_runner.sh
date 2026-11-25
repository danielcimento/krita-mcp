#!/usr/bin/env bash
# Symlink the headless test runner shim into kritarunner's own profile.
#
# kritarunner (headless Krita) imports modules only from its profile's
# pykrita/ folder, which is separate from the GUI Krita's profile:
#   macOS: ~/Library/Application Support/kritarunner/pykrita/
#   Linux: ~/.local/share/kritarunner/pykrita/   (best effort, untested)
# Override with KRITARUNNER_PYKRITA_DIR. Safe to run repeatedly.
#
# Usage: tools/install_test_runner.sh [--force]
#   --force  move an existing non-symlink kmcp_test_runner.py aside
#            (to kmcp_test_runner.py.bak-<timestamp>) instead of refusing.
set -euo pipefail

force=0
for arg in "$@"; do
  case "$arg" in
    --force) force=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src="$repo/tests/krita/kmcp_test_runner.py"

if [ -n "${KRITARUNNER_PYKRITA_DIR:-}" ]; then
  dest="$KRITARUNNER_PYKRITA_DIR"
elif [ "$(uname -s)" = "Darwin" ]; then
  dest="$HOME/Library/Application Support/kritarunner/pykrita"
else
  dest="${XDG_DATA_HOME:-$HOME/.local/share}/kritarunner/pykrita"
  echo "note: Linux kritarunner path is untested: $dest"
fi

[ -f "$src" ] || { echo "missing $src" >&2; exit 1; }
mkdir -p "$dest"
target="$dest/kmcp_test_runner.py"

if [ -e "$target" ] && [ ! -L "$target" ]; then
  if [ "$force" -ne 1 ]; then
    echo "refusing: $target exists and is not a symlink (re-run with --force to move it aside)" >&2
    exit 1
  fi
  backup="$target.bak-$(date +%Y%m%d-%H%M%S)"
  mv "$target" "$backup"
  echo "moved aside: $target -> $backup"
fi

ln -sfn "$src" "$target"
echo "linked: $target -> $src"
