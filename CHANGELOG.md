# Changelog

## v0.1.12 (2026-10-28)

- The desktop icons no longer cover application windows. openbox lowers an
  anchor window to the bottom of the stack when it starts and keeps every
  window beneath that anchor; idesk lowers its icons only once, when it
  creates them, so icons that existed before openbox started stayed above
  every window for good. The icon layer now starts only once openbox has
  published `_NET_SUPPORTING_WM_CHECK` on the root window and on the anchor
  it names (the root's copy outlives a dead openbox), and is started over
  whenever the supervised openbox comes back after a crash. The real-GUI
  smoke test checks the stacking after the start and after a killed openbox.

## v0.1.11 (2026-10-27)

- `.github/workflows/dev-build.yml` reports every published dev build to DEV
  CatOffice (`POST /beaver/dev-build`, repository secret
  `BEAVER_DEV_BUILD_TOKEN`), which queues Beaver's AXP package tests on it;
  `scripts/release-dev.sh` tells the workflow the version and commit it
  published (`GITHUB_OUTPUT`).
- Dev builds: `.github/workflows/dev-build.yml` runs `scripts/release-dev.sh`
  on every push to `main`, publishing `<next patch>-dev.<UTC timestamp>` on the
  `dev` channel as a GitHub pre-release, signed by a dev key
  (`mintbot.ai-mintbot-desktop-dev-2026`) that only mintbot.dev's key directory
  lists (`agent-extension-keys.dev.json`); LIVE agents refuse them. Only the
  newest two dev builds are kept.
- `scripts/release.sh` takes the tree to sign (`RELEASE_TREE`), the channel
  (`AXP_CHANNEL`) and the signing key id (`AXP_SIGNING_KEY_ID`, checked
  against `AXP_KEYDIR`), and gates a release on the package's own channel: it
  names the dev build it promotes (`AXP_PROMOTE_FROM`), refuses a dirty
  checkout, and the mintbot repository's promote check must confirm a green
  DEV Beaver package journey on that build before anything is signed.
- `AGENTS.md` points at the mintbot repository's AXP package flow.

## 0.1.10

- The browser starts once with the desktop and is not restarted when the
  user closes it (the window manager and the icon layer stay supervised). A
  closed browser is opened again by the new desktop icon, by the openbox
  right-click menu, or by `mintbot-desktop browser`; `browse` says so when
  CDP does not answer.
- A desktop icon for the browser, drawn by `idesk` (new APT dependency,
  installed by the install/upgrade hook) with Chromium's own icon; the icon
  file and the openbox root menu are written to the state directory on every
  start. Without `idesk` the menu entry still works.
- The browser window carries Chromium's own `_NET_WM_ICON` and `WM_CLASS`
  (`Chromium`), which openbox shows; the real-GUI smoke test now checks both,
  closes the browser, verifies it stays closed, and reopens it.

## 0.1.9

- Separate AXP host-managed lifecycle from standalone foreground operation using
  the existing `AXP_HOST` environment variable. Standalone install reports a
  runnable command; standalone enable runs it without detaching. The host's
  command-service mapping and activation sequence are unchanged.
- Remove the portable hooks' hardcoded Mintbot systemd service operations.
  Disable and uninstall check the existing supervisor lock and refuse an active
  session instead of stopping services owned by someone else.
- Remove the dedicated standalone prefix on uninstall while preserving durable
  state by default. Require a matching installation record; refuse unsafe,
  symlinked or overlapping cleanup paths and nonempty unowned install prefixes.
  Keep failed browser-driver installation retryable. Explicit purge remains
  opt-in; shared system packages and cache are not removed.
- Document standalone installation, foreground use, supervisor ownership,
  authenticated browser access, cleanup and the hostless trust trade-off.
- Add isolated lifecycle regressions with injected command execution and real
  filesystem/lock checks. No live installer or service manager is used.

The release gate rebuilds the deterministic artifact, compares both manifest
digests, and verifies the publisher signature. Lifecycle tests require no signing
key and never install system packages or change live services.
