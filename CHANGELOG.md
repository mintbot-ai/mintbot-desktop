# Changelog

## Unreleased

- A computer's own desktop. On a Linux computer whose owner is logged in
  at an X11 desktop (mintbot on the customer's own computer), `serve` shows
  **that** screen in the panel instead of starting a desktop of its own:
  the user's X11 session is found through the process environment, the
  user's systemd manager environment and logind, proven with `xdpyinfo`,
  and served over RFB by TigerVNC's X0tigervnc (new APT dependency
  `tigervnc-scraping-server`) on the same loopback port with the same
  password file; the gateway and the view are unchanged, the view keeps
  the screen's size. Nothing is started on the owner's screen (no window
  manager, icons or browser of the package's own); `mintbot-desktop
  browser` opens the agent's Chromium there when needed. `env`, `exec`,
  `browse` and `browser` follow the attached display (`state/attached.json`).
  `--mode auto|attach|virtual` (`MINTBOT_DESKTOP_MODE`) and `--attach
  DISPLAY` (`MINTBOT_DESKTOP_ATTACH`) select; a server with no session, a
  Wayland login or a missing X0tigervnc gets the virtual desktop as before,
  with the reason in the log. An owner who logs out ends the session (the
  scraper keeps exiting); the host's restart looks again.
- The install hook runs APT only as root on Debian. Unprivileged (the agent
  on a customer's computer) or on another distribution (Ubuntu) it installs
  nothing and checks that the programs a session cannot do without are
  there (one X server suffices), naming what is missing by Debian package;
  missing optional programs are listed, not refused.

## v0.1.14 (2026-10-28)

- A double-click on the desktop's browser icon no longer takes the whole
  session down. idesk runs an icon's command from its own process (its
  fork() result is compared before it is assigned), so the launcher ran as
  the very idesk the supervisor watches, with the idesk drawing the icons
  left behind as an orphan. The launcher exited at once when a browser
  already ran (the new window goes to that one), the supervisor started
  another idesk, which refused to run beside the orphan, and after three
  refusals the session was given up on: the X server, openbox and the
  browser with it, and the panel showed "Desktop disconnected". The launcher
  now tells when it runs that way: it starts the browser detached, retires
  the orphan and becomes idesk again. The real-GUI smoke test double-clicks
  the icon.
- An icon layer that keeps dying is dropped on its own; the session, and
  the openbox menu that opens the browser, go on without it.
- The desktop view reconnects by itself after the service was restarted
  (an update, a crash) instead of asking for a reload.

## v0.1.13 (2026-10-28)

- The health hook no longer reports a session that is still coming up as
  unhealthy. The host runs `health.sh` the moment it has restarted the
  service after an update, before the gateway listens; the hook now probes
  the gateway for up to 25 seconds before giving up, and prints the reason
  of a failure on stdout, where the host reads the panel's detail from.

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
