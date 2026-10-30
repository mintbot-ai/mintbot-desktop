"""Foreground supervisor and agent CLI. No system installation at runtime."""
import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time

from . import attach, browse, rdp
from .session import (Config, Children, browser_command, display_in_use, gui_environment, open_browser,
                      port_available, prepare, record_attachment, vnc_command, write_desktop)


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


def check_icons(icons):
    """The icon layer's turn in the loop: the layer, or None once it is given up on.

    The icons are a convenience; the openbox root menu opens the browser
    without them. A layer that keeps dying is dropped, not the session: the
    X server, the window manager and the browser the user is working in
    stay up.
    """
    try:
        icons.check()
    except RuntimeError as error:
        log(f'{error}; the desktop goes on without icons, the openbox menu still opens the browser')
        return None
    return icons


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


async def wait_for_listener(process, port, stop, timeout=10.0):
    """Block until the VNC server listens on the port, exits, or the session is asked to stop."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f'the desktop scraper exited with status {process.returncode} before listening')
        if stop.is_set():
            return
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=.5):
                return
        except OSError:
            pass
        await asyncio.sleep(.1)
    raise RuntimeError('the desktop scraper did not start listening in time')


class VirtualDesktop:
    """The package's own desktop: X server, window manager, icon layer and browser."""

    def __init__(self, x, manager, icons, browser, env, stop):
        self.x, self.manager, self.icons, self.browser, self.env, self.stop = x, manager, icons, browser, env, stop

    @classmethod
    async def start(cls, cfg, children, env, stop):
        """The desktop, up; None when the session was asked to stop meanwhile."""
        x = children.start(vnc_command(cfg), env=env)
        await wait_for_display(x, env, stop, cfg.display)
        if stop.is_set():
            return None
        write_desktop(cfg)
        manager = Supervised(children, 'window manager', ['openbox'], env)
        # The icon layer comes once the window manager is up, never
        # before (wait_for_window_manager). It is optional (an older
        # standalone install without the package); the openbox root menu
        # opens the browser either way.
        await wait_for_window_manager(manager, env, stop)
        if stop.is_set():
            return None
        icons = None
        if shutil.which('idesk'):
            icons = Supervised(children, 'desktop icons', ['idesk'], env)
        else:
            log('idesk is not installed; the browser icon is missing, the openbox menu still opens it')
        browser = Once(children, 'browser', browser_command(cfg, env), env)
        return cls(x, manager, icons, browser, env, stop)

    async def check(self):
        if self.x.poll() is not None:
            raise RuntimeError(f'X server exited with status {self.x.returncode}')
        if self.manager.check():
            # A new openbox lowers a new anchor beneath the icons
            # that already exist: the icon layer starts over
            # once it is up, and is at the bottom again.
            await wait_for_window_manager(self.manager, self.env, self.stop)
            if self.icons and not self.stop.is_set():
                self.icons.restart()
        if self.icons:
            self.icons = check_icons(self.icons)
        self.browser.check()


class ExistingDesktop:
    """The computer's own desktop (attach.py), served over RFB by x0vncserver.

    Nothing else is started on it: the owner's window manager runs it, and a
    browser that popped up on their screen by itself would be in their way.
    `mintbot-desktop browser` opens the agent's browser there when needed.
    """

    def __init__(self, scraper):
        self.scraper = scraper

    @classmethod
    async def start(cls, cfg, children, env, stop, attachment):
        log(f'showing the existing desktop on {attachment.display}')
        scraper = Supervised(children, 'desktop scraper', attach.scrape_command(cfg, attachment, attach.scraper()), env)
        await wait_for_listener(scraper.process, cfg.vnc_port, stop)
        return None if stop.is_set() else cls(scraper)

    async def check(self):
        # A scraper that keeps exiting has lost its display (the owner
        # logged out): the session ends, and the host's restart looks for
        # a desktop to show again.
        self.scraper.check()


