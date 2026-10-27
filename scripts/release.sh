#!/usr/bin/env bash
# Cut a signed AXP release of mintbot Desktop: stamp the version into the
# package, build the deterministic artifact, then let `axp release` set the
# manifest version, fill every sha256, stamp published_at / valid_until and
# sign the manifest with the publisher key. Local build/sign only: nothing is
# committed, tagged, pushed, published, installed or enabled.
#
#   AXP_PROMOTE_FROM=0.1.10-dev.20261027120000 \
#   AXP_SIGNING_KEY=/secure/outside-repo/mintbot.ai-mintbot-desktop.key \
#     scripts/release.sh 0.1.10
#
# A release on the package's own channel (the one LIVE agents install) is a
# PROMOTION of a dev build that DEV's Beaver AXP package journey has installed,
# upgraded to and exercised: AXP_PROMOTE_FROM names that dev build and the
# mintbot repository's developer_tools/axp_promote_check.py confirms the
# evidence (the build exists, this checkout is its commit, a green journey ran
# on it) before anything is stamped or signed. MINTBOT_REPO locates that
# checkout (default ../mintbot beside this one); AXP_PROMOTE_CHECK names the
# gate script itself. The whole flow is written up in that repository:
# developer_tools/agent_skills/axp_package_release.md.
#
# scripts/release-dev.sh runs this script on a scratch export of one commit
# (RELEASE_TREE), on the dev channel (AXP_CHANNEL=dev), with the dev key
# (AXP_SIGNING_KEY_ID) checked against the DEV key directory (AXP_KEYDIR). The
# dev channel is what the gate guards, so a dev build is never gated itself.
#
# The signing key must be listed in the key directory the release is checked
# against; a release signed by an unlisted key is refused here before it can
# reach a host. The freshness window (release.valid_until, SPEC section 7.4)
# is 180 days by default; AXP_VALID_DAYS overrides it.
set -euo pipefail
VERSION="${1:?usage: release.sh <version>}"
KEY="${AXP_SIGNING_KEY:?set AXP_SIGNING_KEY to the publisher private key outside the repo}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TREE="${RELEASE_TREE:-${ROOT}}"
AXP="${AXP_CLI:-axp}"
KEYDIR="${AXP_KEYDIR:-${TREE}/agent-extension-keys.json}"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-dev\.[0-9]{14})?$ ]] || { printf 'invalid version\n' >&2; exit 1; }
test -f "$KEY"
command -v "$AXP" >/dev/null 2>&1 || { printf 'release: the axp CLI is required (AXP_CLI, or pip install -e ../agent-extension)\n' >&2; exit 1; }
CHANNEL="${AXP_CHANNEL:-$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["release"]["channel"])' "${TREE}/agent-extension.json")}"

# The promote gate: a release LIVE agents will install names the dev build it
# promotes, and the evidence for that build is checked before anything changes.
if [ "${CHANNEL}" != "dev" ]; then
  PROMOTE_FROM="${AXP_PROMOTE_FROM:?set AXP_PROMOTE_FROM to the dev build this release promotes (developer_tools/agent_skills/axp_package_release.md in the mintbot repository)}"
  case "${PROMOTE_FROM}" in
    "${VERSION}-dev."*) ;;
    *) printf 'release: %s is not a dev build of %s\n' "${PROMOTE_FROM}" "${VERSION}" >&2; exit 1 ;;
  esac
  if [ -n "$(git -C "${TREE}" status --porcelain)" ]; then
    printf 'release: the checkout has uncommitted changes; promote the dev build'"'"'s commit as it was published\n' >&2
    exit 1
  fi
  GATE="${AXP_PROMOTE_CHECK:-${MINTBOT_REPO:-${ROOT}/../mintbot}/developer_tools/axp_promote_check.py}"
  test -f "${GATE}" || { printf 'release: the promote gate %s is missing (set MINTBOT_REPO or AXP_PROMOTE_CHECK)\n' "${GATE}" >&2; exit 1; }
  python3 "${GATE}" --package mintbot-desktop --version "${PROMOTE_FROM}" --commit "$(git -C "${TREE}" rev-parse HEAD)"
fi

# Signing with a key other than the manifest's own (scripts/release-dev.sh
# signs with the dev key): its key directory entry becomes the signing block.
# `axp release` refuses when the private key does not match that entry.
if [ -n "${AXP_SIGNING_KEY_ID:-}" ]; then
  python3 - "${TREE}/agent-extension.json" "${KEYDIR}" "${AXP_SIGNING_KEY_ID}" <<'PY'
import json, pathlib, sys
path, keydir, key_id = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
keys = json.loads(keydir.read_text(encoding="utf-8"))["keys"]
entry = next((key for key in keys if key.get("key_id") == key_id), None)
if entry is None:
    sys.exit(f"release: {key_id} is not listed in {keydir}")
data = json.loads(path.read_text(encoding="utf-8"))
data["signing"] = {"public_key": entry["public_key"], "key_id": key_id, "next_key": None}
path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
PY
fi

# One version everywhere: the manifest (below), the package and pyproject.
python3 - "$TREE" "$VERSION" <<'PY'
import pathlib, re, sys
root, version = pathlib.Path(sys.argv[1]), sys.argv[2]
for path, name in [(root/'pyproject.toml', 'version'), (root/'mintbot_desktop/__init__.py', '__version__')]:
    text, count = re.subn(r'^' + name + r' = "[^"]+"$', name + ' = "' + version + '"', path.read_text(), flags=re.M)
    if count != 1:
        raise SystemExit('Missing unique version field: ' + str(path))
    path.write_text(text)
PY
bash "$TREE/scripts/build-artifact.sh" "$VERSION"
"$AXP" release "$TREE/agent-extension.json" --version "$VERSION" \
  --artifact "$TREE/dist/mintbot-desktop-$VERSION.tar.gz" --channel "$CHANNEL" \
  --valid-days "${AXP_VALID_DAYS:-180}" --key "$KEY"
"$AXP" validate "$TREE/agent-extension.json"
"$AXP" verify "$TREE/agent-extension.json" --keydir "$KEYDIR"
if [ "$TREE" = "$ROOT" ]; then
  printf 'Local signed release ready for review; nothing published.\n'
  printf 'next: git commit -am "release v%s" && git tag v%s && git push origin main --tags\n' "$VERSION" "$VERSION"
  printf '      gh release create v%s dist/mintbot-desktop-%s.tar.gz agent-extension.json --notes-file CHANGELOG.md\n' "$VERSION" "$VERSION"
fi
