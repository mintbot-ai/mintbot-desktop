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


def test_foreground_real_gui_and_agent_helper(tmp_path):
    assert Path('mintbot_desktop/__main__.py').exists(), 'foreground CLI missing'
    for binary in ['Xtigervnc', 'tigervncpasswd', 'xauth', 'xdpyinfo', 'openbox', 'chromium']:
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
        finally:
            process.terminate()
            process.wait(timeout=20)
        assert process.returncode == 0
    for port in ports:
        with socket.socket() as sock:
            assert sock.connect_ex(('127.0.0.1', port)) != 0
