"""Shared session configuration and foreground child ownership."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import signal
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
    private_write(cfg.state / 'vnc-secret', secrets.token_hex(4).encode())
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


class Children:
    """Only terminate process groups created by this instance."""
    def __init__(self):
        self.processes = []

    def __enter__(self):
        return self

    def start(self, argv, **kwargs):
        process = subprocess.Popen(argv, start_new_session=True, **kwargs)
        self.processes.append(process)
        return process

    def __exit__(self, *exc):
        for process in reversed(self.processes):
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for process in reversed(self.processes):
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
