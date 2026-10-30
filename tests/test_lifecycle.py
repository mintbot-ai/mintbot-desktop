import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


class FakeCommands:
    def __init__(self):
        self.calls = []
        self.installed = False
        self.playwright = False
        self.fail = None

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        if self.fail and self.fail in args:
            return subprocess.CompletedProcess(args, 1, '', 'simulated failure')
        if args[0] == 'dpkg-query':
            return subprocess.CompletedProcess(args, 0 if self.installed else 1, 'installed\n' * (len(args)-3) if self.installed else '', '')
        if args[:2] == ['apt-get', 'install']:
            self.installed = True
        if args[1:3] == ['-m', 'venv']:
            python = Path(args[3]) / 'bin' / 'python'
            python.parent.mkdir(parents=True)
            python.touch()
        if args[1:4] == ['-m', 'pip', 'install']:
            self.playwright = True
        if args[1:] == ['-c', 'import playwright']:
            return subprocess.CompletedProcess(args, 0 if self.playwright else 1, '', '')
        return subprocess.CompletedProcess(args, 0, '', '')


def lifecycle(tmp_path, run, os_release='ID=debian\nVERSION_CODENAME=trixie\n', **kwargs):
    assert importlib.util.find_spec('mintbot_desktop.lifecycle'), 'lifecycle missing'
    from mintbot_desktop.lifecycle import Lifecycle
    (tmp_path/'os-release').write_text(os_release)
    env = {'AXP_PREFIX':str(tmp_path/'prefix'), 'AXP_STATE_DIR':str(tmp_path/'state'), 'AXP_CACHE_DIR':str(tmp_path/'cache'),
           'HERMES_HOME':str(tmp_path/'hermes'), 'AXP_HOST':'mintbot'}
    return Lifecycle(source=ROOT, env=env, run=run, os_release=tmp_path/'os-release', euid=kwargs.pop('euid', 0), **kwargs)


