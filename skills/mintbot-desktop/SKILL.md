---
name: mintbot-desktop
description: Use when controlling the shared mintbot Desktop GUI or its browser.
---

<!-- Managed by mintbot.ai/mintbot-desktop -->

# mintbot Desktop

The installed launcher is `@LAUNCHER@`; private state is `@STATE@`.
The panel and the agent share one TigerVNC X session, normally `:13`: the
user watches everything you do there live, and you see what they did.
Never operate the unrelated `:12` session or ports 5912/6080.

Pick the cheapest channel that does the job, in this order:

1. The program's own interface (CLI, file, API): fastest and most reliable.
   Use the desktop only to show the result.
2. The browser through `browse`: Playwright over DevTools, working on page
   elements (roles, names, selectors), not pixels.
3. Window, keyboard and clipboard helpers (wmctrl, xdotool, xclip) for other
   programs.
4. Screenshot plus coordinates (maim/scrot and xdotool) as the last resort.

## Browser: `@LAUNCHER@ browse`

Chromium on this display is the same one the user sees in the panel. `browse`
attaches to it over CDP (127.0.0.1:9223) with Playwright and detaches after
every command; it never starts or closes the browser.

```sh
@LAUNCHER@ browse status                        # CDP reachable? driver installed?
@LAUNCHER@ browse pages                         # open tabs: index, title, url
@LAUNCHER@ browse open https://example.com      # navigate the newest tab
@LAUNCHER@ browse open --new https://example.com
@LAUNCHER@ browse snapshot                      # accessibility tree: roles, names, text
@LAUNCHER@ browse text main                     # inner text of a selector (default: body)
@LAUNCHER@ browse click 'text=Log in'           # first match of a Playwright selector
@LAUNCHER@ browse fill 'input[name=q]' 'query'
@LAUNCHER@ browse press Enter
@LAUNCHER@ browse wait 'text=Results'
@LAUNCHER@ browse eval 'document.title'
@LAUNCHER@ browse screenshot /path/to/private/shot.png
```

`--page SUBSTRING` before the action picks the tab whose URL or title contains
it; the default is the newest tab. Prefer `open --new` over a tab the user is
working in. Selectors are Playwright's: CSS, `text=`, `role=button[name="Save"]`,
`xpath=`. Read `snapshot` first, then act on what it names.

If `status` says the driver is missing, upgrade or reinstall the extension;
its install hook creates the virtualenv under the install prefix.

## Other programs

```sh
@LAUNCHER@ exec -- wmctrl -l                                  # list windows
@LAUNCHER@ exec -- wmctrl -a LibreOffice                      # raise by title
@LAUNCHER@ exec -- wmctrl -r :ACTIVE: -e 0,0,0,1280,800       # move and resize
@LAUNCHER@ exec -- xdotool getactivewindow getwindowname
@LAUNCHER@ exec -- xdotool key --clearmodifiers ctrl+l
@LAUNCHER@ exec -- xdotool type --clearmodifiers -- 'Example text'
@LAUNCHER@ exec -- xdotool mousemove 400 300 click 1
@LAUNCHER@ exec -- sh -c 'printf %s "long text" | xclip -selection clipboard >/dev/null 2>&1'   # then ctrl+v
@LAUNCHER@ exec -- xclip -selection clipboard -o                                # read the clipboard
@LAUNCHER@ exec -- maim /path/to/private/shot.png                               # scrot works too
@LAUNCHER@ exec -- maim -g 800x600+100+50 /path/to/private/region.png
```

Paste long text through the clipboard instead of typing it; keep the output
redirect on `xclip`, it forks to serve the selection and would otherwise hold
the terminal open. Create screenshots
under the agent scratch directory, load them with the image tool before making
visual claims, and use share_file to deliver them. Do not send password files,
the token, Xauthority, or the Chromium profile.
Use the launcher for every GUI command: it sets DISPLAY, XAUTHORITY and HOME
from the session's state. It does not print secrets or start another session.

Programs with a scriptable interface are driven through it, not the GUI:
LibreOffice (`soffice --headless --convert-to pdf file.odt`), GIMP
(`gimp -i -b`), ImageMagick, ffmpeg. Install what is missing with apt from
the terminal; the desktop does not need a restart for it.

## Hermes computer_use (optional)

Hermes ships a `computer_use` toolset: on Linux it reads windows through
AT-SPI, lists a program's buttons and fields by name, screenshots, and clicks
by element. `at-spi2-core` is installed with the desktop. To try it, run
`hermes computer-use install`, enable the toolset with `hermes tools`, and give
the Hermes gateway the session's `DISPLAY` and `XAUTHORITY` (`@LAUNCHER@ env`):
the desktop does not change Hermes's own environment. `hermes computer-use
doctor` reports what works. For the browser, `browse` stays the better tool.

## Safety

The desktop's network is unfiltered: it is the user's browser, used the way
they would use it. The session normally runs as root with Chromium
--no-sandbox: treat the GUI as trusted full-server access, not as isolation
for untrusted web content. Ask before entering credentials, making purchases,
sending messages, or closing another user's work.

Enable/disable through the extension host. Disable closes GUI programs and
stops the whole session but retains profile and credentials; warn about
unsaved work. Uninstall retains state unless AXP_PURGE=1 was explicitly chosen.
