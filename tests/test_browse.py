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


class FakeSession:
    def __init__(self, target):
        self.target = target

    def send(self, method):
        assert method == 'Target.getTargetInfo'
        return {'targetInfo': {'targetId': self.target}}

    def detach(self):
        pass


class FakePage:
    def __init__(self, url, title, shown=False, target='T'):
        self.url, self._title, self.shown, self.log = url, title, shown, []
        self.target = target
        self.context = self
        page = self

        class Keyboard:
            def press(self, key):
                page.log.append(('press', key))

            def type(self, text):
                page.log.append(('type', text))
        self.keyboard = Keyboard()

    def new_cdp_session(self, page):
        return FakeSession(page.target)

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
                if selector == 'input[name=missing]':
                    raise browse.DriverError(f'Locator.fill: Timeout 10000ms exceeded.\nCall log:\n  - waiting for {selector}\n')
                page.log.append(('fill', selector, text))

            def wait_for(s, timeout):
                page.log.append(('wait', selector, timeout))

            def screenshot(s, path):
                page.log.append(('shot', path, selector))
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
        self.opened = 0

    def new_page(self):
        self.opened += 1
        page = FakePage('about:blank', '', target=f'NEW{self.opened}')
        self.pages.append(page)
        return page


@pytest.fixture
def memory(tmp_path):
    return browse.Memory(tmp_path / browse.TAB_FILE)


def act(browser, memory, *argv):
    browse.run(browse.build_parser().parse_args(argv), browser, memory)


def test_actions_target_the_visible_tab_a_named_tab_or_a_new_one(capsys, memory):
    new = FakePage('https://b.example/', 'Beta', shown=True, target='B')
    old = FakePage('https://a.example/', 'Alpha', target='A')
    browser = FakeBrowser(new, old)  # attachment order says nothing about recency
    act(browser, memory, 'pages')
    listing = json.loads(capsys.readouterr().out)
    assert [(p['title'], p['targetId'], p['current']) for p in listing] == [('Beta', 'B', False), ('Alpha', 'A', False)]
    act(browser, memory, 'open', 'https://c.example/')
    assert new.log == [('goto', 'https://c.example/'), ('front',)] and old.log == []
    assert json.loads(capsys.readouterr().out) == {'targetId': 'B', 'url': 'https://c.example/'}
    act(browser, memory, '--page', 'Alpha', 'click', 'text=Log in')
    assert old.log == [('click', 'text=Log in')]
    act(browser, memory, '--page', 'A', 'press', 'Enter')  # a target id picks a tab too, without changing the default
    assert old.log[-1] == ('press', 'Enter') and memory.recall() == 'B'
    act(browser, memory, 'open', '--new', 'https://d.example/')
    assert len(browser.pages) == 3 and browser.pages[-1].url == 'https://d.example/'
    new.shown, browser.pages[-1].shown = False, True
    act(browser, memory, 'fill', 'input[name=q]', 'hello')
    act(browser, memory, 'text')
    act(browser, memory, 'snapshot', 'main')
    act(browser, memory, 'eval', 'document.title')
    act(browser, memory, 'screenshot', '/tmp/x.png', '--full')
    act(browser, memory, 'screenshot', '/tmp/y.png', '--selector', 'main')
    out = capsys.readouterr().out
    assert 'text of body' in out and '- document: main' in out and '{"expr": "document.title"}' in out
    assert browser.pages[-1].log[-3:] == [('fill', 'input[name=q]', 'hello'), ('shot', '/tmp/x.png', True), ('shot', '/tmp/y.png', 'main')]
    with pytest.raises(RuntimeError, match='no open tab matches'):
        act(browser, memory, '--page', 'nowhere', 'text')


def test_commands_stay_in_the_tab_the_previous_command_used(capsys, memory):
    """Each command is a fresh attachment; a tab that `open --new` created once
    received no follow-up commands because another tab was still the visible
    one at that moment. The remembered target id keeps the sequence together."""
    error_tab = FakePage('chrome-error://chromewebdata/', 'ERR', shown=True, target='ERR')
    browser = FakeBrowser(error_tab)
    act(browser, memory, 'open', '--new', 'https://et.wikipedia.org/')
    opened = browser.pages[-1]
    assert memory.recall() == opened.target and json.loads(capsys.readouterr().out)['targetId'] == opened.target
    act(browser, memory, 'fill', 'input[name=search]', 'Tallinn')  # the error tab is still the visible one
    assert opened.log[-1] == ('fill', 'input[name=search]', 'Tallinn') and error_tab.log == []
    act(browser, memory, 'pages')
    assert [p['current'] for p in json.loads(capsys.readouterr().out)] == [False, True]
    act(browser, memory, '--page', 'ERR', 'press', 'Enter')  # a one-off choice leaves the default alone
    assert error_tab.log == [('press', 'Enter')] and memory.recall() == opened.target
    browser.pages.remove(opened)  # the user closed it: fall back to the visible tab
    act(browser, memory, 'press', 'Enter')
    assert error_tab.log == [('press', 'Enter')] * 2


