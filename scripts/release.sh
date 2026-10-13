#!/usr/bin/env bash
# Local build/sign only. Does not commit, tag, push, publish, or install.
set -euo pipefail
VERSION="${1:?usage: release.sh <version>}"
KEY="${AXP_SIGNING_KEY:?set AXP_SIGNING_KEY to the publisher private key outside the repo}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AXP="${AXP_CLI:-axp}"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { printf 'invalid version\n' >&2; exit 1; }
test -f "$KEY"
# The public key directory authorizes this exact publisher/name/key combination.
python3 - "$ROOT" "$VERSION" <<'PY'
import pathlib, re, sys
root, version = pathlib.Path(sys.argv[1]), sys.argv[2]
for path, name in [(root/'pyproject.toml', 'version'), (root/'mintbot_desktop/__init__.py', '__version__')]:
    text, count = re.subn(r'^' + name + r' = "[^"]+"$', name + ' = "' + version + '"', path.read_text(), flags=re.M)
    if count != 1:
        raise SystemExit('Missing unique version field: ' + str(path))
    path.write_text(text)
PY
bash "$ROOT/scripts/build-artifact.sh" "$VERSION"
"$AXP" release "$ROOT/agent-extension.json" --version "$VERSION" \
  --artifact "$ROOT/dist/mintbot-desktop-$VERSION.tar.gz" --channel beta \
  --valid-days "${AXP_VALID_DAYS:-180}" --key "$KEY"
"$AXP" validate "$ROOT/agent-extension.json"
"$AXP" verify "$ROOT/agent-extension.json" --keydir "$ROOT/agent-extension-keys.json"
printf 'Local signed release ready for review; nothing published.\n'
