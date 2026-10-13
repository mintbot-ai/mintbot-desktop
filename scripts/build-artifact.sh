#!/usr/bin/env bash
set -euo pipefail
VERSION="${1:?usage: build-artifact.sh <version> [out-dir]}"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { printf 'invalid version\n' >&2; exit 1; }
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${2:-${ROOT}/dist}"
FILES=(
  mintbot_desktop/__init__.py mintbot_desktop/__main__.py mintbot_desktop/app.py
  mintbot_desktop/session.py mintbot_desktop/lifecycle.py
  pyproject.toml install.sh upgrade.sh health.sh enable.sh disable.sh uninstall.sh
  skills/mintbot-desktop/SKILL.md README.md LICENSE after-install.md
)
for file in "${FILES[@]}"; do test -f "$ROOT/$file" || { printf 'missing %s\n' "$file" >&2; exit 1; }; done
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/mintbot-desktop-$VERSION.tar.gz"
tar --sort=name --mtime='@0' --owner=0 --group=0 --numeric-owner --mode='u+rwX,go+rX,go-w' \
  -C "$ROOT" -cf - "${FILES[@]}" | gzip -n -9 > "$OUT"
sha256sum "$OUT"
