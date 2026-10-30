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


def child_named(name, proc=Path('/proc')):
    """The pid of a child of this process whose command name is `name`, or None.

    /proc/<pid>/stat reads "pid (comm) state ppid ..."; the name is cut at its
    closing parenthesis, as it may hold spaces and parentheses of its own.
    """
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / 'stat').read_text()
        except OSError:
            continue
        head, _, rest = stat.rpartition(')')
        if head.partition('(')[2] == name and int(rest.split()[1]) == os.getpid():
            return int(entry.name)
    return None


def open_browser(cfg, run=subprocess.run, execve=os.execvpe, orphan=child_named):
    """(Re)open the session's Chromium: the desktop icon, the openbox menu and the agent run this.

    idesk runs an icon's command in its own process: its fork() result is
    compared before it is assigned (`pid = fork() != 0`), so the parent execs
    the command while the child goes on drawing the icons. Run from the icon,
    this is therefore the very idesk the supervisor watches, with the idesk
    that draws the icons as its orphan child. Becoming Chromium here handed
    the supervisor a browser as its icon layer: one that exits at once when a
    browser already runs (the new window goes to that one), whereupon the
    supervisor started another idesk, which refused to run beside the orphan,
    and after three refusals the whole session was given up on. With an
    orphan in tow, the browser starts detached instead, the orphan retires,
    and this process becomes idesk again.
    """
    env = gui_environment(cfg)
    command = browser_command(cfg, env)
    icons = orphan('idesk')
    if icons is None:
        execve(command[0], command, env)
        return
    run(['setsid', '--fork', *command], env=env)
    os.kill(icons, signal.SIGTERM)
    os.waitpid(icons, 0)
    execve('idesk', ['idesk'], env)


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
    on every start, so a moved state directory or interpreter is followed.

    idesk hands the icon's command to `sh -c`, from its own process (see
    open_browser); `exec` makes that process the launcher itself rather than
    a shell waiting for it, so the launcher can tell where it runs. openbox
    spawns the menu's command straight from its words, no shell."""
    command = shlex.join(browser_launcher(cfg))
    icons = cfg.state / '.idesktop'
    icons.mkdir(mode=0o700, exist_ok=True)
    (cfg.state / '.ideskrc').write_text(IDESK_CONFIG)
    (icons / 'browser.lnk').write_text(IDESK_ICON.format(command='exec ' + command, icon=BROWSER_ICON))
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


# Which display the running session shows when it is not the package's own
# (attach.py): `env`, `exec`, `browse` and `browser` run outside the session
# and read it from here. The virtual desktop leaves no record.
ATTACHMENT_FILE = 'attached.json'


def record_attachment(cfg, attachment):
    """Remember the existing display the session shows, or forget one (the virtual desktop)."""
    path = cfg.state / ATTACHMENT_FILE
    if attachment is None:
        path.unlink(missing_ok=True)
        return
    staging = path.with_suffix('.tmp')
    fd = os.open(staging, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump({'display': attachment.display, 'xauthority': attachment.xauthority}, handle)
        handle.write('\n')
    os.replace(staging, path)


def read_attachment(cfg):
    """{'display', 'xauthority'} of the existing display the session shows, or None."""
    try:
        data = json.loads((cfg.state / ATTACHMENT_FILE).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get('display'), str) or not data['display']:
        return None
    return {'display': data['display'], 'xauthority': data.get('xauthority') or None}


def gui_environment(cfg):
    env = dict(os.environ)
    env.update(DISPLAY=f':{cfg.display}', XAUTHORITY=str(cfg.state / 'Xauthority'), HOME=str(cfg.state),
               XDG_CONFIG_HOME=str(cfg.state / 'config'), XDG_CACHE_HOME=str(cfg.state / 'cache'))
    attached = read_attachment(cfg)
    if attached:
        # The computer's own desktop: its display and its authority file.
        # HOME and the XDG directories stay the session's own, so the
        # agent's programs keep their settings out of the owner's.
        env['DISPLAY'] = attached['display']
        del env['XAUTHORITY']
        if attached['xauthority']:
            env['XAUTHORITY'] = attached['xauthority']
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
            self._reap(process)
        self.processes.clear()

    def stop(self, process):
        """Terminate one child of this instance and wait for it, as the exit does for all."""
        self._signal(process, signal.SIGTERM)
        self._reap(process)

    def _reap(self, process):
        """Wait for a signalled child; one that ignores SIGTERM is killed."""
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._signal(process, signal.SIGKILL)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

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
