# Changelog

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