class SharedDesktop:
    """The computer's own desktop shown by its own RDP server (attach.Shared, rdp.py).

    The server is the owner's session's (gnome-remote-desktop, krdp): not a
    child of this session, not restarted by it. It is watched the only way
    it can be: as long as something listens on its port, it is there
    (rdp.port_listened: the kernel's tables, since a connection would land
    in the server's log as an error). One that is gone for a while (the
    owner logged out) ends the session, and the host's restart looks for a
    desktop to show again.
    """
    PROBE_INTERVAL = 5.0
    GRACE = 30.0

    def __init__(self, target, clock=time.monotonic, listened=rdp.port_listened):
        self.target, self.clock, self.listened = target, clock, listened
        self.probed = self.last_seen = clock()

    @classmethod
    async def start(cls, shared):
        log(f'showing the existing desktop through its RDP server at {shared.target.destination} ({shared.target.source})')
        if not await asyncio.to_thread(rdp.listening, shared.target.host, shared.target.port, 3.0):
            raise RuntimeError(f'the RDP server at {shared.target.destination} does not accept connections')
        return cls(shared.target)

    async def check(self):
        now = self.clock()
        if now - self.probed < self.PROBE_INTERVAL:
            return
        self.probed = now
        present = self.listened(self.target.port)
        if present is None:
            present = await asyncio.to_thread(rdp.listening, self.target.host, self.target.port, 3.0)
        if present:
            self.last_seen = now
        elif now - self.last_seen >= self.GRACE:
            raise RuntimeError(f'the RDP server at {self.target.destination} has been gone for {self.GRACE:g}s; giving up')


def viewer_directory(cfg):
    """Where lifecycle.install_viewer put IronRDP's browser client: the install prefix, or the checkout's."""
    try:
        prefix = Path(json.loads((cfg.state / 'install.json').read_text())['prefix'])
    except (OSError, ValueError, KeyError, TypeError):
        prefix = Path(__file__).resolve().parents[1]
    return prefix / 'ironrdp'


async def serve(cfg, mode='auto', display=None, rdp_address=None):
    from aiohttp import web
    from .app import create_app

    prepare(cfg)
    with (cfg.state / 'session.lock').open('w') as lock:
        os.chmod(cfg.state / 'session.lock', 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another desktop session already uses this state directory') from None
        attachment = attach.choose(cfg, mode, display, rdp_address, log=log)
        record_attachment(cfg, attachment)
        shared = attachment if isinstance(attachment, attach.Shared) else None
        ports = [('web', cfg.web_port)] if shared else [('web', cfg.web_port), ('VNC', cfg.vnc_port)]
        if attachment is None:
            if display_in_use(cfg.display):
                raise RuntimeError(f'Display :{cfg.display} already exists; will not take over another session')
            ports.append(('CDP', cfg.cdp_port))
        for name, port in ports:
            if not port_available(port):
                raise RuntimeError(f'{name} port {port} is already in use')
        env = gui_environment(cfg)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        with Children() as children:
            if attachment is None:
                desktop = await VirtualDesktop.start(cfg, children, env, stop)
            elif shared:
                desktop = await SharedDesktop.start(shared)
            else:
                desktop = await ExistingDesktop.start(cfg, children, env, stop, attachment)
            if desktop is None:
                return
            app = create_app(token=(cfg.state / 'token').read_text(), password=(cfg.state / 'vnc-secret').read_text(),
                             rfb_port=cfg.vnc_port, resize_session=attachment is None,
                             rdp=shared.target if shared else None, ironrdp=viewer_directory(cfg))
            runner = web.AppRunner(app, access_log=None)
            try:
                await runner.setup()
                await web.TCPSite(runner, '127.0.0.1', cfg.web_port).start()
                while not stop.is_set():
                    await desktop.check()
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
    # Which desktop `serve` shows (attach.py): the computer's own when there
    # is one (auto), that one only (attach), or the package's own (virtual).
    # `--attach` names the existing X display outright instead of looking,
    # `--rdp` the existing desktop's RDP server (credentials: rdp.py).
    parser.add_argument('--mode', choices=attach.MODES, default=os.environ.get('MINTBOT_DESKTOP_MODE') or 'auto')
    parser.add_argument('--attach', metavar='DISPLAY', default=os.environ.get('MINTBOT_DESKTOP_ATTACH') or None)
    parser.add_argument('--rdp', metavar='HOST:PORT', default=os.environ.get('MINTBOT_DESKTOP_RDP') or None)
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
        asyncio.run(serve(cfg, 'attach' if args.attach or args.rdp else args.mode, args.attach, args.rdp))
    elif args.command == 'env':
        env = gui_environment(cfg)
        # XAUTHORITY is null on an existing display that asks for no authority
        # file; WAYLAND_DISPLAY is set on a Wayland desktop only.
        print(json.dumps({key: env.get(key) for key in ('DISPLAY', 'XAUTHORITY', 'WAYLAND_DISPLAY', 'HOME')}))
    elif args.command == 'browse':
        browse.main(cfg, args.args[1:] if args.args[:1] == ['--'] else args.args)
    elif args.command == 'browser':
        open_browser(cfg)
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
