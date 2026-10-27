# mintbot Desktop

An opt-in shared graphical desktop packaged as the official
`mintbot.ai/mintbot-desktop` AXP 0.5 extension. It provides a generic panel view
(`desktop`, **Agent desktop**) with pin and fullscreen capabilities, not a
product-specific panel route or a Hermes core patch.

Nothing runs until the user installs the extension and accepts its permissions.
This beta targets Debian 13. An AXP 0.5 host can manage the desktop service and
panel integration; a hostless installation uses the foreground runner (no
systemd requirement). Desktop
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

A nonempty `AXP_HOST` selects **host-managed** mode: the host owns activation,
deactivation and prefix removal through the unchanged `component_map` command.
With `AXP_HOST` unset or empty, the hooks use **standalone** mode. Do not set it
in a hand-run installation: it is a host identifier, not a service-manager name.
Hosts that set it must implement the declared command-service mapping.

* **install / upgrade:** idempotent APT dependency check/install, package and
  launcher copy, private credentials/state, packaged stock-Hermes skill. This
  completes installation without starting or detaching a process. Standalone
  mode prints the exact foreground start command; host activation follows the hook.
* **enable:** host mode returns for host activation; standalone mode runs the
  installed launcher with `serve` in the foreground, inheriting the terminal.
  It blocks until stopped and propagates failure. Prefer the launcher itself
  when configuring a service manager.
* **disable:** never calls a service manager or sends signals. The host must
  stop its service first (AXP §6.5); a standalone operator stops their foreground
  runner or their own service first. An active session lock returns failure
  with that instruction, without changing files. A stale unlocked file is safe.
* **health:** bearer-authenticated loopback HTTP probe, never via an HTTP proxy.
* **uninstall:** refuses an active session before cleanup. Removes only the
  owned Hermes skill; standalone mode also removes its dedicated install prefix.
  Host mode leaves prefix and service removal to the host. Credentials/profile
  survive unless `AXP_PURGE=1`. Shared APT packages and download cache remain.

Cleanup requires an intact `state/install.json` matching the prefix and Hermes
home. Root/shared, relative, symlinked and overlapping prefix/state paths are
refused before cleanup; a prefix must not contain the artifact source, Hermes
home or cache. Standalone install refuses to adopt a nonempty unowned prefix.
Use a dedicated directory, never a shared `/usr/local` or a home directory;
anything later placed in that owned prefix is removed on standalone uninstall.
Retained state permits reinstall and a later explicit purge. A missing or
mismatched ownership record requires operator inspection, not forced deletion.
These checks protect against configuration mistakes, not hostile concurrent root
filesystem changes. Stop automatic restarts and avoid concurrent lifecycle runs.

The install hook uses `AXP_PREFIX`, `AXP_STATE_DIR`, `AXP_CACHE_DIR`, and
`AXP_HERMES_HOME` / `HERMES_HOME`. Without overrides it uses
`/opt/mintbot-desktop`, `/var/lib/axp/mintbot.ai/mintbot-desktop`,
`/var/cache/mintbot-desktop`, and the current user's `.hermes`. Installing system
GUI dependencies requires root; no AXP host or running systemd is needed.
The installed skill records exact launcher/state locations. `exec -- COMMAND`
sets DISPLAY, XAUTHORITY and an isolated HOME; it shares the actual desktop
session. The existing Hermes browser tool does not magically attach to it.

### Standalone foreground or operator-owned service

From a reviewed, unpacked artifact on Debian 13, as root (no AXP variables needed):

```sh
./install.sh
/opt/mintbot-desktop/bin/mintbot-desktop serve
# Ctrl-C stops this runner and its children, preserving state.
# Alternatively: ./enable.sh (same foreground workflow, not a daemon installer).
```

Keep the unpacked artifact for its lifecycle scripts. For custom locations,
export the same absolute `AXP_PREFIX`, `AXP_STATE_DIR`, `AXP_CACHE_DIR` and
`AXP_HERMES_HOME` on every hook invocation. In a second terminal, `./health.sh`
checks the running gateway; the installed launcher also provides `env`, `exec`
and `browse` as described below.

