"""Drive the shared Chromium through its DevTools endpoint with Playwright.

`mintbot-desktop browse ...` works on page elements (roles, names, selectors)
instead of pixels. Playwright lives in the extension's own virtualenv under
the install prefix; the launcher runs under the system interpreter, so this
module re-executes itself under that virtualenv before touching Playwright.

The browser stays the one the user watches in the panel: every command
attaches over CDP and detaches when it is done. Nothing here launches or
closes Chromium itself.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request

PLAYWRIGHT_REQUIREMENT = 'playwright>=1.50,<2'


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
    parser.add_argument('--page', metavar='SUBSTRING', help='act on the tab whose URL or title contains this; default: the newest tab')
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
    return parser


def visible(page):
    """The tab the user sees in the window; error pages may not answer."""
    try:
        return page.evaluate('document.visibilityState') == 'visible'
    except Exception:
        return False


def target_page(browser, pattern=None, new=False):
    context = browser.contexts[0]
    if new or not context.pages:
        return context.new_page()
    if pattern:
        for page in context.pages:
            if pattern in page.url or pattern in page.title():
                return page
        raise RuntimeError(f'no open tab matches {pattern!r}')
    # Each command is a fresh attachment, so the pages' order says nothing
    # about recency; the visible tab is the one the user is looking at.
    return next((page for page in context.pages if visible(page)), context.pages[0])


def run(args, browser):
    """Perform one action against a connected Playwright browser."""
    if args.action == 'pages':
        print(json.dumps([{'index': i, 'title': page.title(), 'url': page.url}
                          for i, page in enumerate(browser.contexts[0].pages)], ensure_ascii=False, indent=1))
        return
    page = target_page(browser, args.page, new=getattr(args, 'new', False))
    if args.action == 'open':
        page.goto(args.url, wait_until='domcontentloaded')
        page.bring_to_front()
        print(page.url)
    elif args.action == 'text':
        print(page.locator(args.selector or 'body').first.inner_text())
    elif args.action == 'snapshot':
        print(page.locator(args.selector or 'body').first.aria_snapshot())
    elif args.action == 'click':
        page.locator(args.selector).first.click()
        print(page.url)
    elif args.action == 'fill':
        page.locator(args.selector).first.fill(args.text)
    elif args.action == 'type':
        page.keyboard.type(args.text)
    elif args.action == 'press':
        page.keyboard.press(args.key)
    elif args.action == 'eval':
        print(json.dumps(page.evaluate(args.expression), ensure_ascii=False))
    elif args.action == 'wait':
        if args.selector:
            page.locator(args.selector).first.wait_for(timeout=args.timeout * 1000)
        else:
            page.wait_for_load_state('load', timeout=args.timeout * 1000)
    elif args.action == 'screenshot':
        page.screenshot(path=args.path, full_page=args.full)
        print(args.path)


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
    from playwright.sync_api import Error, sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(endpoint(cfg), timeout=10_000)
        try:
            for context in browser.contexts:
                context.set_default_timeout(10_000)
            run(args, browser)
        except Error as error:
            # Playwright's message says what was waited for; its traceback does not help.
            raise RuntimeError(error.message.strip()) from None
        finally:
            # Detaches from a CDP-connected browser; it does not close Chromium.
            browser.close()
