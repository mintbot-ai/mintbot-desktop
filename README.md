# mintbot Desktop

An opt-in shared graphical desktop packaged as the official
`mintbot.ai/mintbot-desktop` AXP 0.5 extension. It provides a generic panel view
(`desktop`, **Agent desktop**) with pin and fullscreen capabilities, not a
product-specific panel route or a Hermes core patch.

Nothing runs until the user installs the extension and accepts its permissions.
This beta targets Debian 13 with systemd and an AXP 0.5 Mintbot host. Desktop
programs run as the agent service account, normally **root**. Chromium uses
`--no-sandbox`; this is trusted full-server access, **not** an isolation boundary
for untrusted websites. Other distros, particularly Ubuntu's snap Chromium,
need separate packaging/runtime validation.

## Architecture and private bindings

The host owns a single command service, `desktop-web`, launching
`bin/mintbot-desktop serve`. The foreground supervisor owns TigerVNC, Openbox,
Chromium and the aiohttp gateway; termination or child failure cleans up its
own process groups. No process is detached during install. The default session
uses:

| Component | Address / location |
|---|---|
| X11 | `:13`, authenticated Unix socket, X TCP disabled |
| TigerVNC | `127.0.0.1:5913`, VncAuth, IPv6 disabled |
| HTTP / WebSocket gateway | `http://127.0.0.1:6081` |
| Chromium CDP | `127.0.0.1:9223`, trusted local clients only |
| Credentials / isolated browser profile | `AXP_STATE_DIR`, directory 0700 |

The package refuses the known unrelated display `:12` and ports 5912/6080.
It also refuses occupied displays/ports and does not kill existing sessions.
A stale lock left by a crashed X server is not occupancy: Xvnc replaces it.
CDP has no authentication: a local process with access to this loopback port
can control the browser. VNC's eight-byte password is generated randomly and
never delivered to the browser; the gateway completes VncAuth on its behalf.
All HTTP paths (including health, static assets and WebSocket upgrades) require
the `Authorization: Bearer` token from `state:token`. The VNC secret, password
file, Xauthority, browser profile and token are not under the served web root.
Only distro noVNC static files and the small packaged entry point are served.

### Generic panel integration

The manifest names `desktop-web` and `/desktop/`. A host mounts
`/axp-view/<opaque-ticket>/` at upstream `/desktop/`, checks the user's panel
capability, strips untrusted client authorization, then injects the private
service bearer token for **both HTTP and WebSocket** requests. The ticket is
not the upstream token. The browser only sees its host-issued scoped mount.
HTML uses `./app.js`; JavaScript imports `./novnc/core/rfb.js` and resolves
`./rfb` from its current URL, so nested mounts work without a fixed host prefix.

The iframe uses `sandbox="allow-scripts"` without `allow-same-origin`; its
Origin is `null`. The host must handle authenticated ES-module CORS and accept
the opaque origin only inside its own capability boundary. The gateway also
sets `Access-Control-Allow-Origin: *` after bearer authentication, never
`Access-Control-Allow-Credentials`. Do not expose the gateway publicly or put
upstream credentials in HTML, URLs, redirects, browser storage or logs.

## Permissions and containment — no bypass

The manifest deliberately declares `root:true`: APT installs system packages,
and the host's filesystem restrictions/`PrivateTmp` must be disabled so stock
agent GUI commands can access the same X socket and Xauthority. This gives
full filesystem authority, including agent workspaces and secrets; logical
filesystem scopes are disclosure, **not isolation**, for this root package.

The manifest declares `network_egress: ["*"]` — egress to any host. This is
a desktop with a web browser: the user and the agent browse any website from
it, which a list of named hosts could never say. The AXP host cannot filter
that by name, so it runs no egress proxy for this extension, applies no
address filter to its unit, and records the network half as *not enforced*;
the consent card says the same in plain words. Chromium goes direct (QUIC is
still disabled; a host that does set a proxy is honoured). The package does
not use a separate unmanaged unit, wildcard listener, startup failure, or
containment fallback: the open network is what the signed manifest declares
and what the user consents to. Loopback carries the lifecycle health check
and RFB.

Dependencies come from Debian's own mirrors (`deb.debian.org`). The install
hook does **not** run APT against the machine's configured sources: one
unreachable provider mirror or third-party repository (Hetzner, NodeSource, …)
would fail the whole `apt-get update`. Instead it writes a
private deb822 source list for the running Debian release under
`AXP_CACHE_DIR/apt/` and points both `apt-get update` and `apt-get install` at
it, with a private index directory so the host's own index files are never
pruned. The machine's source lists are not modified; downloads share the
system archive cache. Non-Debian hosts are refused before APT runs. Distro
noVNC and Python dependencies receive distro updates; they are not vendored or
byte-pinned in the source artifact.

## Lifecycle

* **install / upgrade:** idempotent APT dependency check/install, package and
  launcher copy, private credentials/state, packaged stock-Hermes skill. It
  does not start a service: host runtime activation follows successful install.
* **enable:** returns without starting a service. Activation hooks run before
  host runtime reconciliation; the host recreates and starts its managed unit
  after this hook succeeds, and reports activation failures.
* **disable:** stops that unit and all its child GUI processes, retaining state.
  Warn users about unsaved work before disabling.
* **health:** bearer-authenticated loopback HTTP probe, never via an HTTP proxy.
* **uninstall:** stop first; any stop error prevents deletion. Remove only the
  managed skill. Retain credentials/profile unless `AXP_PURGE=1`; the host owns
  removal of its unit and install prefix. Shared APT packages remain installed.

