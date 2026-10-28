"""Foreground supervisor and agent CLI. No system installation at runtime."""
import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from . import browse
from .session import (Config, Children, browser_command, display_in_use, gui_environment, port_available,
                      prepare, vnc_command, write_desktop)


# A window manager or browser that dies this often is broken, not unlucky.
RESTART_LIMIT = 3
RESTART_WINDOW = 60.0


def log(message):
    print(f'mintbot-desktop: {message}', file=sys.stderr, flush=True)


async def wait_for_display(x, env, stop, display, tmp=Path('/tmp'), probe=('xdpyinfo',), timeout=10.0, probe_timeout=2.0):
    """Block until the X server answers, exits, or the session is asked to stop.

    The probe runs only once the server's socket exists. Asked about a display
    whose socket is not there yet, libxcb falls back to TCP on localhost:6000+N;
    under the host's address filter that SYN is dropped, not refused, and the
    first probe of every service start hung for a whole TCP connect timeout
    (about two minutes) before the window manager and browser came up. Each
    probe is bounded for the same reason.
    """
    socket_path = tmp / '.X11-unix' / f'X{display}'
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if x.poll() is not None:
            raise RuntimeError(f'X server exited with status {x.returncode} before readiness')
        if stop.is_set():
            return
        if socket_path.exists():
            check = await asyncio.create_subprocess_exec(*probe, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                if await asyncio.wait_for(check.wait(), timeout=probe_timeout) == 0:
                    return
            except asyncio.TimeoutError:
                check.kill()
                await check.wait()
        await asyncio.sleep(.1)
    raise RuntimeError('X server readiness timed out')


class Supervised:
    """A child that is restarted when it dies, up to RESTART_LIMIT times per RESTART_WINDOW."""
    def __init__(self, children, name, argv, env):
        self.children, self.name, self.argv, self.env = children, name, argv, env
        self.process = children.start(argv, env=env)
        self.restarts = []

    def check(self):
        """Restart the child if it died; True when it was restarted just now."""
        if self.process.poll() is None:
            return False
        now = time.monotonic()
        self.restarts = [moment for moment in self.restarts if now - moment < RESTART_WINDOW]
        if len(self.restarts) >= RESTART_LIMIT:
            raise RuntimeError(f'{self.name} keeps exiting (last status {self.process.returncode}); giving up')
        self.restarts.append(now)
        log(f'{self.name} exited with status {self.process.returncode}; restarting')
        self.process = self.children.start(self.argv, env=self.env)
        return True

    def restart(self):
        """Start the child over on purpose: not a death, so it does not count towards the limit."""
        self.children.stop(self.process)
        self.process = self.children.start(self.argv, env=self.env)


async def xprop(env, args, timeout=2.0):
    """xprop's output, or nothing when it fails or hangs (a window that is gone, a server that does not answer)."""
    check = await asyncio.create_subprocess_exec('xprop', *args, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(check.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        check.kill()
        await check.wait()
        return b''
    return output


async def window_manager_ready(env):
    """True once the root window names an anchor window that bears the mark too.

    openbox publishes _NET_SUPPORTING_WM_CHECK on the root window right
    after lowering its anchor, then on the anchor itself. The root's copy
    outlives a dead openbox (a root property is nobody's resource), so on
    its own it would pass a restart at once; the anchor it names is gone
    with its owner, and the next openbox marks its own anchor only once
    that is in place.
    """
    output = await xprop(env, ['-root', '_NET_SUPPORTING_WM_CHECK'])
    if b'window id' not in output:
        return False
    anchor = output.rpartition(b'# ')[2].split()
    if not anchor:
        return False
    return b'window id' in await xprop(env, ['-id', anchor[0].decode(), '_NET_SUPPORTING_WM_CHECK'])


async def wait_for_window_manager(manager, env, stop, ready=window_manager_ready, timeout=15.0):
    """Block until openbox owns the screen, or the session is asked to stop.

    The icon layer may only start afterwards. When openbox starts it puts an
    anchor window at the very bottom of the stack and from then on keeps
    every application window just below that anchor (openbox/screen.c,
    screen_annex); idesk lowers its icons once, when it creates them. An
    icon that already exists when openbox starts therefore ends up above the
    anchor, and above every window the user opens, for good - the race the
    two lost on real agents when they were started together.

    A window manager that dies meanwhile is restarted (or given up on) here
    as in the main loop; one that is up but silent is logged and waited for
    no longer, since a desktop without icons in order beats none at all.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        manager.check()
        if stop.is_set():
            return
        if await ready(env):
            return
        await asyncio.sleep(.1)
    log('window manager readiness timed out; the desktop icons may cover windows')


class Once:
    """A child started with the session and left alone afterwards.

    The browser: the user closes it like any program, and a browser that
    reappeared on its own would be in their way. The desktop icon and the
    openbox menu open it again (`mintbot-desktop browser`), the agent's
    `browse` says so when CDP does not answer.
    """
    def __init__(self, children, name, argv, env):
        self.name = name
        self.process = children.start(argv, env=env)
        self.reported = False

    def check(self):
        if self.process.poll() is None or self.reported:
            return
        self.reported = True
        log(f'{self.name} exited with status {self.process.returncode}; not restarted')


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
            await wait_for_display(x, env, stop, cfg.display)
            if stop.is_set():
                return
            write_desktop(cfg)
            manager = Supervised(children, 'window manager', ['openbox'], env)
            # The icon layer comes once the window manager is up, never
            # before (wait_for_window_manager). It is optional (an older
            # standalone install without the package); the openbox root menu
            # opens the browser either way.
            await wait_for_window_manager(manager, env, stop)
            if stop.is_set():
                return
            icons = None
            if shutil.which('idesk'):
                icons = Supervised(children, 'desktop icons', ['idesk'], env)
            else:
                log('idesk is not installed; the browser icon is missing, the openbox menu still opens it')
            browser = Once(children, 'browser', browser_command(cfg, env), env)
            app = create_app(token=(cfg.state / 'token').read_text(), password=(cfg.state / 'vnc-secret').read_text(), rfb_port=cfg.vnc_port)
            runner = web.AppRunner(app, access_log=None)
            try:
                await runner.setup()
                await web.TCPSite(runner, '127.0.0.1', cfg.web_port).start()
                while not stop.is_set():
                    if x.poll() is not None:
                        raise RuntimeError(f'X server exited with status {x.returncode}')
                    if manager.check():
                        # A new openbox lowers a new anchor beneath the icons
                        # that already exist: the icon layer starts over
                        # once it is up, and is at the bottom again.
                        await wait_for_window_manager(manager, env, stop)
                        if icons and not stop.is_set():
                            icons.restart()
                    if icons:
                        icons.check()
                    browser.check()
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
    parser.add_argument('command', choices=('serve', 'prepare', 'env', 'exec', 'browse', 'browser'))
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
    elif args.command == 'browse':
        browse.main(cfg, args.args[1:] if args.args[:1] == ['--'] else args.args)
    elif args.command == 'browser':
        # (Re)open the session's Chromium: the desktop icon, the openbox menu
        # and the agent run this after the user closed it.
        env = gui_environment(cfg)
        command = browser_command(cfg, env)
        os.execvpe(command[0], command, env)
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