def test_install_idempotently_converges_without_starting_host_service(tmp_path):
    commands = FakeCommands()
    hooks = lifecycle(tmp_path, commands)
    hooks.install()
    first = (tmp_path/'state'/'token').read_bytes()
    hooks.install()
    assert (tmp_path/'state'/'token').read_bytes() == first
    assert sum(c[:2] == ['apt-get','install'] for c in commands.calls) == 1
    assert not any(c[0] == 'systemctl' for c in commands.calls)
    # The browser driver venv converges the same way: built once, then probed.
    pip_installs = [c for c in commands.calls if c[1:4] == ['-m', 'pip', 'install']]
    assert len(pip_installs) == 1 and pip_installs[0][0] == str(tmp_path/'prefix'/'venv'/'bin'/'python')
    assert any('playwright' in part for part in pip_installs[0])
    assert sum(c[1:3] == ['-m', 'venv'] for c in commands.calls) == 1
    launcher = tmp_path/'prefix'/'bin'/'mintbot-desktop'
    assert launcher.is_file()
    assert (tmp_path/'hermes'/'skills'/'mintbot-desktop'/'SKILL.md').is_file()
    result = subprocess.run([str(launcher), 'env'], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['DISPLAY'] == ':13'
    assert json.loads(result.stdout)['XAUTHORITY'] == str(tmp_path/'state'/'Xauthority')


def test_install_declares_the_desktop_icon_package(tmp_path):
    """idesk draws the browser icon on the desktop; the install hook installs
    it with the other GUI packages, so an upgrade of an older install adds it."""
    from mintbot_desktop.lifecycle import PACKAGES
    assert 'idesk' in PACKAGES and 'chromium' in PACKAGES and 'xdotool' in PACKAGES
    commands = FakeCommands()
    lifecycle(tmp_path, commands).install()
    install = next(c for c in commands.calls if c[:2] == ['apt-get', 'install'])
    assert 'idesk' in install
    probe = next(c for c in commands.calls if c[0] == 'dpkg-query')
    assert 'idesk' in probe


class FakeChildren:
    """Children.start without processes: each start hands out a process whose
    exit the test controls."""
    class Process:
        def __init__(self):
            self.returncode = None

        def poll(self):
            return self.returncode

    def __init__(self):
        self.started = []
        self.stopped = []

    def start(self, argv, env=None):
        process = self.Process()
        self.started.append((argv, process))
        return process

    def stop(self, process):
        process.returncode = -15
        self.stopped.append(process)


def test_the_browser_is_started_once_and_never_restarted(capsys):
    """The user closes the browser like any program; a browser that came
    back on its own would be in their way. The desktop icon and the menu
    open it again instead."""
    from mintbot_desktop.__main__ import Once
    children = FakeChildren()
    browser = Once(children, 'browser', ['chromium'], {})
    assert [argv for argv, _ in children.started] == [['chromium']]
    browser.check()
    children.started[0][1].returncode = 0
    for _ in range(5):
        browser.check()
    assert len(children.started) == 1
    assert capsys.readouterr().err.count('browser exited with status 0; not restarted') == 1


def test_the_window_manager_and_icons_stay_supervised():
    from mintbot_desktop.__main__ import RESTART_LIMIT, Supervised
    children = FakeChildren()
    openbox = Supervised(children, 'window manager', ['openbox'], {})
    for _ in range(RESTART_LIMIT):
        children.started[-1][1].returncode = 1
        openbox.check()
    assert len(children.started) == RESTART_LIMIT + 1
    children.started[-1][1].returncode = 1
    with pytest.raises(RuntimeError, match='keeps exiting'):
        openbox.check()


def test_an_icon_layer_that_keeps_dying_is_dropped_not_the_session(capsys):
    """The icons are a convenience: the openbox menu opens the browser without
    them. The icon layer's give-up ends the layer, not the X server, openbox
    and the browser the user is working in."""
    from mintbot_desktop.__main__ import RESTART_LIMIT, Supervised, check_icons
    children = FakeChildren()
    icons = Supervised(children, 'desktop icons', ['idesk'], {})
    for _ in range(RESTART_LIMIT):
        children.started[-1][1].returncode = 1
        assert check_icons(icons) is icons
    children.started[-1][1].returncode = 1
    assert check_icons(icons) is None
    assert len(children.started) == RESTART_LIMIT + 1
    assert 'desktop icons keeps exiting (last status 1); giving up; the desktop goes on without icons' in capsys.readouterr().err


def test_a_deliberate_restart_is_not_a_death():
    """The icon layer is started over whenever the window manager comes back;
    those restarts are nobody's crash and leave the crash budget alone."""
    from mintbot_desktop.__main__ import RESTART_LIMIT, Supervised
    children = FakeChildren()
    icons = Supervised(children, 'desktop icons', ['idesk'], {})
    assert icons.check() is False
    for _ in range(RESTART_LIMIT + 2):
        icons.restart()
    assert len(children.stopped) == RESTART_LIMIT + 2
    assert len(children.started) == RESTART_LIMIT + 3 and icons.process is children.started[-1][1]
    children.started[-1][1].returncode = 1
    assert icons.check() is True
    assert icons.process.poll() is None


async def test_the_icon_layer_waits_for_the_window_manager(capsys):
    """openbox puts its anchor window at the bottom of the stack when it
    starts and keeps every application window beneath it; idesk lowers its
    icons only when it creates them. Icons created first stay above every
    window for good, so the icon layer waits for openbox to publish
    _NET_SUPPORTING_WM_CHECK on the root window."""
    import asyncio
    from mintbot_desktop.__main__ import Supervised, wait_for_window_manager
    children = FakeChildren()
    manager = Supervised(children, 'window manager', ['openbox'], {})
    stop = asyncio.Event()

    async def up(env):
        return True

    async def silent(env):
        return False
    await wait_for_window_manager(manager, {}, stop, ready=up, timeout=5)
    assert capsys.readouterr().err == ''
    # Up but silent: logged, then the session goes on without the icons in order.
    await wait_for_window_manager(manager, {}, stop, ready=silent, timeout=.3)
    assert 'window manager readiness timed out' in capsys.readouterr().err
    # A window manager that dies while waited for is restarted right there.
    children.started[-1][1].returncode = 1
    await wait_for_window_manager(manager, {}, stop, ready=up, timeout=5)
    assert [argv for argv, _ in children.started] == [['openbox'], ['openbox']]
    assert 'window manager exited with status 1; restarting' in capsys.readouterr().err
    # Asked to stop, the wait ends at once.
    stop.set()
    await asyncio.wait_for(wait_for_window_manager(manager, {}, stop, ready=silent, timeout=5), timeout=1)


async def test_window_manager_readiness_outlasts_a_dead_openbox_only_on_the_root(monkeypatch):
    """The root window keeps a dead openbox's _NET_SUPPORTING_WM_CHECK; the
    anchor it names is gone with its owner, and the next openbox marks its
    own anchor only after lowering it. Readiness asks the anchor too."""
    import mintbot_desktop.__main__ as main
    answers, asked = {}, []

    async def fake_xprop(env, args, timeout=2.0):
        asked.append(args)
        return answers.get(args[0] if args[0] == '-root' else args[1], b'_NET_SUPPORTING_WM_CHECK:  not found.\n')
    monkeypatch.setattr(main, 'xprop', fake_xprop)
    assert await main.window_manager_ready({}) is False
    assert asked == [['-root', '_NET_SUPPORTING_WM_CHECK']]
    answers['-root'] = b'_NET_SUPPORTING_WM_CHECK(WINDOW): window id # 0x40011f\n'
    assert await main.window_manager_ready({}) is False
    assert asked[-1] == ['-id', '0x40011f', '_NET_SUPPORTING_WM_CHECK']
    answers['0x40011f'] = b'_NET_SUPPORTING_WM_CHECK(WINDOW): window id # 0x40011f\n'
    assert await main.window_manager_ready({}) is True


def test_desktop_icon_and_menu_open_the_browser(tmp_path):
    """idesk reads its icon from HOME, openbox its root menu from
    XDG_CONFIG_HOME: both are the state directory, both run the package's
    own `browser` command with the interpreter and path spelled out. idesk
    hands its command to `sh -c` from its own process, so `exec` makes that
    process the launcher itself (which must tell where it runs); openbox
    spawns the menu's command from its words, without a shell."""
    import shlex
    from mintbot_desktop.session import BROWSER_ICON, Config, browser_launcher, write_desktop
    cfg = Config(state=tmp_path / 'state')
    cfg.state.mkdir()
    write_desktop(cfg)
    write_desktop(cfg)  # every start rewrites; nothing accumulates
    command = browser_launcher(cfg)
    assert command[-1] == 'browser' and command[-3:-1] == ['--state', str(cfg.state)]
    assert command[:2] == ['env', f'PYTHONPATH={ROOT}'] and '-m' in command and 'mintbot_desktop' in command
    icon = (cfg.state / '.idesktop' / 'browser.lnk').read_text()
    assert f'Command: exec {shlex.join(command)}\n' in icon
    assert f'Icon: {BROWSER_ICON}' in icon and BROWSER_ICON.endswith('chromium.png')
    assert 'table Config' in (cfg.state / '.ideskrc').read_text()
    menu = (cfg.state / 'config' / 'openbox' / 'menu.xml').read_text()
    assert 'id="root-menu"' in menu
    assert f'<execute>{shlex.join(command)}</execute>' in menu
    assert sorted(p.name for p in (cfg.state / '.idesktop').iterdir()) == ['browser.lnk']


def test_apt_failure_does_not_report_success_or_start_services(tmp_path):
    commands = FakeCommands()
    commands.fail = 'update'
    hooks = lifecycle(tmp_path, commands)
    with pytest.raises(subprocess.CalledProcessError):
        hooks.install()
    assert not (tmp_path/'prefix'/'bin'/'mintbot-desktop').exists()
    assert not any(c[0] == 'systemctl' for c in commands.calls)


def test_driver_install_failure_does_not_report_success(tmp_path):
    commands = FakeCommands()
    commands.fail = 'install'
    commands.installed = True  # APT is satisfied; only pip fails
    hooks = lifecycle(tmp_path, commands)
    with pytest.raises(subprocess.CalledProcessError):
        hooks.install()
    assert not (tmp_path/'prefix'/'bin'/'mintbot-desktop').exists()


def test_apt_uses_only_declared_debian_mirrors(tmp_path):
    """A machine's own APT sources (provider mirrors, third-party repositories)
    are not this package's to depend on; both apt-get calls must read a private
    source list naming Debian's own mirrors and a private index dir."""
    commands = FakeCommands()
    hooks = lifecycle(tmp_path, commands, os_release='PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nID=debian\nVERSION_CODENAME=trixie\n')
    hooks.install()
    apt_calls = [c for c in commands.calls if c[0] == 'apt-get']
    assert [c[1] for c in apt_calls] == ['update', 'install']
    sources = tmp_path/'cache'/'apt'/'debian.sources'
    text = sources.read_text()
    assert 'http://deb.debian.org/debian/' in text and 'http://deb.debian.org/debian-security/' in text
    assert 'Suites: trixie trixie-updates' in text and 'Suites: trixie-security' in text
    for call in apt_calls:
        options = [call[i+1] for i, part in enumerate(call) if part == '-o']
        assert f'Dir::Etc::SourceList={sources}' in options
        assert f'Dir::Etc::SourceParts={tmp_path/"cache"/"apt"/"sources.list.d"}' in options
        assert f'Dir::State::Lists={tmp_path/"cache"/"apt"/"lists"}' in options
    assert (tmp_path/'cache'/'apt'/'lists'/'partial').is_dir()
    assert not any((tmp_path/'cache'/'apt'/'sources.list.d').iterdir())


def test_install_declares_the_scraping_server_package(tmp_path):
    """X0tigervnc shows a computer's own desktop; the install hook installs
    it with the other GUI packages, so an upgrade of an older install adds it."""
    from mintbot_desktop.lifecycle import PACKAGES
    assert 'tigervnc-scraping-server' in PACKAGES
    commands = FakeCommands()
    lifecycle(tmp_path, commands).install()
    assert 'tigervnc-scraping-server' in next(c for c in commands.calls if c[:2] == ['apt-get', 'install'])


ALL_PROGRAMS = lambda name: '/usr/bin/' + name
NOTHING = lambda path: False


@pytest.mark.parametrize('host', ['ubuntu', 'unprivileged'])
def test_without_apt_the_programs_already_there_are_used(tmp_path, capsys, host):
    """A customer's own Linux computer: the agent is not root, and Ubuntu's
    packages are not Debian's. The hook installs nothing there; what the
    session needs must be there already, and then the install goes through."""
    os_release = 'ID=ubuntu\nVERSION_CODENAME=noble\n' if host == 'ubuntu' else 'ID=debian\nVERSION_CODENAME=trixie\n'
    euid = 0 if host == 'ubuntu' else 1000
    commands = FakeCommands()
    hooks = lifecycle(tmp_path, commands, os_release=os_release, euid=euid, which=ALL_PROGRAMS, present=lambda path: True)
    hooks.install()
    assert not any(c[0] in ('apt-get', 'dpkg-query') for c in commands.calls)
    assert (tmp_path/'prefix'/'bin'/'mintbot-desktop').is_file()
    assert (tmp_path/'hermes'/'skills'/'mintbot-desktop'/'SKILL.md').is_file()
    assert capsys.readouterr().err == ''


def test_without_apt_what_is_missing_is_named_by_package(tmp_path):
    commands = FakeCommands()
    hooks = lifecycle(tmp_path, commands, euid=1000, which=lambda name: None, present=NOTHING)
    with pytest.raises(RuntimeError, match='not root on Debian') as error:
        hooks.install()
    message = str(error.value)
    for package in ['tigervnc-tools', 'xauth', 'x11-utils', 'tigervnc-standalone-server or tigervnc-scraping-server', 'novnc']:
        assert package in message
    assert not (tmp_path/'prefix').exists() and not (tmp_path/'hermes'/'skills').exists()
    assert not any(c[0] == 'apt-get' for c in commands.calls)
    # One X server is enough: the scraping one alone shows a computer's own desktop.
    scraper_only = lambda name: '/usr/bin/' + name if name in ('X0tigervnc', 'tigervncpasswd', 'xauth', 'xdpyinfo', 'xprop') else None
    hooks = lifecycle(tmp_path, commands, euid=1000, which=scraper_only, present=lambda path: True)
    hooks.install()
    assert (tmp_path/'prefix'/'bin'/'mintbot-desktop').is_file()
    # The Python modules the gateway imports are checked with the interpreter the launcher uses.
    commands.fail = 'import aiohttp, Cryptodome'
    with pytest.raises(RuntimeError, match='python3-aiohttp python3-pycryptodome'):
        lifecycle(tmp_path, commands, euid=1000, which=ALL_PROGRAMS, present=lambda path: True).install()


def test_without_apt_missing_optional_programs_cost_a_feature_not_the_install(tmp_path, capsys):
    required_only = lambda name: '/usr/bin/' + name if name in ('Xtigervnc', 'tigervncpasswd', 'xauth', 'xdpyinfo', 'xprop') else None
    hooks = lifecycle(tmp_path, FakeCommands(), euid=1000, which=required_only, present=lambda path: True)
    hooks.install()
    assert (tmp_path/'prefix'/'bin'/'mintbot-desktop').is_file()
    err = capsys.readouterr().err
    assert err.startswith('mintbot-desktop: not installed, each costs a feature')
    for package in ['openbox', 'idesk', 'chromium', 'xdotool', 'wmctrl', 'xclip', 'maim']:
        assert package in err


def test_failure_summary_keeps_the_failed_commands_own_message():
    from mintbot_desktop.lifecycle import failure_summary
    error = subprocess.CalledProcessError(100, ['apt-get', 'update', '-o', 'x'], output='Hit:1 ...',
                                         stderr='W: first\nE: Failed to fetch http://mirror.hetzner.com/... 403 Forbidden\n')
    summary = failure_summary(error)
    assert summary.startswith('apt-get update exited 100:')
    assert summary.endswith('403 Forbidden')
    assert 'Traceback' not in summary


def test_main_reports_a_failed_command_without_a_traceback(tmp_path, capsys, monkeypatch):
    import mintbot_desktop.lifecycle as module

    class Failing(module.Lifecycle):
        def install(self):
            raise subprocess.CalledProcessError(100, ['apt-get', 'update'], stderr='E: mirror refused\n')

    monkeypatch.setattr(module, 'Lifecycle', Failing)
    with pytest.raises(SystemExit) as exit_info:
        module.main(['lifecycle', 'install'])
    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert err.strip().splitlines() == ['mintbot-desktop: apt-get update exited 100:', 'E: mirror refused']


class FakeGateway:
    """The session's HTTP gateway as urllib sees it: refusing connections
    for the first `refusals` probes (a session still coming up), then ok."""

    def __init__(self, refusals, status=200, body='{"status": "ok"}'):
        self.refusals, self.status, self.body = refusals, status, body
        self.probes = []

    def open(self, request, timeout):
        self.probes.append(request.full_url)
        if len(self.probes) <= self.refusals:
            raise OSError('[Errno 111] Connection refused')
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def read(self):
        return self.body.encode()


def health_state(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'session.json').write_text(json.dumps({'web_port': 6081, 'display': 13, 'vnc_port': 5913, 'cdp_port': 9223}))
    (state / 'token').write_text('secret')
    return state


def test_health_waits_for_a_session_that_is_still_coming_up(tmp_path, monkeypatch):
    """The host runs the health hook the instant it restarted the service
    (right after an update); the gateway listens a second or two later."""
    import urllib.request
    import mintbot_desktop.lifecycle as module
    health_state(tmp_path)
    gateway = FakeGateway(refusals=3)
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *handlers: gateway)
    hooks = lifecycle(tmp_path, FakeCommands())
    now, slept = [0.0], []
    hooks.health(wait=25, sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)), clock=lambda: now[0])
    assert gateway.probes == ['http://127.0.0.1:6081/health'] * 4
    assert slept == [module.HEALTH_RETRY_S] * 3
    # A gateway that stays silent past the deadline is down, with the reason.
    gateway = FakeGateway(refusals=10 ** 6)
    with pytest.raises(RuntimeError, match=r'port 6081 did not answer within 25s: \[Errno 111\] Connection refused'):
        hooks.health(wait=25, sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0])
    assert 25 / module.HEALTH_RETRY_S <= len(gateway.probes) <= 25 / module.HEALTH_RETRY_S + 2
    # One that answers something other than ok is down too, at once.
    gateway = FakeGateway(refusals=0, status=503, body='{"status": "down"}')
    with pytest.raises(RuntimeError, match='answered 503'):
        hooks.health(wait=0, sleep=lambda s: None, clock=lambda: 0)


