"""Real, isolated TigerVNC/Openbox/Chromium smoke; no APT or systemd."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

import pytest


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def cdp_answers(port):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/version', timeout=1) as response:
            return 'Chrome' in json.load(response)['Browser']
    except (OSError, ValueError):
        return False


def browser_window(gui, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = subprocess.run(['xdotool', 'search', '--onlyvisible', '--class', 'chromium'], env=gui, capture_output=True, text=True).stdout.split()
        if found:
            return found[0]
        time.sleep(.2)
    raise AssertionError('no visible Chromium window')


def stacking_top_first(gui):
    """The root window's direct children, top first (xwininfo lists them so),
    each as its own line followed by the ids of every window nested in it."""
    out = subprocess.run(['xwininfo', '-root', '-tree'], env=gui, capture_output=True, text=True, check=True).stdout
    rows = [(len(line) - len(line.lstrip()), line.strip()) for line in out.splitlines() if line.strip().startswith('0x')]
    top = min(indent for indent, _ in rows)
    stack = []
    for indent, text in rows:
        if indent == top:
            stack.append([text])
        else:
            stack[-1].append(text.split()[0])
    return stack


def icons_below(gui, window):
    """True when every desktop icon (idesk's unmanaged 64x64 window at 40,40)
    sits beneath the frame openbox holds the client `window` in. A client
    without a frame (no window manager at the moment) is not below anything."""
    stack = stacking_top_first(gui)
    client = hex(int(window))
    frames = [i for i, entry in enumerate(stack) if client in entry[1:]]
    icons = [i for i, entry in enumerate(stack) if '64x64+40+40' in entry[0]]
    return bool(frames and icons) and all(i > frames[0] for i in icons)


def pids_on_display(name, display):
    """The processes called `name` serving this display alone; other sessions on the host keep theirs."""
    found = []
    for pid in subprocess.run(['pgrep', '-x', name], capture_output=True, text=True).stdout.split():
        try:
            environ = Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
        except OSError:
            continue
        if f'DISPLAY=:{display}'.encode() in environ:
            found.append(int(pid))
    return found


def openbox_pid(display):
    return next(iter(pids_on_display('openbox', display)), None)


def test_foreground_real_gui_and_agent_helper(tmp_path):
    assert Path('mintbot_desktop/__main__.py').exists(), 'foreground CLI missing'
    for binary in ['Xtigervnc', 'tigervncpasswd', 'xauth', 'xdpyinfo', 'openbox', 'chromium', 'xdotool', 'xprop']:
        if not shutil.which(binary):
            pytest.skip(f'{binary} required for real GUI smoke')
    display = next(n for n in range(193, 220) if not Path(f'/tmp/.X11-unix/X{n}').exists() and not Path(f'/tmp/.X{n}-lock').exists())
    ports = [free_port() for _ in range(3)]
    args = [sys.executable, '-m', 'mintbot_desktop', '--state', str(tmp_path / 'state'), '--display', str(display), '--web-port', str(ports[0]), '--vnc-port', str(ports[1]), '--cdp-port', str(ports[2])]
    with (tmp_path / 'log').open('w+') as log:
        process = subprocess.Popen(args + ['serve'], stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    log.seek(0)
                    pytest.fail(log.read())
                try:
                    token = (tmp_path / 'state' / 'token').read_text()
                    request = urllib.request.Request(f'http://127.0.0.1:{ports[0]}/health', headers={'Authorization': 'Bearer '+token})
                    with urllib.request.urlopen(request, timeout=1) as response:
                        assert json.load(response)['status'] == 'ok'
                    with urllib.request.urlopen(f'http://127.0.0.1:{ports[2]}/json/version', timeout=1) as response:
                        assert 'Chrome' in json.load(response)['Browser']
                    break
                except (OSError, ValueError):
                    time.sleep(.15)
            else:
                log.seek(0)
                pytest.fail('GUI startup timed out: '+log.read())
            result = subprocess.run(args + ['exec', '--', 'xdpyinfo'], capture_output=True, text=True, timeout=5)
            assert result.returncode == 0, result.stderr
            assert '1440x900 pixels' in result.stdout
            # No new public listeners: kernel addresses for all three children.
            result = subprocess.run(['ss', '-ltnH'], capture_output=True, text=True, check=True)
            for port in ports:
                lines = [line for line in result.stdout.splitlines() if line.split()[3].endswith(':'+str(port))]
                assert lines and all(line.split()[3] == f'127.0.0.1:{port}' for line in lines), lines
            # The desktop icon and the menu are in place for this session.
            assert (tmp_path / 'state' / '.idesktop' / 'browser.lnk').is_file()
            assert (tmp_path / 'state' / 'config' / 'openbox' / 'menu.xml').is_file()
            if shutil.which('idesk'):
                assert subprocess.run(['pgrep', '-x', 'idesk'], capture_output=True).returncode == 0
            gui = {**os.environ, 'DISPLAY': f':{display}', 'XAUTHORITY': str(tmp_path / 'state' / 'Xauthority')}
            window = browser_window(gui)
            # Chromium publishes its own window icon and class; openbox shows them.
            props = subprocess.run(['xprop', '-id', window, 'WM_CLASS', '_NET_WM_ICON'], env=gui, capture_output=True, text=True, check=True).stdout
            assert '"Chromium"' in props and '_NET_WM_ICON(CARDINAL)' in props and 'Icon (' in props, props
            if shutil.which('idesk'):
                # The desktop icons lie beneath the windows: the icon layer
                # started only once openbox had put its anchor at the bottom.
                assert icons_below(gui, window), stacking_top_first(gui)
                # A window manager that died comes back with a new anchor
                # beneath the icons; the icon layer is started over after it.
                manager = openbox_pid(display)
                assert manager
                os.kill(manager, 9)
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and not (openbox_pid(display) not in (None, manager) and icons_below(gui, window)):
                    time.sleep(.25)
                assert openbox_pid(display) not in (None, manager) and process.poll() is None
                assert icons_below(gui, window), stacking_top_first(gui)
                # A double-click on the browser icon while the browser runs:
                # idesk runs the icon's command from its own process (its
                # fork() result is compared before it is assigned), so the
                # launcher is the supervised idesk itself. Chromium hands the
                # start over to the running instance, which brings its window
                # back; the session goes on and one idesk serves the display.
                subprocess.run(['xdotool', 'windowminimize', '--sync', window], env=gui, check=True, timeout=10)
                assert not subprocess.run(['xdotool', 'search', '--onlyvisible', '--class', 'chromium'], env=gui, capture_output=True, text=True).stdout.split()
                subprocess.run(['xdotool', 'mousemove', '72', '72', 'click', '--repeat', '2', '--delay', '120', '1'], env=gui, check=True)
                restored = browser_window(gui)
                time.sleep(3)
                assert process.poll() is None, 'the session died after a double-click on the browser icon'
                assert len(pids_on_display('idesk', display)) == 1
                assert icons_below(gui, restored), stacking_top_first(gui)
                # The supervised idesk is still idesk: no icon layer died or was given up on.
                log.seek(0)
                assert 'desktop icons' not in log.read()
            # The user closes the browser: it stays closed, the session goes on.
            pid = subprocess.run(['xdotool', 'getwindowpid', window], env=gui, capture_output=True, text=True, check=True).stdout.strip()
            os.kill(int(pid), 15)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and cdp_answers(ports[2]):
                time.sleep(.2)
            assert not cdp_answers(ports[2])
            time.sleep(2)
            assert not cdp_answers(ports[2]) and process.poll() is None
            assert not subprocess.run(['xdotool', 'search', '--onlyvisible', '--class', 'chromium'], env=gui, capture_output=True, text=True).stdout.split()
            # `browser` (the icon's command) opens it again, on the same CDP port.
            reopened = subprocess.Popen(args + ['browser'], stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and not cdp_answers(ports[2]):
                    assert reopened.poll() is None, 'browser launcher exited'
                    time.sleep(.2)
                assert cdp_answers(ports[2])
                assert browser_window(gui)
            finally:
                reopened.terminate()
                reopened.wait(timeout=20)
        finally:
            process.terminate()
            process.wait(timeout=20)
        assert process.returncode == 0
    for port in ports:
        with socket.socket() as sock:
            assert sock.connect_ex(('127.0.0.1', port)) != 0
