"""Showing a desktop that already exists (mintbot_desktop.attach).

The search is exercised with fake commands; the real smoke starts an
Xtigervnc of its own as the "owner's desktop" and lets a session attach to
it through X0tigervnc, the gateway and RFB. No APT, systemd or logind.
"""
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.request

import pytest

from mintbot_desktop import attach
from mintbot_desktop.session import Config, gui_environment, read_attachment, record_attachment

ROOT = Path(__file__).resolve().parents[1]


class FakeCommands:
    """systemctl, loginctl and xdpyinfo as the search sees them."""

    def __init__(self, user_env='', sessions=(), answering=()):
        self.user_env = user_env
        # (session id, uid, Type, Display, State)
        self.sessions = sessions
        # (display, xauthority) pairs xdpyinfo succeeds with
        self.answering = set(answering)
        self.probed = []

    def __call__(self, argv, **kwargs):
        if argv == ['systemctl', '--user', 'show-environment']:
            return subprocess.CompletedProcess(argv, 0, self.user_env, '')
        if argv[:2] == ['loginctl', 'list-sessions']:
            listing = ''.join(f'{sid} {uid} user seat1 - user tty2 no -\n' for sid, uid, *_ in self.sessions)
            return subprocess.CompletedProcess(argv, 0, listing, '')
        if argv[:2] == ['loginctl', 'show-session']:
            sid, _, kind, display, state = next(row for row in self.sessions if row[0] == argv[2])
            return subprocess.CompletedProcess(argv, 0, f'Type={kind}\nDisplay={display}\nState={state}\n', '')
        if argv == ['xdpyinfo']:
            env = kwargs['env']
            pair = (env['DISPLAY'], env.get('XAUTHORITY'))
            self.probed.append(pair)
            return subprocess.CompletedProcess(argv, 0 if pair in self.answering else 1, '', '')
        raise AssertionError(f'unexpected command {argv}')


def search(commands, tmp_path, environ=None, **kwargs):
    (tmp_path / 'home').mkdir(exist_ok=True)
    return attach.find_display(run=commands, environ=environ or {}, uid=1000, home=tmp_path / 'home', **kwargs)


def test_the_user_manager_environment_names_the_display_and_its_authority(tmp_path):
    """Desktops import DISPLAY and XAUTHORITY into the user's systemd manager
    at login; a service of that manager finds the owner's screen there."""
    authority = tmp_path / 'gdm-Xauthority'
    authority.write_bytes(b'cookie')
    commands = FakeCommands(user_env=f'DISPLAY=:0\nXAUTHORITY={authority}\nLANG=C.UTF-8\n', answering=[(':0', str(authority))])
    found, reason = search(commands, tmp_path)
    assert found == attach.Attachment(':0', str(authority)) and reason is None
    assert commands.probed == [(':0', str(authority))]


def test_a_wayland_session_is_not_attached_to(tmp_path):
    """XWayland's DISPLAY answers xdpyinfo, but its root window shows nothing:
    a Wayland environment is no candidate, and the reason says so."""
    commands = FakeCommands(user_env='DISPLAY=:0\nWAYLAND_DISPLAY=wayland-0\n', answering=[(':0', None)],
                            sessions=[('3', '1000', 'wayland', '', 'active')])
    found, reason = search(commands, tmp_path)
    assert found is None and 'Wayland' in reason
    assert commands.probed == []


def test_logind_names_this_users_x11_session_and_gdm_keeps_its_authority(tmp_path):
    """Without a manager environment (a service started another way), logind
    knows the owner's x11 session; another user's session is not theirs.
    GDM keeps the authority file under the runtime directory."""
    runtime = tmp_path / 'run'
    (runtime / 'gdm').mkdir(parents=True)
    authority = runtime / 'gdm' / 'Xauthority'
    authority.write_bytes(b'cookie')
    commands = FakeCommands(sessions=[('2', '1001', 'x11', ':0', 'active'), ('4', '1000', 'x11', ':1', 'active'),
                                      ('5', '1000', 'x11', ':2', 'closing')],
                            answering=[(':1', str(authority)), (':0', str(authority))])
    found, reason = search(commands, tmp_path, environ={'XDG_RUNTIME_DIR': str(runtime)})
    assert found == attach.Attachment(':1', str(authority))
    assert all(display == ':1' for display, _ in commands.probed)