def test_main_reports_a_failed_health_on_stdout(tmp_path, capsys, monkeypatch):
    """The host shows the health hook's stdout as the red dot's detail."""
    import urllib.request
    import mintbot_desktop.lifecycle as module
    state = health_state(tmp_path)
    monkeypatch.setenv('AXP_STATE_DIR', str(state))
    monkeypatch.setenv('AXP_PREFIX', str(tmp_path / 'prefix'))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'hermes'))
    monkeypatch.setattr(module, 'HEALTH_WAIT_S', 0)
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *handlers: FakeGateway(refusals=10))
    with pytest.raises(SystemExit) as exit_info:
        module.main(['lifecycle', 'health'])
    assert exit_info.value.code == 1
    out, err = capsys.readouterr()
    assert out.startswith('mintbot-desktop: the desktop gateway on port 6081 did not answer within 0s') and err == ''


@pytest.mark.parametrize('host', ['mintbot', 'mintbot/2026.10', 'other/1', ''])
def test_disable_never_controls_a_host_service(tmp_path, host):
    commands = FakeCommands()
    hooks = lifecycle(tmp_path, commands)
    hooks.env['AXP_HOST'] = host
    hooks.install()
    before = list(commands.calls)
    hooks.disable()
    hooks.disable()
    assert commands.calls == before
    assert (tmp_path/'state'/'token').exists()


