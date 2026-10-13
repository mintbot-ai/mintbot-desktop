"""Foreground supervisor and agent CLI. No system installation at runtime."""
import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys

from .session import Config, Children, browser_command, gui_environment, prepare, vnc_command


async def serve(cfg):
    from aiohttp import web
    from .app import create_app

    prepare(cfg)
    with (cfg.state / 'session.lock').open('w') as lock:
        os.chmod(cfg.state / 'session.lock', 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if Path(f'/tmp/.X11-unix/X{cfg.display}').exists() or Path(f'/tmp/.X{cfg.display}-lock').exists():
            raise RuntimeError('Display already exists; will not take over another session')
        for port in (cfg.web_port, cfg.vnc_port, cfg.cdp_port):
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', port))
        env = gui_environment(cfg)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        with Children() as children:
            x = children.start(vnc_command(cfg), env=env)
            for _ in range(100):
                if x.poll() is not None:
                    raise RuntimeError('X server exited before readiness')
                check = await asyncio.create_subprocess_exec('xdpyinfo', env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if await check.wait() == 0:
                    break
                if stop.is_set():
                    return
                await asyncio.sleep(.1)
            else:
                raise RuntimeError('X server readiness timed out')
            children.start(['openbox'], env=env)
            children.start(browser_command(cfg, env), env=env)
            app = create_app(token=(cfg.state / 'token').read_text(), password=(cfg.state / 'vnc-secret').read_text(), rfb_port=cfg.vnc_port)
            runner = web.AppRunner(app, access_log=None)
            try:
                await runner.setup()
                await web.TCPSite(runner, '127.0.0.1', cfg.web_port).start()
                while not stop.is_set():
                    if any(child.poll() is not None for child in children.processes):
                        raise RuntimeError('Desktop child exited unexpectedly')
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=.25)
                    except asyncio.TimeoutError:
                        pass
            finally:
                await runner.cleanup()


def main():
    parser = argparse.ArgumentParser(description='Mintbot Desktop foreground session and shared GUI helper')
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
