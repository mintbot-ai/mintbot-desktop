"""Drive the shared Chromium through its DevTools endpoint with Playwright.

`mintbot-desktop browse ...` works on page elements (roles, names, selectors)
instead of pixels. Playwright lives in the extension's own virtualenv under
the install prefix; the launcher runs under the system interpreter, so this
module re-executes itself under that virtualenv before touching Playwright.

The browser stays the one the user watches in the panel: every command
attaches over CDP and detaches when it is done. Nothing here launches or
closes Chromium itself. The tab `open` navigated is remembered in the state
directory, so the commands that follow keep acting on it even though each
attachment sees the tabs in an arbitrary order.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.request

PLAYWRIGHT_REQUIREMENT = 'playwright>=1.50,<2'

# The file under the state directory that records the current tab's target id.
TAB_FILE = 'browse-tab'

# Actions whose result is printed as JSON; the others print plain text.
JSON_RESULTS = frozenset({'pages', 'open', 'eval', 'run'})

try:
    from playwright.sync_api import Error as DriverError
except ImportError:
    class DriverError(Exception):
        """Stands in for Playwright's error until the module re-executes under the driver's virtualenv."""


def venv_python(prefix=None):
    """The extension virtualenv's interpreter; the install hook creates it."""
    venv = os.environ.get('MINTBOT_DESKTOP_VENV')
    if venv:
        return Path(venv) / 'bin' / 'python'
    # lib/mintbot_desktop/browse.py -> prefix/venv
    return Path(prefix or Path(__file__).resolve().parents[2]) / 'venv' / 'bin' / 'python'


def endpoint(cfg):
    return f'http://127.0.0.1:{cfg.cdp_port}'


def http_json(url):
    # Loopback must never go through an inherited proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=5) as response:
        return json.load(response)


def status(cfg):
    """Whether the browser answers on CDP and whether the driver is installed."""
    report = {'cdp': endpoint(cfg), 'browser': None, 'playwright': False}
    try:
        report['browser'] = http_json(endpoint(cfg) + '/json/version').get('Browser')
    except OSError as error:
        report['error'] = str(error)
    python = venv_python()
    if python.exists():
        probe = subprocess.run([str(python), '-c', 'import playwright, importlib.metadata as m; print(m.version("playwright"))'],
                               capture_output=True, text=True)
        if probe.returncode == 0:
            report['playwright'] = probe.stdout.strip()
    return report


def build_parser():
    parser = argparse.ArgumentParser(prog='mintbot-desktop browse', description='Control the shared Chromium through Playwright over CDP')
    parser.add_argument('--page', metavar='TAB', help='act on the tab with this target id, or whose URL or title contains this; '
                                                       'default: the tab the last `open` navigated')
    actions = parser.add_subparsers(dest='action', required=True)
    actions.add_parser('status', help='is CDP reachable and is the driver installed')
    actions.add_parser('pages', help='list open tabs')
    open_ = actions.add_parser('open', help='navigate a tab to a URL')
    open_.add_argument('--new', action='store_true', help='open a new tab instead of reusing one')
    open_.add_argument('url')
    text = actions.add_parser('text', help='inner text of an element (default: the whole page)')
    text.add_argument('selector', nargs='?')
    snapshot = actions.add_parser('snapshot', help='accessibility tree: roles, names and text')
    snapshot.add_argument('selector', nargs='?')
    snapshot.add_argument('--roles', metavar='ROLE,ROLE', help='only nodes with these roles, e.g. button,link,textbox')
    click = actions.add_parser('click', help='click the first element matching a Playwright selector')
    click.add_argument('selector')
    fill = actions.add_parser('fill', help='clear and fill an input')
    fill.add_argument('selector')
    fill.add_argument('text')
    type_ = actions.add_parser('type', help='type text into the focused element')
    type_.add_argument('text')
    press = actions.add_parser('press', help='press a key, e.g. Enter, Tab, Control+a')
    press.add_argument('key')
    eval_ = actions.add_parser('eval', help='evaluate a JavaScript expression and print its JSON value')
    eval_.add_argument('expression')
    wait = actions.add_parser('wait', help='wait for a selector (default: for the page to load)')
    wait.add_argument('selector', nargs='?')
    wait.add_argument('--timeout', type=float, default=15, help='seconds')
    shot = actions.add_parser('screenshot', help='save a PNG of the tab')
    shot.add_argument('path')
    shot.add_argument('--full', action='store_true', help='the whole scrollable page')
    shot.add_argument('--selector', help='only the first element matching this selector')
    run_ = actions.add_parser('run', help='perform a JSON list of steps over one connection, e.g. '
                                          '[{"fill": ["input[name=q]", "hello"]}, {"press": "Enter"}, {"text": "main"}]')
    run_.add_argument('file', nargs='?', help='the steps file; default: standard input')
    return parser