def test_standalone_install_reports_foreground_workflow_and_enable_runs_it(tmp_path, capsys):
    commands = FakeCommands()
    hooks = lifecycle(tmp_path, commands)
    hooks.env.pop('AXP_HOST')
    hooks.install()
    launcher = str(hooks.prefix / 'bin' / 'mintbot-desktop')
    assert launcher in capsys.readouterr().out
    before = len(commands.calls)
    hooks.enable()
    assert commands.calls[before:] == [[launcher, 'serve']]
    commands.fail = 'serve'
    with pytest.raises(subprocess.CalledProcessError):
        hooks.enable()


@pytest.mark.parametrize('host', ['mintbot', 'mintbot/2026.10', 'other/1'])
def test_host_enable_leaves_activation_to_host(tmp_path, host):
    commands = FakeCommands()
    hooks = lifecycle(tmp_path, commands)
    hooks.env['AXP_HOST'] = host
    hooks.install()
    before = list(commands.calls)
    hooks.enable()
    assert commands.calls == before


@pytest.mark.parametrize('hook', ['disable', 'uninstall'])
@pytest.mark.parametrize('host', ['', 'mintbot/2026.10'])
def test_live_session_refuses_disable_or_uninstall_without_signalling(tmp_path, hook, host):
    import fcntl
    commands = FakeCommands()
    hooks = lifecycle(tmp_path, commands)
    hooks.env['AXP_HOST'] = host
    hooks.install()
    hooks.env['AXP_PURGE'] = '1'
    before = list(commands.calls)
    with (hooks.state / 'session.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match='Stop.*foreground.*service'):
            getattr(hooks, hook)()
        assert (hooks.state / 'token').exists()
        assert (hooks.prefix / 'bin' / 'mintbot-desktop').exists()
    assert commands.calls == before
    getattr(hooks, hook)()  # stale lock file is not a running session


def test_standalone_uninstall_removes_prefix_preserves_state_and_is_repeatable(tmp_path):
    hooks = lifecycle(tmp_path, FakeCommands())
    hooks.env.pop('AXP_HOST')
    hooks.install()
    token = (hooks.state / 'token').read_bytes()
    hooks.uninstall()
    assert not hooks.prefix.exists()
    assert (hooks.state / 'token').read_bytes() == token
    hooks.uninstall()
    hooks.install()
    assert (hooks.state / 'token').read_bytes() == token
    hooks.env['AXP_PURGE'] = '1'
    hooks.uninstall()
    hooks.uninstall()
    assert not hooks.state.exists()


def test_host_uninstall_leaves_prefix_cleanup_to_host(tmp_path):
    hooks = lifecycle(tmp_path, FakeCommands())
    hooks.install()
    hooks.uninstall()
    assert (hooks.prefix / 'bin' / 'mintbot-desktop').exists()


@pytest.mark.parametrize('prefix', ['/', '/opt', '/usr/local', '/var/lib', '/home', '/root', '', '.', 'relative/prefix'])
def test_unsafe_prefix_is_refused_before_any_command(tmp_path, prefix):
    from mintbot_desktop.lifecycle import Lifecycle
    commands = FakeCommands()
    with pytest.raises(ValueError, match='Unsafe'):
        Lifecycle(source=ROOT, env={'AXP_PREFIX': prefix, 'AXP_STATE_DIR': str(tmp_path/'state')}, run=commands)
    assert not commands.calls


@pytest.mark.parametrize('nested', ['prefix-in-state', 'state-in-prefix', 'prefix-is-home', 'prefix-is-source', 'symlink'])
def test_prefix_aliases_and_overlaps_are_refused(tmp_path, nested):
    from mintbot_desktop.lifecycle import Lifecycle
    prefix, state = tmp_path/'prefix', tmp_path/'state'
    home = tmp_path/'hermes'
    if nested == 'prefix-in-state':
        prefix = state/'prefix'
    elif nested == 'state-in-prefix':
        state = prefix/'state'
    elif nested == 'prefix-is-home':
        prefix = home
    elif nested == 'prefix-is-source':
        prefix = ROOT
    else:
        target = tmp_path/'target'
        target.mkdir()
        prefix.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match='Unsafe'):
        Lifecycle(source=ROOT, env={'AXP_PREFIX':str(prefix), 'AXP_STATE_DIR':str(state),
                                  'HERMES_HOME':str(home)}, run=FakeCommands())