The host unit name follows its existing command-service mapping:
`mintbot-ext-mintbot.ai-mintbot-desktop-svc-desktop-web.service`. A missing unit
is tolerated on stop; a failed start/stop, dependency install, credential setup
or health probe returns failure. No live system installer is executed by tests;
APT and systemctl calls are injected fakes in lifecycle unit tests.

The install hook uses `AXP_PREFIX`, `AXP_STATE_DIR`, `AXP_HERMES_HOME` /
`HERMES_HOME`. Without host paths it uses `/opt/mintbot-desktop`,
`/var/lib/axp/mintbot.ai/mintbot-desktop`, and the current user's `.hermes`.
No standalone unit is installed: outside AXP, use the foreground runner below.
The installed skill records exact launcher/state locations. `exec -- COMMAND`
sets DISPLAY, XAUTHORITY and an isolated HOME; it shares the actual panel
session. The existing Hermes browser tool does not magically attach to it.

## Agent tooling

`mintbot-desktop browse ACTION` drives the session's Chromium through its
DevTools endpoint with Playwright: `pages`, `open`, `snapshot` (accessibility
tree, optionally filtered by `--roles`), `text`, `click`, `fill`, `type`,
`press`, `eval`, `wait`, `screenshot` (whole tab or one `--selector`), `run`
(a JSON list of steps over one connection) and `status`. It attaches per
command and detaches afterwards; the browser the user watches stays the same
one. Because each attachment lists the tabs in an arbitrary order, the tab the
last `open` navigated is recorded by its CDP target id in `state:browse-tab`
and stays the default for the commands that follow; `--page` overrides it for
one command, and errors are prefixed with the tab's title and URL. The
install hook creates `PREFIX/venv` with the Playwright package (no browser
download; `MINTBOT_DESKTOP_VENV` overrides the location) and the launcher
re-executes `browse` under it. APT also
installs `wmctrl`, `xclip`, `maim` and `at-spi2-core` for windows, clipboard,
screenshots and Hermes's optional `computer_use` toolset; the skill explains
when to use which.

## Development and isolated real GUI smoke

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
```

Tests exercise genuine loopback HTTP/WebSocket transport and VncAuth byte
exchange, credential privacy, relative assets, deterministic artifacts,
lifecycle failure semantics, and a temporary real GUI stack when distro
binaries are present. The host-contract test also runs this manifest through
the neighboring host checkout's real `axp_runtime.apply` path with systemctl
replaced by a recorder and every writable path redirected to scratch. It
It verifies the managed unit name, that no proxy or address filter is applied
and that the open network is recorded as such; this is not a live systemd
activation.
Set `MINTBOT_HOST_REPO` to select another host checkout.

The GUI test uses scratch state, an unused display in
193–219, ephemeral ports, and direct child processes. It runs no APT, systemctl,
or service installer. Missing GUI binaries are reported as a skipped smoke.

For manual host-proxy testing, first confirm these test addresses are unused,
then run in the foreground with a **fresh** state directory:

```sh
.venv/bin/python -m mintbot_desktop \
  --state "$TMPDIR/mintbot-desktop-e2e" --display 194 \
  --web-port 16081 --vnc-port 15913 --cdp-port 19223 serve
```

The token is in that directory's `token` file. Forward your temporary host mount
to port 16081 and inject the token server-side. Never copy it into browser code.
In another terminal:

```sh
.venv/bin/python -m mintbot_desktop --state "$TMPDIR/mintbot-desktop-e2e" env
.venv/bin/python -m mintbot_desktop --state "$TMPDIR/mintbot-desktop-e2e" exec -- xdpyinfo
```

Ctrl-C/SIGTERM stops only the foreground runner's own children. It retains
scratch state for inspection; delete that test directory explicitly afterward.
Do not reuse a state directory with different display/port settings.

### Real runtime E2E still required before deployment

The foreground smoke is **not proof of AXP activation**. On a disposable
Debian 13 agent with the new host/protocol code and consent, verify:

1. Published publisher key authorization, signed archive download/digest,
   lifecycle APT under actual root sandbox and configured mirror/proxy rules.
2. Host command mapping creates the exact unit, keeps network enforcement, and
   does not enter a containment downgrade. All four GUI components stay ready.
3. A real authenticated panel session opens the nested opaque-origin iframe;
   noVNC modules load, VNC canvas paints, mouse/keyboard input changes the same
   session, pin/fullscreen work, revocation rejects HTTP **and** WebSockets.
4. Agent `exec` and a separately attached CDP client visibly affect that same
   desktop; no token, VNC password, Xauthority or browser profile appears in
   browser network responses, console, artifact, or logs.
5. Browser reaches an allowed site through host egress but a disallowed public
   site fails. Loopback listeners never bind a public interface.
6. Disable stops the entire cgroup without deleting data; enable restores it.
   Upgrade preserves state; uninstall retains state, explicit purge deletes it.
   Unrelated `:12` / 5912 / 6080 remain unchanged throughout.

## Local signed release

```sh
AXP_SIGNING_KEY=/secure/outside-repo/mintbot.ai-mintbot-desktop.key \
  scripts/release.sh 0.1.0
```

Requires the AXP CLI with spec 0.5 support (`AXP_CLI` can select that build).
The script stamps package versions, builds a deterministic timestamp-free
archive, fills hashes/freshness with `axp release`, signs using the external
key, validates and verifies against the scoped public key directory. It does
**not** commit, tag, push, publish, install, or enable anything. Private signing
keys are excluded from Git and the explicit runtime archive allowlist.
`dist/mintbot-desktop-0.1.0.tar.gz` excludes the signed manifest to avoid a digest
cycle. A future release must attach both that artifact and
`agent-extension.json`, and publish the authorized desktop public key at the
publisher's well-known key-directory endpoint before user installation.
