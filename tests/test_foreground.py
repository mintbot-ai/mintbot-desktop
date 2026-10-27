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