@pytest.mark.parametrize('record', ['missing', 'wrong-prefix', 'malformed', 'symlink'])
def test_uninstall_refuses_unrecognized_installation_before_deleting_anything(tmp_path, record):
    hooks = lifecycle(tmp_path, FakeCommands())
    hooks.env.pop('AXP_HOST')
    hooks.install()
    hooks.env['AXP_PURGE'] = '1'
    path = hooks.state/'install.json'
    if record == 'missing':
        path.unlink()
    elif record == 'wrong-prefix':
        path.write_text(json.dumps({'prefix':str(tmp_path/'other'), 'hermes_home':str(hooks.hermes)}))
    elif record == 'malformed':
        path.write_text('{}')
    else:
        other = tmp_path/'record.json'
        path.rename(other)
        path.symlink_to(other)
    with pytest.raises(ValueError, match='installation record'):
        hooks.uninstall()
    assert (hooks.prefix/'bin'/'mintbot-desktop').exists()
    assert (hooks.state/'token').exists()
    assert (hooks.hermes/'skills'/'mintbot-desktop'/'SKILL.md').exists()


def test_standalone_install_refuses_to_claim_nonempty_unowned_prefix(tmp_path):
    commands = FakeCommands()
    hooks = lifecycle(tmp_path, commands)
    hooks.env.pop('AXP_HOST')
    hooks.prefix.mkdir()
    unrelated = hooks.prefix/'unrelated'
    unrelated.write_text('keep')
    with pytest.raises(ValueError, match='installation record'):
        hooks.install()
    assert unrelated.read_text() == 'keep'
    assert commands.calls == []


