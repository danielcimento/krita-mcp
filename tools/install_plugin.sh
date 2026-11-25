#!/usr/bin/env bash
# Install the plugin from this checkout into the GUI Krita by symlink, so
# edits take effect on the next Krita restart without copying.
#
# Links krita-plugin/kritamcp and krita-plugin/kritamcp.desktop into:
#   macOS: ~/Library/Application Support/krita/pykrita/
#   Linux: ~/.local/share/krita/pykrita/
# Override the target with KRITA_PYKRITA_DIR.
#
# An existing symlink is replaced. An existing real file or directory (for
# example a copied install) is never deleted: the script refuses, unless
# --force is given, in which case it is moved aside to <name>.bak-<timestamp>.
#
# Usage: tools/install_plugin.sh [--force]
set -euo pipefail

force=0
for arg in "$@"; do
  case "$arg" in
    --force) force=1 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
names=(kritamcp kritamcp.desktop)

if [ -n "${KRITA_PYKRITA_DIR:-}" ]; then
  dest="$KRITA_PYKRITA_DIR"
elif [ "$(uname -s)" = "Darwin" ]; then
  dest="$HOME/Library/Application Support/krita/pykrita"
else
  dest="${XDG_DATA_HOME:-$HOME/.local/share}/krita/pykrita"
fi

for name in "${names[@]}"; do
  [ -e "$repo/krita-plugin/$name" ] || { echo "missing $repo/krita-plugin/$name" >&2; exit 1; }
done

# Check everything before changing anything.
blocked=()
for name in "${names[@]}"; do
  target="$dest/$name"
  if [ -e "$target" ] && [ ! -L "$target" ]; then
    blocked+=("$target")
  fi
done
if [ "${#blocked[@]}" -gt 0 ] && [ "$force" -ne 1 ]; then
  echo "refusing: these exist and are not symlinks (a copied install?):" >&2
  printf '  %s\n' "${blocked[@]}" >&2
  echo "re-run with --force to move them aside to <name>.bak-<timestamp>" >&2
  exit 1
fi

mkdir -p "$dest"
stamp="$(date +%Y%m%d-%H%M%S)"
for name in "${names[@]}"; do
  src="$repo/krita-plugin/$name"
  target="$dest/$name"
  if [ -L "$target" ]; then
    old="$(readlink "$target")"
    if [ "$old" = "$src" ]; then
      echo "already linked: $target -> $src"
      continue
    fi
    echo "replacing symlink: $target (was -> $old)"
  elif [ -e "$target" ]; then
    backup="$target.bak-$stamp"
    if [ -e "$backup" ] || [ -L "$backup" ]; then
      echo "backup path already exists: $backup" >&2
      exit 1
    fi
    mv "$target" "$backup"
    echo "moved aside: $target -> $backup"
  fi
  ln -sfn "$src" "$target"
  echo "linked: $target -> $src"
done

echo "Restart Krita. If the plugin is new to this profile, enable \"Krita MCP Bridge\""
echo "in Settings > Configure Krita > Python Plugin Manager, then restart again."