def test_the_users_own_authority_file_is_tried_and_none_at_last(tmp_path):
    commands = FakeCommands(sessions=[('4', '1000', 'x11', ':0', 'active')], answering=[(':0', None)])
    found, _ = search(commands, tmp_path)
    assert found == attach.Attachment(':0', None)
    home = tmp_path / 'home'
    (home / '.Xauthority').write_bytes(b'cookie')
    commands = FakeCommands(sessions=[('4', '1000', 'x11', ':0', 'active')], answering=[(':0', str(home / '.Xauthority'))])
    found, _ = search(commands, tmp_path)
    assert found == attach.Attachment(':0', str(home / '.Xauthority'))


def test_no_desktop_and_a_display_that_does_not_answer_are_told_apart(tmp_path):
    found, reason = search(FakeCommands(), tmp_path)
    assert found is None and reason == 'this user is logged in at no X11 desktop'
    commands = FakeCommands(user_env='DISPLAY=:0\n')
    found, reason = search(commands, tmp_path)
    assert found is None and reason == 'the display :0 did not admit this user'
    assert commands.probed == [(':0', None)]


def test_the_sessions_own_display_is_never_a_candidate(tmp_path):
    commands = FakeCommands(user_env='DISPLAY=:13\n', answering=[(':13', None)])
    found, reason = search(commands, tmp_path, exclude={':13'})
    assert found is None and commands.probed == []


def test_a_named_display_is_the_only_candidate(tmp_path):
    authority = tmp_path / 'auth'
    authority.write_bytes(b'cookie')
    commands = FakeCommands(user_env='DISPLAY=:0\n', answering=[(':0', None), (':5', str(authority))])
    found, _ = search(commands, tmp_path, environ={'XAUTHORITY': str(authority)}, display=':5')
    assert found == attach.Attachment(':5', str(authority))
    assert all(display == ':5' for display, _ in commands.probed)


def test_choose_follows_the_mode(tmp_path):
    cfg = Config(state=tmp_path)
    found = attach.Attachment(':0', None)
    calls, messages = [], []
    finder = lambda **kwargs: (calls.append(kwargs), (found, None))[1]
    present = lambda name: '/usr/bin/' + name
    assert attach.choose(cfg, 'virtual', find=finder, which=present) is None and calls == []
    assert attach.choose(cfg, 'auto', find=finder, which=present) == found
    assert calls[-1] == {'display': None, 'exclude': {':13'}}
    assert attach.choose(cfg, 'attach', ':0', find=finder, which=present) == found
    assert calls[-1]['display'] == ':0'
    nothing = lambda **kwargs: (None, 'this user is logged in at no X11 desktop')
    assert attach.choose(cfg, 'auto', find=nothing, which=present, log=messages.append) is None
    assert messages == ['no existing desktop to show: this user is logged in at no X11 desktop; starting the virtual desktop']
    with pytest.raises(RuntimeError, match='No existing desktop to show: this user'):
        attach.choose(cfg, 'attach', find=nothing, which=present)
    # Without x0vncserver there is nothing to show an existing desktop with.
    messages.clear()
    assert attach.choose(cfg, 'auto', find=finder, which=lambda name: None, log=messages.append) is None
    assert messages == ['x0vncserver is not installed (Debian: tigervnc-scraping-server); starting the virtual desktop']
    with pytest.raises(RuntimeError, match='tigervnc-scraping-server'):
        attach.choose(cfg, 'attach', find=finder, which=lambda name: None)
    with pytest.raises(ValueError):
        attach.choose(cfg, 'sometimes')


