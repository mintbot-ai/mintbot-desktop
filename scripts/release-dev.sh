#!/usr/bin/env bash
# Publish a dev build: a commit on main (the newest by default), signed by the
# dev key on the `dev` channel (SPEC section 7.2) and published as a GitHub
# pre-release. .github/workflows/dev-build.yml runs it on every push to main.
#
#   AXP_SIGNING_KEY=/path/to/mintbot.ai-mintbot-desktop-dev.key scripts/release-dev.sh [<commit>]
#
# The dev key is listed only in agent-extension-keys.dev.json, the key
# directory mintbot.dev serves to DEV agents; mintbot.ai never lists it, so a
# LIVE agent refuses every dev build. A host pins the key of its first install
# (SPEC section 8.1): an agent that installed a dev build stays on dev builds,
# one that installed a release never takes one, and moving between the two is
# a reinstall with an explicit re-trust.
#
# The version is the patch after the latest release plus a UTC timestamp,
# e.g. 0.1.10-dev.20261027120000: each dev build sorts above the one before it
# and below the release it leads to.
#
# Three rules keep the two lines apart:
# - A release must be the repository's GitHub "latest". A bare repository URL
#   installs releases/latest and reads the full listing, dev builds included,
#   only when there is none; without one this script refuses.
# - Only the newest dev builds are kept. A host weighs the five newest versions
#   above its own (MAX_RELEASE_CANDIDATES in axp/repository.py), so a pile of
#   dev builds would hide the next release from every host on the package's
#   own channel.
# - A dev build follows every release at once (the workflow runs on release
#   too). A dev-tracking host accepts the package's releases as well, and one
#   signed by the release key must not stand as its newest candidate.
#
# The working tree is never touched: the commit is exported to a scratch
# directory and stamped, built and signed there by scripts/release.sh. The
# promote gate in that script applies to releases, never to a dev build.
set -euo pipefail

REPO="mintbot-ai/mintbot-desktop"
DEV_KEY_ID="mintbot.ai-mintbot-desktop-dev-2026"
KEEP=2
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${AXP_SIGNING_KEY:?set AXP_SIGNING_KEY to the dev private key (never commit it)}"
for tool in "${AXP_CLI:-axp}" gh git; do
  command -v "${tool}" >/dev/null 2>&1 || { echo "release-dev: ${tool} is required" >&2; exit 1; }
done

git -C "${ROOT}" fetch --quiet --tags origin
COMMIT="$(git -C "${ROOT}" rev-parse --verify "${1:-origin/main}^{commit}")"
git -C "${ROOT}" merge-base --is-ancestor "${COMMIT}" origin/main \
  || { echo "release-dev: ${COMMIT} is not on origin/main" >&2; exit 1; }

STABLE="$(gh release view --repo "${REPO}" --json tagName --jq .tagName 2>/dev/null || true)"
if [[ ! "${STABLE}" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
  echo "release-dev: ${REPO} has no stable latest release; a bare repository install would pick a dev build" >&2
  exit 1
fi
VERSION="${BASH_REMATCH[1]}.${BASH_REMATCH[2]}.$((BASH_REMATCH[3] + 1))-dev.$(date -u +%Y%m%d%H%M%S)"

TREE="$(mktemp -d)"
trap 'rm -rf "${TREE}"' EXIT
git -C "${ROOT}" archive "${COMMIT}" | tar -x -C "${TREE}"
RELEASE_TREE="${TREE}" AXP_CHANNEL=dev AXP_SIGNING_KEY_ID="${DEV_KEY_ID}" \
  AXP_KEYDIR="${TREE}/agent-extension-keys.dev.json" bash "${ROOT}/scripts/release.sh" "${VERSION}"

gh release create "v${VERSION}" --repo "${REPO}" --target "${COMMIT}" --prerelease \
  --title "v${VERSION} (dev)" \
  --notes "Dev build of ${COMMIT:0:12} on main, signed by the dev key that only mintbot.dev lists. LIVE agents refuse it." \
  "${TREE}/dist/mintbot-desktop-${VERSION}.tar.gz" "${TREE}/agent-extension.json"

# The timestamp grows with every build, whatever the base version.
gh release list --repo "${REPO}" --limit 100 --json tagName --jq '.[].tagName' \
  | { grep -E '^v[0-9]+\.[0-9]+\.[0-9]+-dev\.[0-9]+$' || true; } \
  | sort -t. -k4,4nr \
  | tail -n "+$((KEEP + 1))" \
  | while read -r old; do
      gh release delete "${old}" --repo "${REPO}" --cleanup-tag --yes
    done

# The workflow's next step queues DEV's Beaver package tests for this build.
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  printf 'version=%s\ncommit=%s\n' "${VERSION}" "${COMMIT}" >> "${GITHUB_OUTPUT}"
fi

echo "published v${VERSION}: https://github.com/${REPO}/releases/download/v${VERSION}/agent-extension.json"
