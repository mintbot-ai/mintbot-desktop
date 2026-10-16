# Changelog

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
