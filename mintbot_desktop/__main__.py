"""Foreground supervisor and agent CLI. No system installation at runtime."""
import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .session import (Config, Children, browser_command, display_in_use, gui_environment, port_available,
                      prepare, vnc_command)


# A window manager or browser that dies this often is broken, not unlucky.
RESTART_LIMIT = 3
RESTART_WINDOW = 60.0


def log(message):
    print(f'mintbot-desktop: {message}', file=sys.stderr, flush=True)


async def wait_for_display(x, env, stop):
    """Block until the X server answers, exits, or the session is asked to stop."""
    for _ in range(100):
        if x.poll() is not None:
            raise RuntimeError(f'X server exited with status {x.returncode} before readiness')
        check = await asyncio.create_subprocess_exec('xdpyinfo', env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if await check.wait() == 0 or stop.is_set():
            return
        await asyncio.sleep(.1)
    raise RuntimeError('X server readiness timed out')


class Supervised:
    """A child that is restarted when it dies, up to RESTART_LIMIT times per RESTART_WINDOW."""
    def __init__(self, children, name, argv, env):
        self.children, self.name, self.argv, self.env = children, name, argv, env
        self.process = children.start(argv, env=env)
        self.restarts = []

    def check(self):
        if self.process.poll() is None:
            return
        now = time.monotonic()
        self.restarts = [moment for moment in self.restarts if now - moment < RESTART_WINDOW]
        if len(self.restarts) >= RESTART_LIMIT:
            raise RuntimeError(f'{self.name} keeps exiting (last status {self.process.returncode}); giving up')
        self.restarts.append(now)
        log(f'{self.name} exited with status {self.process.returncode}; restarting')
        self.process = self.children.start(self.argv, env=self.env)


async def serve(cfg):
    from aiohttp import web
    from .app import create_app

    prepare(cfg)
    with (cfg.state / 'session.lock').open('w') as lock:
        os.chmod(cfg.state / 'session.lock', 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another desktop session already uses this state directory') from None
        if display_in_use(cfg.display):
            raise RuntimeError(f'Display :{cfg.display} already exists; will not take over another session')
        for name, port in (('web', cfg.web_port), ('VNC', cfg.vnc_port), ('CDP', cfg.cdp_port)):
            if not port_available(port):
                raise RuntimeError(f'{name} port {port} is already in use')
        env = gui_environment(cfg)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        with Children() as children:
            x = children.start(vnc_command(cfg), env=env)
            await wait_for_display(x, env, stop)
            if stop.is_set():
                return
            supervised = [Supervised(children, 'window manager', ['openbox'], env),
                          Supervised(children, 'browser', browser_command(cfg, env), env)]
            app = create_app(token=(cfg.state / 'token').read_text(), password=(cfg.state / 'vnc-secret').read_text(), rfb_port=cfg.vnc_port)
            runner = web.AppRunner(app, access_log=None)
            try:
                await runner.setup()
                await web.TCPSite(runner, '127.0.0.1', cfg.web_port).start()
                while not stop.is_set():
                    if x.poll() is not None:
                        raise RuntimeError(f'X server exited with status {x.returncode}')
                    for child in supervised:
                        child.check()
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=.25)
                    except asyncio.TimeoutError:
                        pass
            finally:
                await runner.cleanup()


def main():
    parser = argparse.ArgumentParser(description='mintbot Desktop foreground session and shared GUI helper')
    parser.add_argument('--state', type=Path, default=Path(os.environ.get('AXP_STATE_DIR', '/var/lib/axp/mintbot.ai/mintbot-desktop')))
    for name in ('display', 'web-port', 'vnc-port', 'cdp-port'):
        parser.add_argument('--'+name, type=int)
    parser.add_argument('command', choices=('serve', 'prepare', 'env', 'exec'))
    parser.add_argument('args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    settings = args.state / 'session.json'
    values = json.loads(settings.read_text()) if settings.exists() else {}
    values.update({key: getattr(args, key) for key in ('display', 'web_port', 'vnc_port', 'cdp_port') if getattr(args, key) is not None})
    cfg = Config(state=args.state, **values)
    if args.command == 'prepare':
        prepare(cfg)
    elif args.command == 'serve':
        asyncio.run(serve(cfg))
    elif args.command == 'env':
        env = gui_environment(cfg)
        print(json.dumps({key: env[key] for key in ('DISPLAY', 'XAUTHORITY', 'HOME')}))
    else:
        command = args.args[1:] if args.args[:1] == ['--'] else args.args
        if not command:
            parser.error('exec requires a command after --')
        os.execvpe(command[0], command, gui_environment(cfg))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        print(f'mintbot-desktop: {error}', file=sys.stderr)
        sys.exit(1)
