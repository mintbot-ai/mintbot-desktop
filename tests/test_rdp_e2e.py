"""The RDP path end to end, with real programs standing in for the owner's Wayland desktop.

FreeRDP's shadow server (`freerdp-shadow-cli`, Debian: freerdp3-shadow-x11)
shares an X display over RDP the way GNOME Remote Desktop shares a Wayland
screen; a stand-in Xtigervnc with a red root window is that display. The
session is started with `--rdp` at that server, and a headless Chromium
opens the panel view: IronRDP's client loads, the gateway proxies
RDCleanPath, and the frame that arrives is red. This proves the pieces
that fake servers cannot: IronRDP's own RDCleanPath encoding against the
gateway's decoder, the TLS upgrade against a real RDP server, and the
WebAssembly client rendering in a browser.

IronRDP's scripts are fetched from the npm registry into the checkout's
`ironrdp/` directory once (the pinned packages lifecycle installs); with
no network the test is skipped.
"""
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from mintbot_desktop import lifecycle

ROOT = Path(__file__).resolve().parents[1]
SHADOW_SERVERS = ('freerdp-shadow-cli3', 'freerdp-shadow-cli')


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def wait_for(condition, timeout, what):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, what
        time.sleep(.2)


def listening(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=.5):
            return True
    except OSError:
        return False


def viewer_scripts():
    """IronRDP's scripts in the checkout, fetched once; None when they cannot be."""
    directory = ROOT / lifecycle.VIEWER_DIRECTORY
    try:
        lifecycle.fetch_viewer(directory)
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return directory