def test_errors_name_the_tab_they_happened_in(memory):
    page = FakePage('https://a.example/login', 'Alpha', shown=True, target='A')
    with pytest.raises(RuntimeError) as failure:
        act(FakeBrowser(page), memory, 'fill', 'input[name=missing]', 'x')
    lines = str(failure.value).splitlines()
    assert lines[0] == 'page: Alpha https://a.example/login'
    assert lines[1] == 'Locator.fill: Timeout 10000ms exceeded.'


def test_run_performs_steps_over_one_connection_and_stops_at_the_first_failure(tmp_path, capsys, memory):
    page = FakePage('https://a.example/', 'Alpha', shown=True, target='A')
    browser = FakeBrowser(page)
    steps = tmp_path / 'steps.json'
    steps.write_text(json.dumps([{'fill': ['input[name=q]', 'hello']}, {'press': 'Enter'}, {'wait': ['text=Results', '--timeout', '3']},
                                 {'text': 'main'}, {'eval': 'document.title'}, {'open': ['--new', 'https://b.example/']}, {'text': None}]))
    act(browser, memory, 'run', str(steps))
    results = json.loads(capsys.readouterr().out)
    assert results[:5] == [None, None, None, 'text of main', {'expr': 'document.title'}]
    assert results[5] == {'targetId': 'NEW1', 'url': 'https://b.example/'} and results[6] == 'text of body'
    assert page.log == [('fill', 'input[name=q]', 'hello'), ('press', 'Enter'), ('wait', 'text=Results', 3000.0)]
    assert browser.pages[-1].log == [('goto', 'https://b.example/'), ('front',)] and memory.recall() == 'NEW1'
    steps.write_text(json.dumps([{'page': 'Alpha', 'click': 'text=Go'}, {'page': 'Alpha', 'fill': ['input[name=missing]', 'x']}, {'press': 'Enter'}]))
    with pytest.raises(RuntimeError, match=r'step 1 failed: \{"page": "Alpha", "fill": \["input\[name=missing\]", "x"\]\}\npage: Alpha https://a.example/') as failure:
        act(browser, memory, 'run', str(steps))
    assert 'Timeout 10000ms' in str(failure.value)
    assert json.loads(capsys.readouterr().out) == ['https://a.example/']  # the completed steps' results still arrive
    assert page.log[-1] == ('click', 'text=Go')


def test_run_reads_the_steps_from_stdin_and_rejects_malformed_steps(monkeypatch, capsys, memory):
    import io
    browser = FakeBrowser(FakePage('https://a.example/', 'Alpha', shown=True, target='A'))
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps([{'press': 'Enter'}, {'click': 'a'}])))
    act(browser, memory, 'run')
    assert json.loads(capsys.readouterr().out) == [None, 'https://a.example/']
    for bad in ([{'fill': ['x'], 'press': 'Enter'}], [{'run': None}], ['press'], [{'press': ['Enter', '--bogus']}]):
        monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(bad)))
        with pytest.raises(RuntimeError, match='step 0 failed'):
            act(browser, memory, 'run', '-')
        capsys.readouterr()
    monkeypatch.setattr(sys, 'stdin', io.StringIO('{}'))
    with pytest.raises(ValueError, match='JSON list'):
        act(browser, memory, 'run')


def test_snapshot_roles_keep_only_the_nodes_an_agent_can_act_on():
    tree = ('- document "Search":\n'
            '  - banner:\n'
            '    - link "Home" [url=/]\n'
            '    - heading "Welcome" [level=1]\n'
            '  - main:\n'
            '    - paragraph: Some long\n'
            '      wrapped text\n'
            '    - textbox "Search"\n'
            '    - button "Go" [disabled]\n'
            '    - list:\n'
            '      - listitem:\n'
            '        - link "Result 1"\n')
    assert browse.filter_roles(tree, 'button,link,textbox').splitlines() == [
        '- link "Home" [url=/]', '- textbox "Search"', '- button "Go" [disabled]', '- link "Result 1"']
    assert browse.filter_roles(tree, ' heading ') == '- heading "Welcome" [level=1]'
    assert browse.filter_roles(tree, 'nothing') == ''


def test_steps_map_onto_the_single_command_syntax():
    assert browse.parse_step({'press': 'Enter'}) == ['press', 'Enter']
    assert browse.parse_step({'text': None}) == ['text']
    assert browse.parse_step({'page': 'Alpha', 'wait': ['text=Done', '--timeout', 5]}) == ['--page', 'Alpha', 'wait', 'text=Done', '--timeout', '5']
    for bad in ('press', {}, {'page': 'x'}, {'press': 'a', 'type': 'b'}, {'status': None}):
        with pytest.raises(ValueError):
            browse.parse_step(bad)


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