def parse_step(step):
    """One step of `run` as the argv of the equivalent single command.

    A step is an object with one action key whose value is the action's
    argument list (a scalar is one argument, null is none), plus an optional
    "page" key that picks the tab like --page does.
    """
    if not isinstance(step, dict):
        raise ValueError(f'a step must be an object, not {json.dumps(step)}')
    actions = [key for key in step if key != 'page']
    if len(actions) != 1 or actions[0] in ('run', 'status'):
        raise ValueError(f'a step needs exactly one action: {json.dumps(step, ensure_ascii=False)}')
    action = actions[0]
    value = step[action]
    arguments = [] if value is None else [str(item) for item in value] if isinstance(value, list) else [str(value)]
    argv = ['--page', str(step['page'])] if step.get('page') is not None else []
    return argv + [action] + arguments


def visible(page):
    """The tab the user sees in the window; error pages may not answer."""
    try:
        return page.evaluate('document.visibilityState') == 'visible'
    except Exception:
        return False


def target_id(page):
    """Chromium's identifier of the tab, stable for as long as the tab lives."""
    try:
        session = page.context.new_cdp_session(page)
    except Exception:
        return None
    try:
        return session.send('Target.getTargetInfo')['targetInfo']['targetId']
    except Exception:
        return None
    finally:
        session.detach()


def describe(page):
    """The `page: <title> <url>` line that tells which tab a message is about."""
    try:
        title = page.title()
    except Exception:
        title = ''
    return f'page: {title} {page.url}'.rstrip()


class Memory:
    """The tab the last `open` navigated, kept across attachments."""
    def __init__(self, path):
        self.path = Path(path)

    def recall(self):
        try:
            return self.path.read_text().strip() or None
        except OSError:
            return None

    def remember(self, page):
        identity = target_id(page)
        if identity:
            self.path.write_text(identity + '\n')
        else:
            self.path.unlink(missing_ok=True)
        return identity


def target_page(browser, pattern=None, new=False, remembered=None):
    """The tab to act on: a new one, the one --page names, the remembered one, or the visible one."""
    context = browser.contexts[0]
    if new or not context.pages:
        return context.new_page()
    if pattern:
        for page in context.pages:
            if pattern == target_id(page):
                return page
        for page in context.pages:
            if pattern in page.url or pattern in page.title():
                return page
        raise RuntimeError(f'no open tab matches {pattern!r}')
    if remembered:
        for page in context.pages:
            if target_id(page) == remembered:
                return page
    # Each command is a fresh attachment, so the pages' order says nothing
    # about recency; the visible tab is the one the user is looking at.
    return next((page for page in context.pages if visible(page)), context.pages[0])


def filter_roles(snapshot, roles):
    """Only the nodes of the accessibility tree with one of the given roles, one per line."""
    wanted = {role.strip() for role in roles.split(',') if role.strip()}
    kept = []
    for line in snapshot.splitlines():
        node = re.match(r'\s*- ([a-z]+)', line)
        if node and node.group(1) in wanted:
            kept.append(line.strip())
    return '\n'.join(kept)


