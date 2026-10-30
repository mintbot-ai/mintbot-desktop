"""A desktop that already exists: the computer's own X11 session.

On a server the package starts a desktop of its own (Xtigervnc on :13,
openbox, a browser; see __main__.serve). On a Linux computer whose owner is
logged in at the screen, the desktop to show is theirs: the panel then
displays, and controls, the very screen the owner is looking at. TigerVNC's
x0vncserver serves an existing X display over RFB with the same password
file the virtual desktop uses, so the gateway and the panel view stay as
they are. Debian and Ubuntu name that binary X0tigervnc; their x0vncserver
is a daemonising wrapper around it, so the binary is preferred.

A Wayland session has no X root window to read (its DISPLAY is XWayland,
whose root window shows nothing); it is not attached to, and the virtual
desktop is started instead. The decision is taken when the session starts:
a desktop that appears or goes away later is picked up by the next start.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess

MODES = ('auto', 'attach', 'virtual')
SCRAPERS = ('X0tigervnc', 'x0vncserver')
PROBE_TIMEOUT = 3.0


@dataclass(frozen=True)
class Attachment:
    """An existing X display and the authority file that opens it (None: the server asks for none)."""
    display: str
    xauthority: str | None = None


def scraper(which=shutil.which):
    """The installed x0vncserver binary, or None."""
    return next((name for name in SCRAPERS if which(name)), None)


def scrape_command(cfg, attachment, program):
    """Serve the existing display over RFB exactly as Xtigervnc serves the virtual one: loopback, VncAuth, shared."""
    return [program, '-display', attachment.display, '-rfbport', str(cfg.vnc_port),
            '-localhost', 'yes', '-interface', '127.0.0.1', '-UseIPv6', 'no',
            '-SecurityTypes', 'VncAuth', '-PasswordFile', str(cfg.state / 'vnc-password'), '-AlwaysShared']


def display_answers(display, xauthority, run=subprocess.run, environ=None):
    """True when the X server behind `display` admits a client with this authority file."""
    env = dict(os.environ if environ is None else environ)
    env['DISPLAY'] = display
    env.pop('XAUTHORITY', None)
    if xauthority:
        env['XAUTHORITY'] = xauthority
    try:
        return run(['xdpyinfo'], env=env, capture_output=True, timeout=PROBE_TIMEOUT).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _properties(text):
    return dict(line.split('=', 1) for line in text.splitlines() if '=' in line)


def user_manager_environment(run=subprocess.run):
    """The user's systemd manager environment: desktops import DISPLAY, XAUTHORITY and WAYLAND_DISPLAY into it at login."""
    try:
        result = run(['systemctl', '--user', 'show-environment'], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return {} if result.returncode else _properties(result.stdout)


def logind_sessions(run=subprocess.run, uid=None):
    """(type, display) of this user's logind sessions, the ones on their way out left aside."""
    uid = os.getuid() if uid is None else uid
    try:
        listing = run(['loginctl', 'list-sessions', '--no-legend'], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if listing.returncode:
        return []
    sessions = []
    for line in listing.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[1] != str(uid):
            continue
        try:
            shown = run(['loginctl', 'show-session', parts[0], '-p', 'Type', '-p', 'Display', '-p', 'State'],
                        capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if shown.returncode:
            continue
        props = _properties(shown.stdout)
        if props.get('State') != 'closing':
            sessions.append((props.get('Type', ''), props.get('Display', '')))
    return sessions


def find_display(*, display=None, exclude=(), run=subprocess.run, environ=None, uid=None, home=None, probe=None):
    """The X11 display of the desktop this user is logged in at: (Attachment, None), or (None, why not).

    Candidates come from this process's own environment, the user's systemd
    manager environment and logind's x11 sessions of this user, in that
    order; `display` names one outright instead. An environment of a
    Wayland session (WAYLAND_DISPLAY, XDG_SESSION_TYPE=wayland) names no
    display worth showing: its DISPLAY is XWayland, whose root window is
    empty. A candidate is chosen only once it answers xdpyinfo, with the
    authority file its environment names, GDM's, the user's own, or none.
    """
    environ = dict(os.environ if environ is None else environ)
    uid = os.getuid() if uid is None else uid
    home = Path.home() if home is None else Path(home)
    if probe is None:
        probe = lambda candidate, xauthority: display_answers(candidate, xauthority, run, environ)
    wayland = False
    candidates = []
    if display:
        candidates.append((display, environ.get('XAUTHORITY')))
    else:
        for env in (environ, user_manager_environment(run)):
            if env.get('WAYLAND_DISPLAY') or env.get('XDG_SESSION_TYPE') == 'wayland':
                wayland = True
            elif env.get('DISPLAY'):
                candidates.append((env['DISPLAY'], env.get('XAUTHORITY')))
        for kind, found in logind_sessions(run, uid):
            if kind == 'wayland':
                wayland = True
            elif kind == 'x11' and found:
                candidates.append((found, None))
    runtime = environ.get('XDG_RUNTIME_DIR') or f'/run/user/{uid}'
    authorities = [environ.get('XAUTHORITY'), f'{runtime}/gdm/Xauthority', str(home / '.Xauthority')]
    tried = []
    for candidate, xauthority in candidates:
        if candidate in exclude or candidate in tried:
            continue
        tried.append(candidate)
        for authority in dict.fromkeys([xauthority, *authorities, None]):
            if authority is not None and not Path(authority).is_file():
                continue
            if probe(candidate, authority):
                return Attachment(candidate, authority), None
    if tried:
        return None, f'the display {", ".join(tried)} did not admit this user'
    if wayland:
        return None, 'the desktop session is Wayland, which has no X11 screen to show'
    return None, 'this user is logged in at no X11 desktop'


def choose(cfg, mode='auto', display=None, *, find=find_display, which=shutil.which, log=lambda message: None):
    """The desktop the session shows: an Attachment to an existing one, or None for the virtual desktop.

    `auto` takes an existing desktop when there is one to show and falls
    back to the virtual desktop, saying why; `attach` insists on one;
    `virtual` never looks.
    """
    if mode not in MODES:
        raise ValueError(f'Unknown desktop mode {mode!r}; one of {", ".join(MODES)}')
    if mode == 'virtual':
        return None
    if scraper(which) is None:
        reason = 'x0vncserver is not installed (Debian: tigervnc-scraping-server)'
        if mode == 'attach':
            raise RuntimeError(f'Cannot show an existing desktop: {reason}')
        log(f'{reason}; starting the virtual desktop')
        return None
    attachment, reason = find(display=display, exclude={f':{cfg.display}'})
    if attachment is not None:
        return attachment
    if mode == 'attach':
        raise RuntimeError(f'No existing desktop to show: {reason}')
    log(f'no existing desktop to show: {reason}; starting the virtual desktop')
    return None
