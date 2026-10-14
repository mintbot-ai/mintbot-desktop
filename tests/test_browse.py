import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from mintbot_desktop import browse
from mintbot_desktop.session import Config

ROOT = Path(__file__).resolve().parents[1]


class FakePage:
    def __init__(self, url, title, shown=False):
        self.url, self._title, self.shown, self.log = url, title, shown, []

    def title(self):
        return self._title

    def goto(self, url, **kw):
        self.url = url
        self.log.append(('goto', url))

    def bring_to_front(self):
        self.log.append(('front',))

    def locator(self, selector):
        page = self

        class Locator:
            first = property(lambda s: s)

            def inner_text(s):
                return f'text of {selector}'

            def aria_snapshot(s):
                return f'- document: {selector}'

            def click(s):
                page.log.append(('click', selector))

            def fill(s, text):
                page.log.append(('fill', selector, text))

            def wait_for(s, timeout):
                page.log.append(('wait', selector, timeout))
        return Locator()

    def evaluate(self, expression):
        if expression == 'document.visibilityState':
            return 'visible' if self.shown else 'hidden'
        return {'expr': expression}

    def screenshot(self, path, full_page):
        self.log.append(('shot', path, full_page))


class FakeBrowser:
    def __init__(self, *pages):
        context = self
        self.pages = list(pages)
        self.contexts = [context]

    def new_page(self):
        page = FakePage('about:blank', '')
        self.pages.append(page)
        return page


def act(browser, *argv):
    browse.run(browse.build_parser().parse_args(argv), browser)


def test_actions_target_the_visible_tab_a_named_tab_or_a_new_one(capsys):
    new = FakePage('https://b.example/', 'Beta', shown=True)
    old = FakePage('https://a.example/', 'Alpha')
    browser = FakeBrowser(new, old)  # attachment order says nothing about recency
    act(browser, 'pages')
    assert [p['title'] for p in json.loads(capsys.readouterr().out)] == ['Beta', 'Alpha']
    act(browser, 'open', 'https://c.example/')
    assert new.log == [('goto', 'https://c.example/'), ('front',)] and old.log == []
    act(browser, '--page', 'Alpha', 'click', 'text=Log in')
    assert old.log == [('click', 'text=Log in')]
    act(browser, 'open', '--new', 'https://d.example/')
    assert len(browser.pages) == 3 and browser.pages[-1].url == 'https://d.example/'
    new.shown, browser.pages[-1].shown = False, True
    act(browser, 'fill', 'input[name=q]', 'hello')
    act(browser, 'text')
    act(browser, 'snapshot', 'main')
    act(browser, 'eval', 'document.title')
    act(browser, 'screenshot', '/tmp/x.png', '--full')
    out = capsys.readouterr().out
    assert 'text of body' in out and '- document: main' in out and '{"expr": "document.title"}' in out
    assert browser.pages[-1].log[-2:] == [('fill', 'input[name=q]', 'hello'), ('shot', '/tmp/x.png', True)]
    with pytest.raises(RuntimeError, match='no open tab matches'):
        act(browser, '--page', 'nowhere', 'text')


def test_status_reads_cdp_without_the_driver(tmp_path, monkeypatch):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({'Browser': 'Chrome/155.0'}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass
    server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv('MINTBOT_DESKTOP_VENV', str(tmp_path / 'no-venv'))
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:9')  # loopback must bypass any proxy
    try:
        report = browse.status(Config(state=tmp_path, cdp_port=server.server_address[1]))
    finally:
        server.shutdown()
        server.server_close()
    assert report['browser'] == 'Chrome/155.0' and report['playwright'] is False


def test_browse_reexecs_under_the_extension_venv_or_says_what_is_missing(tmp_path, monkeypatch):
    cfg = Config(state=tmp_path, cdp_port=9224)
    monkeypatch.setenv('MINTBOT_DESKTOP_VENV', str(tmp_path / 'venv'))
    with pytest.raises(RuntimeError, match='reinstall or upgrade'):
        browse.main(cfg, ['pages'])
    python = tmp_path / 'venv' / 'bin' / 'python'
    python.parent.mkdir(parents=True)
    python.touch()
    seen = {}
    browse.main(cfg, ['--page', 'x', 'click', 'text=Go'], execve=lambda path, argv, env: seen.update(path=path, argv=argv, env=env))
    assert seen['path'] == str(python)
    assert seen['argv'][1:] == ['-m', 'mintbot_desktop', '--state', str(tmp_path), '--cdp-port', '9224',
                                'browse', '--page', 'x', 'click', 'text=Go']
    assert seen['env']['NO_PROXY'] == 'localhost,127.0.0.1'


def test_launcher_exposes_browse_status(tmp_path):
    import socket
    with socket.socket() as probe:  # a free port nothing listens on afterwards
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    (tmp_path / 'session.json').write_text(json.dumps({'cdp_port': port}))
    result = subprocess.run([sys.executable, '-m', 'mintbot_desktop', '--state', str(tmp_path), 'browse', 'status'],
                            capture_output=True, text=True, cwd=ROOT,
                            env={**os.environ, 'PYTHONPATH': str(ROOT), 'MINTBOT_DESKTOP_VENV': str(tmp_path / 'none')})
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['cdp'] == f'http://127.0.0.1:{port}' and report['browser'] is None and 'error' in report