For unattended operation, configure **your chosen** supervisor to run
`/opt/mintbot-desktop/bin/mintbot-desktop serve` as its foreground command,
with the same account/environment and optional restart-on-failure policy. Send
SIGTERM and wait for exit when stopping; the runner cleans up its child process
groups. Disable that supervisor's automatic restarts before uninstall. The
package installs no unit, chooses no unit name, and never stops Mintbot or other
host services. Do not run the host-managed service and a standalone runner
against the same state concurrently.

After stopping the runner/service, from the retained artifact:

```sh
./disable.sh                  # verifies stopped; preserves files and data
./uninstall.sh                # removes owned prefix/skill; retains state
AXP_PURGE=1 ./uninstall.sh     # optionally delete retained credentials/profile
```

Standalone installation bypasses AXP signature verification, consent, pinning,
freshness and host containment. It does not provide the host's panel/proxy.
The gateway remains authenticated and loopback-only: browser access needs your
own trusted HTTP/WebSocket proxy injecting the bearer token server-side, with
the authorization and origin checks described above. Never expose CDP or embed
the token in a URL or browser code.

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

## Releasing

mintbot Desktop follows the mintbot platform's AXP package flow, written up in
the mintbot repository (`developer_tools/agent_skills/axp_package_release.md`).

**Dev builds publish themselves.** `main` is the dev channel:
`.github/workflows/dev-build.yml` runs `scripts/release-dev.sh` on every push
to `main` (and right after each release). It publishes the commit as
`<next patch>-dev.<UTC timestamp>` on the `dev` channel, a GitHub pre-release,
and keeps only the newest two dev builds. Dev builds are signed by a separate
dev key (repository secret `AXP_DEV_SIGNING_KEY`,
`mintbot.ai-mintbot-desktop-dev-2026`). Only `agent-extension-keys.dev.json`,
the key directory mintbot.dev serves to DEV agents, lists it; mintbot.ai never
does, so a LIVE agent refuses every dev build. An agent pins the key of its
first install: one that installed a dev build updates to every later dev
build, one that installed a release never takes a dev build. By hand, with the
dev key:

```sh
AXP_SIGNING_KEY=/path/to/mintbot.ai-mintbot-desktop-dev.key scripts/release-dev.sh [<commit>]
```

**DEV tests the dev builds.** Beaver's "AXP package tests" (CatOffice on DEV)
install the second-newest dev build and upgrade it to the newest, through the
real panel mount, WebSocket/RFB framebuffer and noVNC canvas.

**A release promotes a tested dev build.** A release on the package's own
channel (`beta`, what LIVE agents install) names the dev build it promotes:

```sh
AXP_PROMOTE_FROM=0.1.10-dev.20261027120000 \
AXP_SIGNING_KEY=/secure/outside-repo/mintbot.ai-mintbot-desktop.key \
  scripts/release.sh 0.1.10
```

`scripts/release.sh` refuses without `AXP_PROMOTE_FROM` (a dev build of the
version being cut), refuses a checkout with uncommitted changes, and first
runs the mintbot repository's `developer_tools/axp_promote_check.py`
(`MINTBOT_REPO`, default `../mintbot`): the dev build must exist, the
checkout must be its commit and a green Beaver package journey must have run
on it. Then it stamps package versions, builds a deterministic timestamp-free
archive, fills hashes/freshness with `axp release`, signs using the external
key, validates and verifies against the scoped public key directory. It does
**not** commit, tag, push, publish, install or enable anything; the `git` and
`gh release create` steps it prints do, attaching both the artifact and
`agent-extension.json`. A release is never a GitHub pre-release: it becomes
the repository's "latest", which a bare repository URL installs. Requires the
AXP CLI with spec 0.5 support (`AXP_CLI` can select that build). Private
signing keys are excluded from Git and the explicit runtime archive allowlist;
`dist/mintbot-desktop-<version>.tar.gz` excludes the signed manifest to avoid
a digest cycle.
