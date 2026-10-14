"""Shared session configuration and foreground child ownership."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess


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