def test_debian_names_the_binary_x0tigervnc_and_the_wrapper_is_second():
    """Debian's x0vncserver is a daemonising Perl wrapper; the binary behind
    it is X0tigervnc. Upstream builds call the binary x0vncserver itself."""
    assert attach.SCRAPERS == ('X0tigervnc', 'x0vncserver')
    assert attach.scraper(which=lambda name: '/usr/bin/' + name if name == 'x0vncserver' else None) == 'x0vncserver'
    assert attach.scraper(which=lambda name: None) is None


def test_the_scraper_serves_the_display_like_xtigervnc_serves_the_virtual_one(tmp_path):
    cfg = Config(state=tmp_path, vnc_port=15913)
    argv = attach.scrape_command(cfg, attach.Attachment(':0', None), 'X0tigervnc')
    assert argv[:3] == ['X0tigervnc', '-display', ':0']
    for key, value in {'-localhost': 'yes', '-interface': '127.0.0.1', '-UseIPv6': 'no', '-SecurityTypes': 'VncAuth',
                       '-rfbport': '15913', '-PasswordFile': str(tmp_path / 'vnc-password')}.items():
        assert argv[argv.index(key) + 1] == value
    assert '-AlwaysShared' in argv


def test_the_attached_display_is_what_the_helpers_use(tmp_path):
    """`env`, `exec`, `browse` and `browser` run outside the session: the
    display it shows is recorded in the state directory. HOME stays the
    session's own, so the agent's programs keep out of the owner's settings."""
    cfg = Config(state=tmp_path)
    assert read_attachment(cfg) is None
    assert gui_environment(cfg)['DISPLAY'] == ':13'
    record_attachment(cfg, attach.Attachment(':0', '/run/user/1000/gdm/Xauthority'))
    assert (tmp_path / 'attached.json').stat().st_mode & 0o077 == 0
    env = gui_environment(cfg)
    assert env['DISPLAY'] == ':0' and env['XAUTHORITY'] == '/run/user/1000/gdm/Xauthority'
    assert env['HOME'] == str(tmp_path) and env['XDG_CONFIG_HOME'] == str(tmp_path / 'config')
    record_attachment(cfg, attach.Attachment(':0', None))
    env = gui_environment(cfg)
    assert env['DISPLAY'] == ':0' and 'XAUTHORITY' not in env
    record_attachment(cfg, None)
    assert not (tmp_path / 'attached.json').exists()
    assert gui_environment(cfg)['XAUTHORITY'] == str(tmp_path / 'Xauthority')
    (tmp_path / 'attached.json').write_text('{"display": ""}')
    assert read_attachment(cfg) is None


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def pids_on_display(name, display):
    found = []
    for pid in subprocess.run(['pgrep', '-x', name], capture_output=True, text=True).stdout.split():
        try:
            environ = Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
        except OSError:
            continue
        if f'DISPLAY={display}'.encode() in environ:
            found.append(int(pid))
    return found


def rfb_server_init(url, token):
    """The framebuffer size the gateway's RFB bridge announces, through its own handshake."""
    import asyncio
    import aiohttp

    async def handshake():
        async with aiohttp.ClientSession() as client:
            async with client.ws_connect(url, headers={'Authorization': 'Bearer ' + token}) as ws:
                buffer = bytearray()

                async def receive(count):
                    while len(buffer) < count:
                        message = await ws.receive(timeout=10)
                        assert message.type == aiohttp.WSMsgType.BINARY, message
                        buffer.extend(message.data)
                    chunk = bytes(buffer[:count])
                    del buffer[:count]
                    return chunk
                assert await receive(12) == b'RFB 003.008\n'
                await ws.send_bytes(b'RFB 003.008\n')
                assert await receive(2) == b'\x01\x01'
                await ws.send_bytes(b'\x01')
                assert await receive(4) == b'\0\0\0\0'
                await ws.send_bytes(b'\x01')  # ClientInit: shared
                width, height = struct.unpack('>HH', await receive(4))
                return width, height
    return asyncio.run(handshake())