def test_standalone_retry_after_driver_failure_preserves_ownership(tmp_path):
    commands = FakeCommands()
    hooks = lifecycle(tmp_path, commands)
    hooks.env.pop('AXP_HOST')
    commands.installed = True
    commands.fail = 'install'
    with pytest.raises(subprocess.CalledProcessError):
        hooks.install()
    commands.fail = None
    hooks.install()
    assert (hooks.prefix/'bin'/'mintbot-desktop').exists()
    hooks.uninstall()
    assert not hooks.prefix.exists()


@pytest.mark.parametrize('target', ['prefix', 'state', 'skill', 'lock'])
def test_uninstall_rechecks_symlinks_before_cleanup(tmp_path, target):
    hooks = lifecycle(tmp_path, FakeCommands())
    hooks.env.pop('AXP_HOST')
    hooks.install()
    hooks.env['AXP_PURGE'] = '1'
    paths = {'prefix': hooks.prefix, 'state': hooks.state,
             'skill': hooks.hermes/'skills'/'mintbot-desktop',
             'lock': hooks.state/'session.lock'}
    path = paths[target]
    if target == 'lock':
        path.touch()
    outside = tmp_path/'outside'
    path.rename(outside)
    path.symlink_to(outside, target_is_directory=target != 'lock')
    with pytest.raises(ValueError, match='Unsafe'):
        hooks.uninstall()
    assert outside.exists()
    assert (hooks.state/'token').exists()
    assert (hooks.prefix/'bin'/'mintbot-desktop').exists()


def test_uninstall_retains_state_unless_explicit_purge(tmp_path):
    commands = FakeCommands()
    hooks = lifecycle(tmp_path, commands)
    hooks.install()
    hooks.uninstall()
    assert (tmp_path/'state'/'token').exists()
    assert not (tmp_path/'hermes'/'skills'/'mintbot-desktop').exists()
    hooks.env['AXP_PURGE'] = '1'
    hooks.uninstall()
    assert not (tmp_path/'state').exists()
