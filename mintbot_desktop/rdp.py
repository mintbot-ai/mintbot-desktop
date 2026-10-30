"""A desktop that already exists and speaks RDP: a Wayland session's own remote desktop server.

A Wayland desktop has no X11 root window to scrape (attach.py), but GNOME
and KDE ship an RDP server that shows the logged-in user's own screen:
GNOME Remote Desktop (`gnome-remote-desktop`, driven by `grdctl`) and
KDE's krdp. The session then shows that server in the panel through the
gateway's RDCleanPath proxy (rdcleanpath.py) and IronRDP's browser client.

GNOME's server is set up here when it is not already: `grdctl rdp enable`
with credentials of the package's own, the view-only switch off, and the
user's `gnome-remote-desktop.service` started. An owner who had Desktop
Sharing on already keeps their settings; the session reads the credentials
they chose (`grdctl status --show-credentials`) and warns when they left it
view-only. Another server (krdp, or GNOME configured by hand) is named
outright with `--rdp HOST:PORT` and the credentials in the environment.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import socket
import subprocess
import time

DEFAULT_PORT = 3389
USERNAME = 'mintbot'
USERNAME_VARIABLE = 'MINTBOT_DESKTOP_RDP_USERNAME'
PASSWORD_VARIABLE = 'MINTBOT_DESKTOP_RDP_PASSWORD'
SERVICE = 'gnome-remote-desktop.service'
LISTEN_TIMEOUT = 15.0


@dataclass(frozen=True)
class Target:
    """The RDP server the session shows and how to log in to it.

    `fingerprint` is the SHA-256 (hex) of the certificate the server shows,
    when known; the proxy refuses another. `source` says where the target
    came from, for the log.
    """
    host: str
    port: int
    username: str
    password: str
    fingerprint: str | None = None
    source: str = 'given'

    @property
    def destination(self):
        return f'{self.host}:{self.port}'


def given(address, environ=None):
    """The target named by `--rdp HOST:PORT`, with the credentials from the environment."""
    environ = os.environ if environ is None else environ
    host, sep, port = address.rpartition(':')
    if not sep or not host or not port.isdigit():
        raise ValueError(f'--rdp wants HOST:PORT, not {address!r}')
    host = host.strip('[]')
    return Target(host, int(port), environ.get(USERNAME_VARIABLE, ''), environ.get(PASSWORD_VARIABLE, ''), None, 'given')


def parse_status(text):
    """grdctl's status output as {'RDP': {'Status': 'enabled', ...}, 'VNC': {...}}.

    Sections start at the left margin (``RDP:``); their lines are indented
    ``Key: value`` pairs. Keys grdctl may add later are kept as they come.
    """
    sections, current = {}, None
    for line in text.splitlines():
        if not line.strip():
            continue
        if not line[0].isspace():
            current = sections.setdefault(line.strip().rstrip(':'), {})
            continue
        key, sep, value = line.strip().partition(':')
        if sep and current is not None:
            current[key.strip()] = value.strip()
    return sections


def fingerprint_of(status):
    """The SHA-256 fingerprint grdctl prints, as bare lowercase hex; None when it prints none or another form."""
    digits = status.get('TLS fingerprint', '').replace(':', '').strip().lower()
    return digits if len(digits) == 64 and all(c in '0123456789abcdef' for c in digits) else None


def listening(host, port, timeout=1.0):
    """True when the server accepts a connection (one it logs: for the start, not for watching)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


LISTEN_STATE = '0A'


def port_listened(port, proc=Path('/proc/net')):
    """True while some socket on this computer listens on the TCP port.

    Read from the kernel's socket tables, not by connecting: the owner's
    RDP server logs every connection that brings no RDP, and a watch that
    connects every few seconds would fill its log with errors. Without
    the tables (not Linux) the answer is None: unknown.
    """
    found = False
    for name in ('tcp', 'tcp6'):
        try:
            lines = (proc / name).read_text().splitlines()[1:]
        except OSError:
            continue
        found = True
        for line in lines:
            fields = line.split()
            if len(fields) > 3 and fields[3] == LISTEN_STATE and int(fields[1].rpartition(':')[2], 16) == port:
                return True
    return False if found else None


def wait_until_listening(host, port, timeout=LISTEN_TIMEOUT, sleep=time.sleep, clock=time.monotonic):
    deadline = clock() + timeout
    while not listening(host, port):
        if clock() >= deadline:
            return False
        sleep(.25)
    return True


class Gnome:
    """GNOME Remote Desktop on this computer, through grdctl in the user's session."""

    def __init__(self, environment, *, run=subprocess.run, log=lambda message: None, sleep=time.sleep, clock=time.monotonic,
                 password=lambda: secrets.token_hex(12)):
        # grdctl needs the session bus (gsettings, the keyring); a user
        # service of the same user shares it at the user runtime directory.
        self.env = dict(os.environ)
        self.env.update(environment)
        runtime = self.env.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}'
        self.env.setdefault('DBUS_SESSION_BUS_ADDRESS', f'unix:path={runtime}/bus')
        self.run, self.log, self.sleep, self.clock, self.password = run, log, sleep, clock, password

    def grdctl(self, *args):
        try:
            result = self.run(['grdctl', *args], env=self.env, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(f'grdctl {" ".join(args)} failed: {error}') from error
        if result.returncode:
            raise RuntimeError(f'grdctl {" ".join(args)} failed: {(result.stderr or result.stdout).strip() or result.returncode}')
        return result.stdout

    def status(self):
        return parse_status(self.grdctl('status', '--show-credentials')).get('RDP', {})

    def start_service(self):
        """The user's gnome-remote-desktop.service, running now and at every login."""
        for args in (['start', SERVICE], ['enable', SERVICE]):
            result = self.run(['systemctl', '--user', *args], env=self.env, capture_output=True, text=True, timeout=20)
            if result.returncode:
                self.log(f'systemctl --user {" ".join(args)}: {(result.stderr or result.stdout).strip()}')

    def target(self):
        """The server, sharing the owner's screen with credentials this session can use; RuntimeError says why not."""
        status = self.status()
        if status.get('Status') != 'enabled' or not status.get('Username') or not status.get('Password'):
            self.log('turning GNOME Remote Desktop sharing on for the panel')
            self.grdctl('rdp', 'set-credentials', USERNAME, self.password())
            self.grdctl('rdp', 'disable-view-only')
            self.grdctl('rdp', 'enable')
            status = self.status()
            if status.get('Status') != 'enabled' or not status.get('Username') or not status.get('Password'):
                raise RuntimeError('GNOME Remote Desktop did not take the sharing settings')
        elif status.get('View-only', 'no').lower() in ('yes', 'true'):
            self.log('GNOME Remote Desktop is view-only: the panel shows the screen but cannot operate it '
                     '(grdctl rdp disable-view-only changes that)')
        port = int(status['Port']) if status.get('Port', '').isdigit() else DEFAULT_PORT
        if not listening('127.0.0.1', port):
            self.start_service()
            if not wait_until_listening('127.0.0.1', port, sleep=self.sleep, clock=self.clock):
                raise RuntimeError(f'GNOME Remote Desktop is enabled but nothing listens on port {port}')
        return Target('127.0.0.1', port, status['Username'], status['Password'], fingerprint_of(status), 'gnome-remote-desktop')