def perform(args, page):
    """One action on a resolved tab; the return value is what the command prints."""
    if args.action == 'open':
        page.goto(args.url, wait_until='domcontentloaded')
        page.bring_to_front()
        # No index: the attachment order of the tabs differs between commands, only the target id is stable.
        return {'targetId': target_id(page), 'url': page.url}
    if args.action == 'text':
        return page.locator(args.selector or 'body').first.inner_text()
    if args.action == 'snapshot':
        snapshot = page.locator(args.selector or 'body').first.aria_snapshot()
        return filter_roles(snapshot, args.roles) if args.roles else snapshot
    if args.action == 'click':
        page.locator(args.selector).first.click()
        return page.url
    if args.action == 'fill':
        page.locator(args.selector).first.fill(args.text)
    elif args.action == 'type':
        page.keyboard.type(args.text)
    elif args.action == 'press':
        page.keyboard.press(args.key)
    elif args.action == 'eval':
        return page.evaluate(args.expression)
    elif args.action == 'wait':
        if args.selector:
            page.locator(args.selector).first.wait_for(timeout=args.timeout * 1000)
        else:
            page.wait_for_load_state('load', timeout=args.timeout * 1000)
    elif args.action == 'screenshot':
        if args.selector:
            page.locator(args.selector).first.screenshot(path=args.path)
        else:
            page.screenshot(path=args.path, full_page=args.full)
        return args.path
    return None


def execute(args, browser, memory):
    """Perform one parsed command against a connected browser and return its result."""
    if args.action == 'pages':
        current = memory.recall()
        listing = []
        for index, page in enumerate(browser.contexts[0].pages):
            identity = target_id(page)
            listing.append({'index': index, 'targetId': identity, 'title': page.title(), 'url': page.url,
                            'current': identity is not None and identity == current})
        return listing
    page = target_page(browser, args.page, new=getattr(args, 'new', False), remembered=memory.recall())
    if args.action == 'open':
        # The tab `open` navigates becomes the default for what follows; --page is a one-off choice.
        memory.remember(page)
    try:
        return perform(args, page)
    except DriverError as error:
        # Playwright's message says what was waited for; its traceback does not help.
        raise RuntimeError(f'{describe(page)}\n{str(error).strip()}') from None


def execute_steps(steps, browser, memory, parser):
    """Perform the steps of `run` in order; the first failure stops the run and names its index."""
    if not isinstance(steps, list):
        raise ValueError('the steps must be a JSON list')
    results = []
    for index, step in enumerate(steps):
        try:
            args = parser.parse_args(parse_step(step))
            results.append(execute(args, browser, memory))
        except (ValueError, RuntimeError, SystemExit) as error:
            # The completed steps' results still reach stdout; the failure goes to stderr.
            print(json.dumps(results, ensure_ascii=False, indent=1))
            raise RuntimeError(f'step {index} failed: {json.dumps(step, ensure_ascii=False)}\n{error}') from None
    return results


def read_steps(path):
    source = sys.stdin if path in (None, '-') else open(path, encoding='utf-8')
    with source:
        return json.load(source)


def run(args, browser, memory):
    """Perform a command (or the steps of `run`) and print the result."""
    if args.action == 'run':
        result = execute_steps(read_steps(args.file), browser, memory, build_parser())
    else:
        result = execute(args, browser, memory)
    if args.action in JSON_RESULTS:
        # An evaluated value stays on one line; listings and step results are easier to read indented.
        print(json.dumps(result, ensure_ascii=False, indent=None if args.action == 'eval' else 1))
    elif result is not None:
        print(result)


def main(cfg, argv, execve=os.execve):
    args = build_parser().parse_args(argv)
    if args.action == 'status':
        print(json.dumps(status(cfg), indent=1))
        return
    python = venv_python()
    # A venv interpreter is a symlink to the system one, so compare the prefix,
    # not the resolved executable.
    if Path(sys.prefix) != python.parent.parent:
        if not python.exists():
            raise RuntimeError(f'browser driver environment missing at {python.parent.parent}; '
                               'reinstall or upgrade the extension, which sets it up')
        env = {**os.environ, 'NO_PROXY': 'localhost,127.0.0.1', 'no_proxy': 'localhost,127.0.0.1'}
        execve(str(python), [str(python), '-m', 'mintbot_desktop', '--state', str(cfg.state),
                             '--cdp-port', str(cfg.cdp_port), 'browse', *argv], env)
        return
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(endpoint(cfg), timeout=10_000)
        try:
            for context in browser.contexts:
                context.set_default_timeout(10_000)
            run(args, browser, Memory(cfg.state / TAB_FILE))
        except DriverError as error:
            raise RuntimeError(str(error).strip()) from None
        finally:
            # Detaches from a CDP-connected browser; it does not close Chromium.
            browser.close()
