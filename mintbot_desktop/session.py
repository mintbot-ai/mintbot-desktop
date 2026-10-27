"""Shared session configuration and foreground child ownership."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import shlex
import signal
import socket
import subprocess
import sys


@dataclass(frozen=True)
class Config:
    state: Path
    display: int = 13
    web_port: int = 6081
    vnc_port: int = 5913
    cdp_port: int = 9223

    def __post_init__(self):
        object.__setattr__(self, 'state', Path(self.state).resolve())
        ports = (self.web_port, self.vnc_port, self.cdp_port)
        if not 13 <= self.display <= 999 or any(p in (5912, 6080) or not 1024 <= p <= 65535 for p in ports) or len(set(ports)) != 3:
            raise ValueError('Invalid or reserved desktop display/ports')


def vnc_command(cfg):
    return ['Xtigervnc', f':{cfg.display}', '-geometry', '1440x900', '-depth', '24',
            '-localhost', 'yes', '-interface', '127.0.0.1', '-UseIPv6', 'no', '-rfbport', str(cfg.vnc_port),
            '-SecurityTypes', 'VncAuth', '-PasswordFile', str(cfg.state / 'vnc-password'),
            '-auth', str(cfg.state / 'Xauthority'), '-AlwaysShared', '-nolisten', 'tcp']


def browser_command(cfg, env):
    args = ['chromium', '--no-sandbox', '--disable-dev-shm-usage', '--disable-quic',
            '--no-first-run', '--no-default-browser-check', '--disable-background-networking',
            '--password-store=basic', '--remote-debugging-address=127.0.0.1',
            f'--remote-debugging-port={cfg.cdp_port}', f'--user-data-dir={cfg.state / "chromium"}']
    proxy = env.get('https_proxy') or env.get('HTTPS_PROXY') or env.get('http_proxy')
    if proxy:
        args += [f'--proxy-server={proxy}']
    return args + ['about:blank']


# The browser's own icon, shipped by Debian's chromium package; idesk and the
# openbox menu point at it.
BROWSER_ICON = '/usr/share/icons/hicolor/64x64/apps/chromium.png'


def browser_launcher(cfg):
    """The command the desktop icon and the openbox menu run to (re)open the browser.

    It names the interpreter and the package's own directory outright: idesk
    and openbox run it from inside the session, where the launcher script's
    environment is not a given. Chromium itself turns a second start on the
    same profile into a new window of the running instance.
    """
    package_root = Path(__file__).resolve().parents[1]
    return ['env', f'PYTHONPATH={package_root}', sys.executable, '-m', 'mintbot_desktop',
            '--state', str(cfg.state), 'browser']


IDESK_CONFIG = """table Config
  FontName: sans
  FontSize: 10
  FontColor: #ffffff
  Locked: true
  Transparency: 150
  Shadow: true
  ShadowColor: #000000
  ShadowX: 1
  ShadowY: 1
  Bold: false
  ClickDelay: 300
  IconSnap: true
  SnapWidth: 55
  SnapHeight: 100
  SnapOrigin: TopLeft
  SnapShadow: true
  SnapShadowTrans: 200
  CaptionOnHover: false
  Background.Delay: 0
  Background.Source: None
  Background.File: None
  Background.Mode: Center
  Background.Color: #2b3440
end
table Actions
  Lock: control right doubleClk
  Reload: middle doubleClk
  Drag: left hold
  EndDrag: left singleClk
  Execute[0]: left doubleClk
  Execute[1]: right doubleClk
end
"""

IDESK_ICON = """table Icon
  Caption: Browser
  Command: {command}
  Icon: {icon}
  Width: 64
  Height: 64
  X: 40
  Y: 40
end
"""

OPENBOX_MENU = """<?xml version="1.0" encoding="UTF-8"?>
<openbox_menu xmlns="http://openbox.org/3.4/menu">
  <menu id="root-menu" label="mintbot Desktop">
    <item label="Browser">
      <action name="Execute"><execute>{command}</execute></action>
    </item>
  </menu>
