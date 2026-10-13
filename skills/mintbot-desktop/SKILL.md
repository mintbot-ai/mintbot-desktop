---
name: mintbot-desktop
description: Use when controlling the shared Mintbot Desktop GUI.
---

<!-- Managed by mintbot.ai/mintbot-desktop -->

# Mintbot Desktop

The installed launcher is `@LAUNCHER@`; private state is `@STATE@`.
The panel and the agent share one TigerVNC X session, normally `:13`.
Never operate the unrelated `:12` session or ports 5912/6080.

Use terminal tools (stock Hermes; no plugin or core patch required):

```sh
@LAUNCHER@ env
@LAUNCHER@ exec -- xdpyinfo
@LAUNCHER@ exec -- xdotool getactivewindow getwindowname
@LAUNCHER@ exec -- xdotool mousemove 400 300 click 1
@LAUNCHER@ exec -- xdotool type --clearmodifiers -- 'Example text'
@LAUNCHER@ exec -- xdotool key --clearmodifiers ctrl+l
@LAUNCHER@ exec -- scrot /path/to/private/screenshot.png
```

Create screenshots under the agent scratch directory, load them with the
image tool before making visual claims, and use share_file to deliver them.
Do not send password files, the token, Xauthority, or the Chromium profile.
Use the launcher for every GUI command: it sets DISPLAY, XAUTHORITY and HOME
from the session's state. It does not print secrets or start another session.

Chromium is headed on this display with its own state/chromium profile.
Its CDP endpoint is **127.0.0.1:9223**, configurable only in foreground tests.
Read `/json/version` or `/json/list` using an HTTP client with proxies disabled;
connect to the returned debugger WebSocket using your own CDP client. Attach
Playwright with `chromium.connect_over_cdp('http://127.0.0.1:9223')` only if
Playwright is actually installed. Close the CDP client connection, not the
shared browser. The existing Hermes browser tool does **not** automatically
join this browser/session. No browser automation library is installed here.

Browser egress is restricted by the AXP host's signed allowlist. Chromium uses
the host proxy; arbitrary websites may fail. Do not bypass that containment.
The session normally runs as root with Chromium --no-sandbox: treat the GUI
as trusted full-server access, not as isolation for untrusted web content.
Ask before entering credentials, making purchases, sending messages, or
closing another user's work. Prefer a fresh test page to a live user tab.

Enable/disable through the extension host. Disable closes GUI programs and
stops the whole session but retains profile and credentials; warn about
unsaved work. Uninstall retains state unless AXP_PURGE=1 was explicitly chosen.