def test_a_session_attaches_to_an_existing_x_display(tmp_path):
    """An Xtigervnc of the test's own stands in for the owner's desktop: the
    session shows it through X0tigervnc, starts no window manager, icons or
    browser of its own there, points the helpers at it, and leaves it
    running when it stops."""
    for binary in ['Xtigervnc', 'tigervncpasswd', 'xauth', 'xdpyinfo']:
        if not shutil.which(binary):
            pytest.skip(f'{binary} required for the real attach smoke')
    program = attach.scraper()
    if program is None:
        pytest.skip('X0tigervnc (tigervnc-scraping-server) required for the real attach smoke')
    free = (n for n in range(193, 220) if not Path(f'/tmp/.X11-unix/X{n}').exists() and not Path(f'/tmp/.X{n}-lock').exists())
    owner_display, session_display = next(free), next(free)
    owner = tmp_path / 'owner'
    owner.mkdir()
    authority = owner / 'Xauthority'
    authority.touch(mode=0o600)
    subprocess.run(['xauth', '-f', str(authority)], input=f'add :{owner_display} MIT-MAGIC-COOKIE-1 {secrets.token_hex(16)}\n',
                   text=True, capture_output=True, check=True)
    owner_env = {**os.environ, 'DISPLAY': f':{owner_display}', 'XAUTHORITY': str(authority)}
    # -noreset: the owner's desktop always has clients (its window manager);
    # this stand-in has none between probes, and an X server resets when
    # its last client leaves, refusing connections for a moment.
    existing = subprocess.Popen(['Xtigervnc', f':{owner_display}', '-geometry', '1024x768', '-depth', '24', '-localhost', 'yes',
                                 '-rfbport', str(free_port()), '-SecurityTypes', 'None', '-auth', str(authority), '-nolisten', 'tcp', '-noreset'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    ports = [free_port() for _ in range(3)]
    args = [sys.executable, '-m', 'mintbot_desktop', '--state', str(tmp_path / 'state'), '--display', str(session_display),
            '--web-port', str(ports[0]), '--vnc-port', str(ports[1]), '--cdp-port', str(ports[2])]
    try:
        deadline = time.monotonic() + 15
        while subprocess.run(['xdpyinfo'], env=owner_env, capture_output=True).returncode:
            assert existing.poll() is None and time.monotonic() < deadline, 'the stand-in desktop did not come up'
            time.sleep(.2)
        with (tmp_path / 'log').open('w+') as log:
            # The owner's display is named outright: this test runs as whoever
            # runs the suite, with no logind session of its own to find.
            process = subprocess.Popen(args + ['--attach', f':{owner_display}', 'serve'], stdout=log, stderr=log,
                                       env={**os.environ, 'XAUTHORITY': str(authority)})
            try:
                deadline = time.monotonic() + 30
                while True:
                    if process.poll() is not None:
                        log.seek(0)
                        pytest.fail(log.read())
                    try:
                        token = (tmp_path / 'state' / 'token').read_text()
                        request = urllib.request.Request(f'http://127.0.0.1:{ports[0]}/health', headers={'Authorization': 'Bearer ' + token})
                        with urllib.request.urlopen(request, timeout=1) as response:
                            assert json.load(response)['status'] == 'ok'
                        break
                    except (OSError, ValueError):
                        assert time.monotonic() < deadline, 'attach startup timed out'
                        time.sleep(.15)
                # The gateway bridges to the owner's screen: its size, not the virtual desktop's 1440x900.
                assert rfb_server_init(f'http://127.0.0.1:{ports[0]}/desktop/rfb', token) == (1024, 768)
                # The view keeps that size instead of resizing the owner's screen.
                request = urllib.request.Request(f'http://127.0.0.1:{ports[0]}/desktop/app.js', headers={'Authorization': 'Bearer ' + token})
                with urllib.request.urlopen(request, timeout=5) as response:
                    assert b'rfb.resizeSession = false;' in response.read()
                # The helpers run on the owner's display with its authority file.
                result = subprocess.run(args + ['env'], capture_output=True, text=True, timeout=5)
                assert json.loads(result.stdout) == {'DISPLAY': f':{owner_display}', 'XAUTHORITY': str(authority), 'HOME': str(tmp_path / 'state')}
                result = subprocess.run(args + ['exec', '--', 'xdpyinfo'], capture_output=True, text=True, timeout=5)
                assert result.returncode == 0 and '1024x768 pixels' in result.stdout, result.stderr
                # Nothing of the package's own was started on the owner's screen, and no display of its own either.
                assert not pids_on_display('openbox', f':{owner_display}') and not pids_on_display('idesk', f':{owner_display}')
                assert not pids_on_display('chromium', f':{owner_display}')
                assert not Path(f'/tmp/.X11-unix/X{session_display}').exists()
                assert not (tmp_path / 'state' / '.idesktop').exists()
                assert len(pids_on_display(program, f':{owner_display}')) == 1
                # Only loopback listeners, and no CDP port taken: no browser was started.
                result = subprocess.run(['ss', '-ltnH'], capture_output=True, text=True, check=True)
                listening = [line.split()[3] for line in result.stdout.splitlines()]
                assert f'127.0.0.1:{ports[0]}' in listening and f'127.0.0.1:{ports[1]}' in listening
                assert not any(entry.endswith(f':{ports[2]}') for entry in listening)
                assert not any(entry.endswith(f':{ports[1]}') and not entry.startswith('127.0.0.1:') for entry in listening)
            finally:
                process.terminate()
                process.wait(timeout=20)
            log.seek(0)
            assert process.returncode == 0, log.read()
            assert f'showing the existing desktop on :{owner_display}' in log.read()
        # The owner's desktop outlives the session that showed it.
        assert existing.poll() is None
        assert subprocess.run(['xdpyinfo'], env=owner_env, capture_output=True).returncode == 0
        assert not pids_on_display(program, f':{owner_display}')
        # The owner logs out while shown: X0tigervnc keeps exiting, the
        # session gives up with the reason (the host's restart looks for a
        # desktop again) and takes nothing else down with it.
        with (tmp_path / 'log2').open('w+') as log:
            process = subprocess.Popen(args + ['--attach', f':{owner_display}', 'serve'], stdout=log, stderr=log,
                                       env={**os.environ, 'XAUTHORITY': str(authority)})
            try:
                deadline = time.monotonic() + 30
                while not pids_on_display(program, f':{owner_display}'):
                    assert process.poll() is None and time.monotonic() < deadline, 'second attach did not come up'
                    time.sleep(.2)
                existing.kill()
                existing.wait(timeout=20)
                process.wait(timeout=60)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=20)
            log.seek(0)
            text = log.read()
            assert process.returncode == 1 and 'desktop scraper keeps exiting' in text, text
        assert not pids_on_display(program, f':{owner_display}')
    finally:
        if existing.poll() is None:
            existing.terminate()
            existing.wait(timeout=20)
        # A killed X server leaves its lock and socket behind.
        for leftover in (Path(f'/tmp/.X{owner_display}-lock'), Path(f'/tmp/.X11-unix/X{owner_display}')):
            leftover.unlink(missing_ok=True)


def test_a_display_that_is_not_there_is_refused_before_anything_starts(tmp_path):
    """A named display that admits nobody (the owner logged out before the
    start) ends the start with the reason; nothing is started."""
    program = attach.scraper()
    if program is None or not shutil.which('tigervncpasswd'):
        pytest.skip('X0tigervnc (tigervnc-scraping-server) required')
    display = next(n for n in range(220, 240) if not Path(f'/tmp/.X11-unix/X{n}').exists())
    ports = [free_port() for _ in range(3)]
    args = [sys.executable, '-m', 'mintbot_desktop', '--state', str(tmp_path / 'state'), '--display', str(display + 1),
            '--web-port', str(ports[0]), '--vnc-port', str(ports[1]), '--cdp-port', str(ports[2])]
    result = subprocess.run(args + ['--attach', f':{display}', 'serve'], capture_output=True, text=True, timeout=60)
    assert result.returncode == 1
    assert f'No existing desktop to show: the display :{display} did not admit this user' in result.stderr