</openbox_menu>
"""


def write_desktop(cfg):
    """The desktop icon (idesk, read from HOME) and the openbox root menu (read
    from XDG_CONFIG_HOME): both open the browser the user closed. Rewritten
    on every start, so a moved state directory or interpreter is followed."""
    command = shlex.join(browser_launcher(cfg))
    icons = cfg.state / '.idesktop'
    icons.mkdir(mode=0o700, exist_ok=True)
    (cfg.state / '.ideskrc').write_text(IDESK_CONFIG)
    (icons / 'browser.lnk').write_text(IDESK_ICON.format(command=command, icon=BROWSER_ICON))
    menu = cfg.state / 'config' / 'openbox'
    menu.mkdir(parents=True, exist_ok=True)
    (menu / 'menu.xml').write_text(OPENBOX_MENU.format(command=command.replace('&', '&amp;').replace('<', '&lt;')))


VNC_SECRET_ALPHABET = 'abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'


def vnc_secret():
    """VNC passwords are capped at 8 characters, so spend them on a wide alphabet (~47 bits), not hex (32)."""
    return ''.join(secrets.choice(VNC_SECRET_ALPHABET) for _ in range(8))


def port_available(port, host='127.0.0.1'):
    """True when nothing listens on the port.

    SO_REUSEADDR makes the probe ignore the TIME_WAIT connections a previous
    session left behind, which would otherwise block a restart for a minute.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def private_write(path, data):
    """Create once without following symlinks; never overwrite credentials."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if path.is_symlink() or not path.is_file():
            raise ValueError('Unsafe credential path') from None
        os.chmod(path, 0o600)
        return
    with os.fdopen(fd, 'wb') as handle:
        handle.write(data)


def prepare(cfg):
    cfg.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(cfg.state, 0o700)
    private_write(cfg.state / 'token', secrets.token_urlsafe(48).encode())
    private_write(cfg.state / 'vnc-secret', vnc_secret().encode())
    for name in ('vnc-password', 'Xauthority'):
        path = cfg.state / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError('Unsafe credential path')
        if path.exists():
            path.chmod(0o600)
    if not (cfg.state / 'vnc-password').exists():
        secret = (cfg.state / 'vnc-secret').read_bytes()
        result = subprocess.run(['tigervncpasswd', '-f'], input=secret + b'\n', capture_output=True, check=True)
        private_write(cfg.state / 'vnc-password', result.stdout)
    if not (cfg.state / 'Xauthority').exists() or (cfg.state / 'Xauthority').stat().st_size == 0:
        private_write(cfg.state / 'Xauthority', b'')
        subprocess.run(['xauth', '-f', str(cfg.state / 'Xauthority')],
                       input=f'add :{cfg.display} MIT-MAGIC-COOKIE-1 {secrets.token_hex(16)}\n',
                       text=True, capture_output=True, check=True)
    settings = {'display': cfg.display, 'web_port': cfg.web_port, 'vnc_port': cfg.vnc_port, 'cdp_port': cfg.cdp_port}
    private_write(cfg.state / 'session.json', (json.dumps(settings, sort_keys=True) + '\n').encode())
    if json.loads((cfg.state / 'session.json').read_text()) != settings:
        raise ValueError('State belongs to another display/port configuration; use a fresh state directory')


def gui_environment(cfg):
    env = dict(os.environ)
    env.update(DISPLAY=f':{cfg.display}', XAUTHORITY=str(cfg.state / 'Xauthority'), HOME=str(cfg.state),
               XDG_CONFIG_HOME=str(cfg.state / 'config'), XDG_CACHE_HOME=str(cfg.state / 'cache'))
    return env


def display_in_use(display, tmp=Path('/tmp')):
    """True while another X server owns the display.

    An unclean exit leaves the lock and socket behind, and Xvnc replaces a
    lock whose PID is gone (rebinding the socket) itself. Only a live or
    unreadable lock means another session owns the display.
    """
    try:
        pid = int((tmp / f'.X{display}-lock').read_text().strip())
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


class Children:
    """Only terminate process groups created by this instance."""
    def __init__(self):
        self.processes = []

    def __enter__(self):
        return self

    def start(self, argv, **kwargs):
        self._prune_finished()
        process = subprocess.Popen(argv, start_new_session=True, **kwargs)
        self.processes.append(process)
        return process

    def _prune_finished(self):
        """Drop exited children so repeated restarts do not grow the list without bound."""
        self.processes = [p for p in self.processes if p.poll() is None]

    def __exit__(self, *exc):
        self._signal_all(signal.SIGTERM)
        for process in reversed(self.processes):
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._signal(process, signal.SIGKILL)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        self.processes.clear()

    def _signal_all(self, signum):
        for process in reversed(self.processes):
            self._signal(process, signum)

    @staticmethod
    def _signal(process, signum):
        """Signal the child's process group; a group that already vanished is not an error."""
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass
