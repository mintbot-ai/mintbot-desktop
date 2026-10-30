---
name: mintbot-desktop
description: Use when controlling the shared mintbot Desktop GUI or its browser.
---

<!-- Managed by mintbot.ai/mintbot-desktop -->

# mintbot Desktop

The installed launcher is `@LAUNCHER@`; private state is `@STATE@`.
The panel and the agent share one X session: the user watches everything
you do there live, and you see what they did. On a server that is the
package's own desktop, normally `:13`. On the user's own Linux computer
(`@LAUNCHER@ env` shows a display such as `:0`, and `WAYLAND_DISPLAY` on a
Wayland desktop, whose screen the panel gets through the desktop's own RDP
server) it is **their real screen**:
the windows there are theirs, the pointer and keyboard you drive are theirs,
and your programs appear on their desktop. Do not close, move or type into
windows you did not open, keep what you open small and tidy, and prefer
`browse` and the programs' own interfaces, which disturb nothing on screen.
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
every command; it never closes the browser. On the package's own desktop the
browser starts with the desktop and is not restarted when the user closes
it; on the user's own computer nothing pops up on their screen by itself.
Either way `@LAUNCHER@ browser` opens it (the desktop icon and the
right-click menu do the same on the package's own desktop), and `browse`
tells you when that is needed.

```sh
@LAUNCHER@ browse status                        # CDP reachable? driver installed?
@LAUNCHER@ browse pages                         # open tabs: targetId, title, url, current
@LAUNCHER@ browse open https://example.com      # navigate your tab; prints its targetId
@LAUNCHER@ browse open --new https://example.com
@LAUNCHER@ browse snapshot                      # accessibility tree: roles, names, text
@LAUNCHER@ browse snapshot main --roles button,link,textbox   # only what you can act on
@LAUNCHER@ browse text main                     # inner text of a selector (default: body)
@LAUNCHER@ browse click 'text=Log in'           # first match of a Playwright selector
@LAUNCHER@ browse fill 'input[name=q]' 'query'
@LAUNCHER@ browse press Enter
@LAUNCHER@ browse wait 'text=Results'
@LAUNCHER@ browse eval 'document.title'
@LAUNCHER@ browse screenshot /path/to/private/shot.png
@LAUNCHER@ browse screenshot /path/to/private/form.png --selector main   # one element, small
```

**Your tab.** The tab the last `open` navigated stays the target of every
following command until you `open` again, even when the user switches tabs
meanwhile. Start a task with `open --new` rather than taking over a tab the
user is working in. `--page TAB` before the action picks another tab for that
one command: a target id from `pages`, or a substring of the URL or title.
If your tab was closed, commands fall back to the tab the user is looking at.
Every error starts with `page: <title> <url>`, so a timeout tells you which
tab it was waiting in.

**Several steps in one go.** Each command attaches to the browser anew (about
a second); `run` performs a whole list of steps over one connection and
prints their results as a JSON array:

```sh
printf '%s' '[{"fill": ["input[name=q]", "query"]}, {"press": "Enter"},
  {"wait": "text=Results"}, {"text": "main"}]' | @LAUNCHER@ browse run
@LAUNCHER@ browse run /path/to/steps.json
```

A step is `{"action": arguments}` with the same arguments as the command line
(a list, one string, or null for none; options like `--timeout` go in the
list); `"page"` next to the action picks a tab for that step. The first failing
step stops the run and its error names the step index; the results of the
completed steps are still printed.

Selectors are Playwright's: CSS, `text=`, `role=button[name="Save"]`,
`xpath=`. Read `snapshot` first (with `--roles` on a large page: the full
tree can be far longer than your terminal shows), then act on what it names.

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