@pytest.fixture
def shadowed_display(tmp_path):
    """A red X display served over RDP (TLS, no credentials) by FreeRDP's shadow server."""
    shadow = next((name for name in SHADOW_SERVERS if shutil.which(name)), None)
    for binary in ['Xtigervnc', 'xauth', 'xdpyinfo', 'xsetroot']:
        if not shutil.which(binary):
            pytest.skip(f'{binary} required for the RDP end-to-end test')
    if shadow is None:
        pytest.skip('freerdp-shadow-cli (freerdp3-shadow-x11) required for the RDP end-to-end test')
    display = next(n for n in range(240, 260) if not Path(f'/tmp/.X11-unix/X{n}').exists() and not Path(f'/tmp/.X{n}-lock').exists())
    authority = tmp_path / 'Xauthority'
    authority.touch(mode=0o600)
    subprocess.run(['xauth', '-f', str(authority)], input=f'add :{display} MIT-MAGIC-COOKIE-1 {secrets.token_hex(16)}\n',
                   text=True, capture_output=True, check=True)
    env = {**os.environ, 'DISPLAY': f':{display}', 'XAUTHORITY': str(authority)}
    x = subprocess.Popen(['Xtigervnc', f':{display}', '-geometry', '800x600', '-depth', '24', '-localhost', 'yes',
                          '-rfbport', str(free_port()), '-SecurityTypes', 'None', '-auth', str(authority), '-nolisten', 'tcp', '-noreset'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    server = None
    try:
        wait_for(lambda: x.poll() is None and subprocess.run(['xdpyinfo'], env=env, capture_output=True).returncode == 0, 15,
                 'the stand-in display did not come up')
        subprocess.run(['xsetroot', '-solid', '#ff0000'], env=env, check=True, capture_output=True)
        port = free_port()
        with (tmp_path / 'shadow.log').open('w') as log:
            server = subprocess.Popen([shadow, f'/port:{port}', '/sec:tls', '-auth', '/bind-address:127.0.0.1', '/log-level:WARN'],
                                      env=env, stdout=log, stderr=log, start_new_session=True)
        wait_for(lambda: server.poll() is None and listening(port), 20, 'the shadow server did not start listening')
        yield {'display': display, 'port': port, 'server': server, 'env': env}
    finally:
        for process in (server, x):
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=20)
        for leftover in (Path(f'/tmp/.X{display}-lock'), Path(f'/tmp/.X11-unix/X{display}')):
            leftover.unlink(missing_ok=True)


def test_the_panel_shows_an_rdp_desktop_through_ironrdp(tmp_path, shadowed_display):
    pytest.importorskip('playwright')
    from playwright.sync_api import sync_playwright
    chromium = shutil.which('chromium') or shutil.which('chromium-browser')
    if chromium is None:
        pytest.skip('chromium required for the RDP end-to-end test')
    viewer = viewer_scripts()
    if viewer is None:
        pytest.skip('IronRDP scripts are not in the checkout and could not be fetched')
    ports = [free_port() for _ in range(3)]
    args = [sys.executable, '-m', 'mintbot_desktop', '--state', str(tmp_path / 'state'), '--display', '299',
            '--web-port', str(ports[0]), '--vnc-port', str(ports[1]), '--cdp-port', str(ports[2])]
    env = {**os.environ, 'MINTBOT_DESKTOP_RDP_USERNAME': 'mintbot', 'MINTBOT_DESKTOP_RDP_PASSWORD': 'unchecked'}
    with (tmp_path / 'session.log').open('w+') as log:
        session = subprocess.Popen(args + ['--rdp', f'127.0.0.1:{shadowed_display["port"]}', 'serve'], stdout=log, stderr=log, env=env)
        try:
            def healthy():
                if session.poll() is not None:
                    log.seek(0)
                    pytest.fail(log.read())
                try:
                    token = (tmp_path / 'state' / 'token').read_text()
                    request = urllib.request.Request(f'http://127.0.0.1:{ports[0]}/health', headers={'Authorization': 'Bearer ' + token})
                    with urllib.request.urlopen(request, timeout=1) as response:
                        return json.load(response)['status'] == 'ok'
                except (OSError, ValueError):
                    return False
            wait_for(healthy, 30, 'the session did not come up')
            token = (tmp_path / 'state' / 'token').read_text()
            # Nothing of the package's own: no X server, no VNC port; the record says RDP.
            assert not Path('/tmp/.X11-unix/X299').exists() and not listening(ports[1])
            assert json.loads((tmp_path / 'state' / 'attached.json').read_text())['kind'] == 'rdp'
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=chromium, args=['--no-sandbox', '--disable-dev-shm-usage'])
                try:
                    context = browser.new_context(extra_http_headers={'Authorization': 'Bearer ' + token}, viewport={'width': 1000, 'height': 700})
                    page = context.new_page()
                    console = []
                    page.on('console', lambda message: console.append(f'{message.type}: {message.text}'))
                    page.on('pageerror', lambda error: console.append(f'pageerror: {error}'))
                    page.goto(f'http://127.0.0.1:{ports[0]}/desktop/')
                    # The status banner goes once IronRDP has connected through the gateway.
                    page.wait_for_function('document.getElementById("status").hidden', timeout=60_000)
                    # The frame is the stand-in display's red root window; the
                    # first one takes a moment after the connection.
                    sample = '''() => {
                        const canvas = document.querySelector('iron-remote-desktop').shadowRoot.querySelector('canvas');
                        const context = canvas.getContext('2d');
                        const data = context.getImageData(Math.floor(canvas.width / 2), Math.floor(canvas.height / 2), 1, 1).data;
                        return {width: canvas.width, height: canvas.height, rgb: [data[0], data[1], data[2]]};
                    }'''
                    deadline = time.monotonic() + 30
                    while True:
                        pixel = page.evaluate(sample)
                        red, green, blue = pixel['rgb']
                        if red > 200 and green < 60 and blue < 60:
                            break
                        assert time.monotonic() < deadline, (pixel, console)
                        time.sleep(.5)
                    # The shadow server shares at the size the client asked for
                    # (GNOME's announces the screen's own); either way a frame
                    # of the shared screen was decoded and drawn.
                    assert pixel['width'] >= 800 and pixel['height'] >= 600, (pixel, console)
                finally:
                    browser.close()
        finally:
            session.terminate()
            session.wait(timeout=20)
        log.seek(0)
        text = log.read()
        assert session.returncode == 0, text
        assert f'showing the existing desktop through its RDP server at 127.0.0.1:{shadowed_display["port"]} (given)' in text
    # The owner's server outlives the session that showed it.
    assert shadowed_display['server'].poll() is None and listening(shadowed_display['port'])
